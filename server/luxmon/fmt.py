"""Форматирование для сообщений: длительности, размеры, подписи областей."""
import re


def scope_label(scope):
    return {"all": "весь кластер", "ceph": "Ceph", "patroni": "PostgreSQL/etcd", "checks": "проверки сервисов",
            "backups": "бэкапы", "ssh": "входы по SSH"}.get(scope, scope.replace("host:", "хост ").replace("vm:", "VM "))


def fmt_minutes(m):
    return f"{m // 60} ч" + (f" {m % 60} мин" if m % 60 else "") if m >= 60 else f"{m} мин"


def fmt_dur(sec):
    sec = int(sec)
    if sec >= 86400:
        return f"{sec // 86400} д {sec % 86400 // 3600} ч"
    return fmt_minutes(sec // 60) if sec >= 60 else f"{sec} с"


def fmt_bytes(v):
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(v) < 1024 or u == "ТБ":
            return f"{v:.0f} {u}" if u == "Б" else f"{v:.1f} {u}"
        v /= 1024


def parse_minutes(s):
    m = re.fullmatch(r"(\d+)\s*(m|min|мин|h|ч|d|д)?", s.lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "m"
    return n * (1440 if unit in ("d", "д") else 60 if unit in ("h", "ч") else 1)
