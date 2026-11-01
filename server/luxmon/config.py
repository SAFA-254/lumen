"""Настройки сервера: файл конфигурации, пороги алертов и их значения по умолчанию."""
import json
import os


CONFIG_PATH = os.environ.get("LUXMON_CONFIG", "/etc/luxmon/server.json")
TG_ENV_PATH = os.environ.get("LUXMON_TG_ENV", "/etc/luxmon/telegram.env")
DEFAULTS = {
    "listen": "0.0.0.0",
    "port": 8095,
    "redirect_port": 0,       # если задан — HTTP на этом порту перенаправляет на HTTPS
    "public_url": "",          # адрес для ссылок и перенаправления, например https://192.168.3.7
    "tls_cert": "",
    "tls_key": "",
    "token": "",
    "users": {},               # user -> pbkdf2 hash, задаётся командой passwd
    "session_days": 14,
    "db": "/var/lib/luxmon/luxmon.db",
    "web": os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"),
    "notify_cmd": "",
    "raw_keep_hours": 48,
    "rollup_keep_days": 30,
    "rules": {},
}

# Правила алертов: ключ -> (значение по умолчанию, подпись, единица, мин, макс). Меняются в настройках.
RULES_META = {
    "host_down_sec": (90, "Хост не присылает данные", "с", 30, 3600),
    "cpu_pct": (90, "CPU выше", "%", 10, 100),
    "cpu_for_sec": (300, "…дольше", "с", 0, 7200),
    "mem_pct": (92, "Память выше", "%", 10, 100),
    "mem_for_sec": (300, "…дольше", "с", 0, 7200),
    "swap_pct": (50, "Swap занят больше", "%", 1, 100),
    "load_per_cpu": (1.5, "Load average на ядро выше (5 мин)", "", 0.1, 100),
    "disk_lat_ms": (100, "Задержка диска выше (15 мин)", "мс", 1, 100000),
    "net_err_per_min": (10, "Ошибок на сетевом интерфейсе больше", "в мин", 0.1, 100000),
    "mem_psi_pct": (10, "Процессы ждут память (подкачка) дольше", "% времени", 1, 100),
    "disk_warn_pct": (85, "Диск: предупреждение", "%", 10, 100),
    "disk_crit_pct": (95, "Диск: критично", "%", 10, 100),
    "temp_c": (85, "Температура CPU выше", "°C", 40, 120),
    "patroni_lag_bytes": (16 * 1024 * 1024, "Лаг реплики PostgreSQL больше", "байт", 0, 10 ** 12),
    "pg_conn_pct": (80, "Подключений к PostgreSQL больше", "% от max", 10, 100),
    "pg_xact_age_sec": (600, "Транзакция PostgreSQL дольше", "с", 30, 86400),
    "backup_max_age_h": (36, "Бэкап гостя старше", "ч", 1, 720),
    "cluster_stale_sec": (300, "Данные кластера устарели через", "с", 60, 3600),
    "check_for_sec": (60, "Проверка сервиса не проходит дольше", "с", 0, 3600),
    "etcd_fsync_ms": (100, "fsync etcd больше", "мс", 1, 10000),
    "etcd_for_sec": (600, "…дольше", "с", 0, 7200),
    "osd_latency_ms": (250, "Задержка записи OSD больше", "мс", 1, 100000),
    "forecast_days": (14, "Предупреждать, если диск заполнится через", "дн", 1, 365),
    "net_util_pct": (80, "Загрузка сетевого канала выше", "%", 10, 100),
    "net_for_sec": (300, "…дольше", "с", 0, 7200),
    "log_err_per_min": (5, "Ошибок в журнале больше", "в мин", 0.1, 100000),
    "log_for_sec": (300, "…дольше", "с", 0, 7200),
    "smart_wear_pct": (80, "Износ SSD больше", "%", 1, 100),
    "smart_temp_c": (60, "Температура диска выше", "°C", 30, 100),
    "cert_days": (14, "Сертификат HTTPS истекает раньше чем через", "дн", 1, 365),
    "clear_sec": (300, "Считать алерт решённым, когда проблемы нет уже", "с", 0, 3600),
    "remind_h": (4, "Напоминать о незакрытом алерте каждые (0 — нет)", "ч", 0, 168),
    "iowait_pct": (30, "Ожидание диска (iowait) выше", "%", 1, 100),
    "iowait_for_sec": (600, "…дольше", "с", 0, 7200),
    "anomaly_factor": (4, "Нагрузка выше обычной для этого часа в (0 — не следить)", "раз", 0, 100),
    "agent_cpu_pct": (10, "Агент Luxmon тратит CPU больше", "%", 1, 100),
    "agent_io_kbs": (5120, "Агент Luxmon читает/пишет диск больше", "КБ/с", 10, 10 ** 6),
    "clock_skew_ms": (500, "Часы хоста расходятся больше чем на", "мс", 10, 600000),
    "pbs_gc_days": (3, "Очистка (GC) PBS не проходила дольше", "дн", 1, 60),
    "pbs_verify_days": (14, "Проверка (verify) PBS не проходила дольше", "дн", 1, 120),
}
DEFAULT_SETTINGS = {
    "summary_time": "09:00",
    "weekly_report": True,
    "checks_runner": "pve1",
    "ssh_known": [],
}

# счётчики SMART, рост которых сообщается сразу
SMART_NAMES = {"realloc": "переназначенных секторов", "pending": "секторов ждут переназначения",
               "uncorrectable": "нечитаемых секторов", "reported_uncorrect": "неисправленных ошибок",
               "crc": "ошибок CRC (кабель SATA)"}
ROLLUP_STEP = 300
SERIES_MIN_GAP = 20  # кластерные ряды присылают несколько агентов — пишем не чаще раза в 20 с
SILENCE_SCOPES = ("all", "ceph", "patroni", "checks", "backups", "ssh")
LOGIN_LIMIT = (5, 600)  # неудачных попыток за столько секунд с одного адреса


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    return cfg


CFG = load_config()
