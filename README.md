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
| `patroni` | хост, которому доступны API Patroni и etcd | участники кластера, лидер, лаг, здоровье etcd |

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
- `GET /api/history?host=pve1&metrics=cpu,mem&range=86400` — временные ряды

## Заметки

- Если шлюз подменяет TLS-сертификаты (как USIAG), поставь его корневой сертификат в систему. `tg-notify.py` отключает только строгую проверку X.509 в Python 3.13, потому что в сертификате USIAG нет поля Authority Key Identifier; цепочка и имя хоста по-прежнему проверяются.
- `install.sh` написан под конкретный кластер: адреса узлов и номера CT в нём заданы явно.
