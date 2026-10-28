# Lumen

Лёгкий мониторинг для небольшого кластера Proxmox VE. Агенты на хостах раз в 15 секунд отправляют метрики на сервер, сервер хранит их в SQLite, показывает одностраничный дашборд и шлёт алерты в Telegram.

Без зависимостей: только Python 3 из стандартной поставки и один HTML-файл без CDN. Агент и сервер занимают примерно по 20–25 МБ памяти.

## Что собирается

| Модуль    | Где запускать                    | Что даёт |
|-----------|----------------------------------|----------|
| `system`  | везде                            | CPU, iowait, RAM, swap, диски, сеть, дисковый I/O, load, температура |
| `pve`     | узлы Proxmox                     | статус узлов, все VM/CT (статус, CPU, RAM, аптайм), хранилища |
| `ceph`    | узлы Proxmox с Ceph              | health и проверки, OSD, MON, PG, заполнение пулов, I/O |
| `backups` | узлы Proxmox                     | задания vzdump и последний бэкап каждого гостя на PBS (с verify) |
| `patroni` | хост, которому доступны API Patroni и etcd | участники кластера, лидер, лаг, здоровье etcd, время fsync WAL и commit etcd |
| `checks`  | хост, которому видны сервисы     | проверки HTTP (код, текст в ответе), TCP и ping со временем ответа |

Модуль `ceph` дополнительно собирает задержку записи каждой OSD (`ceph osd perf`). Задержки OSD, etcd, лаг репликации и время ответа сервисов сохраняются как временные ряды, их графики открываются из панелей Ceph, Patroni и «Сервисы».

### Проверки сервисов

Список задаётся в `agent.json` (пример в `examples/checks.json`):

```json
"modules": ["system", "checks"],
"checks": [
  {"name": "Nextcloud", "type": "http", "url": "http://192.168.3.10/status.php", "expect_text": "installed"},
  {"name": "PVE pve1",  "type": "http", "url": "https://192.168.3.3:8006/", "insecure": true},
  {"name": "PostgreSQL","type": "tcp",  "host": "192.168.3.11", "port": 5432},
  {"name": "db1",       "type": "ping", "host": "192.168.3.11"}
]
```

Для HTTP по умолчанию успехом считается код меньше 400; `expect_status` задаёт свой список кодов, `insecure` отключает проверку сертификата. Проверки идут параллельно раз в `checks_interval` секунд (по умолчанию 30).

Кластерные модули можно включить сразу на нескольких узлах: сервер берёт самые свежие данные, поэтому падение одного узла не оставляет дашборд пустым.

## Алерты

Пороги задаются в `rules` в `server.json`. Сообщение приходит, когда проблема появилась и когда решилась.

- хост не присылает данные дольше 90 с
- CPU выше 90% или RAM выше 92% дольше 5 минут
- диск заполнен больше чем на 85% (предупреждение) или 95% (критично)
- температура CPU выше 85 °C
- Ceph не в `HEALTH_OK`
- нет лидера Patroni, участник не в состоянии running/streaming, лаг реплики больше 16 МБ, сменился лидер
- узел etcd нездоров
- задание бэкапа завершилось ошибкой, последний бэкап гостя старше 36 ч
- узел Proxmox не в сети
- проверка сервиса не проходит дольше 60 с
- средний fsync WAL etcd больше 100 мс, задержка записи OSD больше 250 мс дольше 2 минут

## Режим обслуживания

Кнопка «🔕 Обслуживание» на дашборде глушит алерты на время работ: для всего кластера, отдельного хоста, Ceph, Patroni/etcd, проверок или бэкапов, от 30 минут до суток. Заглушённые алерты видны на дашборде, но в Telegram не уходят. Если проблема осталась после окончания окна, уведомление придёт. О начале и конце обслуживания тоже приходит сообщение.

API: `POST /api/silence` с телом `{"scope": "host:pve2", "minutes": 60, "reason": "..."}` и заголовком `X-Lumen: 1`; снять — `POST /api/silence/delete` с `{"id": "..."}`.

Старт и остановка VM/CT пишутся в ленту событий на дашборде, но в Telegram не отправляются.

## Хранение

Сырые точки хранятся 48 часов, средние за 5 минут — 30 дней. При просмотре длинного периода дашборд сам переключается на агрегаты.

## Структура

```
agent/lumen-agent.py        агент
server/lumen-server.py      сервер: приём, SQLite, API, алерты
server/web/index.html       дашборд
server/tg-notify.py         отправка в Telegram
deploy/*.service            юниты systemd
examples/                   примеры конфигов
install.sh                  обновление кода на всех хостах текущего кластера
```

## Установка

**Сервер** (лучше в отдельном CT, а не на узле):

```bash
mkdir -p /opt/lumen /etc/lumen && chmod 700 /etc/lumen
cp -r server/lumen-server.py server/tg-notify.py server/web /opt/lumen/
cp examples/server.json /etc/lumen/server.json            # задать token
cp examples/telegram.env.example /etc/lumen/telegram.env  # токен бота и chat id
chmod 600 /etc/lumen/*
cp deploy/lumen-server.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now lumen-server
```

**Агент** на каждом хосте:

```bash
mkdir -p /opt/lumen /etc/lumen && chmod 700 /etc/lumen
cp agent/lumen-agent.py /opt/lumen/
cp examples/agent-pve-node.json /etc/lumen/agent.json     # server, token, host, modules
chmod 600 /etc/lumen/agent.json
cp deploy/lumen-agent.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now lumen-agent
```

Дашборд открывается по адресу `http://<сервер>:8095`. Чтобы закрыть его паролем, задай `ui_user` и `ui_password` в `server.json`.

## API

- `POST /api/push` — приём данных от агента, нужен заголовок `Authorization: Bearer <token>`
- `GET /api/overview` — текущее состояние: хосты, кластер, алерты, события
- `GET /api/history?host=pve1&metrics=cpu,mem&range=86400` — временные ряды (кластерные ряды лежат под псевдохостами `_ceph`, `_etcd`, `_checks`)
- `GET /api/metrics?host=_ceph` — какие ряды есть у хоста за последний час
- `POST /api/silence`, `POST /api/silence/delete` — режим обслуживания

## Заметки

- Если шлюз подменяет TLS-сертификаты (как USIAG), поставь его корневой сертификат в систему. `tg-notify.py` отключает только строгую проверку X.509 в Python 3.13, потому что в сертификате USIAG нет поля Authority Key Identifier; цепочка и имя хоста по-прежнему проверяются.
- `install.sh` написан под конкретный кластер: адреса узлов и номера CT в нём заданы явно.
