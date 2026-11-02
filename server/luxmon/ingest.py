"""Приём данных от агентов и учёт входов по SSH."""
import ipaddress
import json
import sys
import time

from .config import SERIES_MIN_GAP
from .store import S, event
from .settings import checks_list, settings, validate_check
from .alerts import ALERTS


METHODS = {"publickey": "ключ", "password": "пароль", "keyboard-interactive": "пароль",
           "invalid user": "нет такого пользователя"}
SSH_FAIL_SENT = {}  # (host, ip) -> когда сообщали о переборе


def addr_names():
    """IP -> имя: свои адреса хостов с агентом и подписи из списка известных адресов («IP имя»)."""
    out = {}
    for name, latest in S.q("SELECT name, latest FROM hosts"):
        for ip in (json.loads(latest).get("info") or {}).get("ips") or []:
            out.setdefault(ip, name)
    for k in settings()["ssh_known"]:
        parts = k.split(None, 1)
        if len(parts) == 2 and "/" not in parts[0]:
            out[parts[0]] = parts[1]
    return out


def ssh_bruteforce(host, now):
    """5 и больше неудачных попыток с одного адреса за 10 минут — сообщаем, не чаще раза в час."""
    for ip, n, users in S.q("SELECT ip, COUNT(*), GROUP_CONCAT(DISTINCT user) FROM logins "
                            "WHERE host=? AND fail=1 AND ts>? GROUP BY ip HAVING COUNT(*) >= 5", (host, now - 600)):
        if now - SSH_FAIL_SENT.get((host, ip), 0) < 3600:
            continue
        SSH_FAIL_SENT[(host, ip)] = now
        msg = f"{host}: {n} неудачных попыток входа по SSH за 10 мин с {ip} (пользователи: {users[:120]})"
        event("warn", msg)
        if not ALERTS.silenced("ssh:" + host):
            ALERTS.notify("🚫 " + msg)


def ip_known(ip, known):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for k in known:
        try:
            if addr in ipaddress.ip_network(k.split()[0], strict=False):
                return True
        except ValueError:
            continue
    return False


SERIES_LAST = {}
CLOCK_SKEW = {}  # host -> последние оценки расхождения часов, мс (агент присылает время отправки)


def ingest(p, auth=None):
    host = str(p["host"])[:64]
    now = time.time()
    ts = int(p.get("ts") or now)
    rows = [(host, k, ts, float(v)) for k, v in (p.get("metrics") or {}).items() if isinstance(v, (int, float))]
    for d in p.get("disks") or []:
        if d.get("size"):
            rows.append((host, "disk:" + d["mount"], ts, round(100.0 * d["used"] / d["size"], 2)))
    for group, values in (p.get("series") or {}).items():
        if not group.startswith("_"):
            continue
        for k, v in values.items():
            key = (group, k)
            if isinstance(v, (int, float)) and ts - SERIES_LAST.get(key, 0) >= SERIES_MIN_GAP:
                SERIES_LAST[key] = ts
                rows.append((group[:64], k[:128], ts, float(v)))
    latest = {k: p.get(k) for k in ("metrics", "disks", "info", "interval", "ifaces")}
    latest["ts"] = ts
    latest["auth"] = auth  # "host" — свой токен, "shared" — общий
    clock = dict(p.get("clock") or {})
    if isinstance(p.get("sent"), (int, float)):
        # время в пути до сервера — миллисекунды, медиана по 5 отправкам сглаживает задержки сети
        hist = CLOCK_SKEW.setdefault(host, [])
        hist.append((p["sent"] - now) * 1000)
        del hist[:-5]
        clock["skew_ms"] = round(sorted(hist)[len(hist) // 2], 1)
    latest["clock"] = clock
    known = settings()["ssh_known"]
    ssh = p.get("ssh_logins") or []
    # строки входов и неудачных попыток: host, ts, user, ip, method, known, port, end, key, fail
    logins = [(host, float(x["ts"]), str(x["user"])[:64], str(x["ip"])[:64], str(x.get("method", ""))[:32],
               int(ip_known(x["ip"], known)), x.get("port"), None, str(x["key"])[:80] if x.get("key") else None,
               int(x.get("kind") == "fail"))
              for x in ssh if x.get("kind", "in") in ("in", "fail")]
    logouts = [(float(x["ts"]), host, str(x["ip"])[:64], x.get("port"), str(x["user"])[:64])
               for x in ssh if x.get("kind") == "out"]

    def w(db):
        db.execute("INSERT OR REPLACE INTO hosts VALUES(?,?,?)", (host, now, json.dumps(latest)))
        db.executemany("INSERT INTO raw VALUES(?,?,?,?)", rows)
        # агент шлёт входы повторно, если не дождался ответа, хотя сервер их уже сохранил
        logins[:] = [x for x in logins if not db.execute(
            "SELECT 1 FROM logins WHERE host=? AND ts=? AND user=? AND ip=? AND port IS ? AND fail=?",
            (x[0], x[1], x[2], x[3], x[6], x[9])).fetchone()]
        db.executemany("INSERT INTO logins(host, ts, user, ip, method, known, port, end, key, fail) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", logins)
        db.executemany("UPDATE logins SET end=? WHERE host=? AND ip=? AND port=? AND user=? AND end IS NULL "
                       "AND fail=0", logouts)
        if isinstance(p.get("ssh_open"), list):
            # сессия числится открытой, а подключения уже нет — выход не попал в журнал агента
            alive = {(str(x[0]), int(x[1])) for x in p["ssh_open"] if isinstance(x, list) and len(x) == 2}
            gone = [(now, rowid) for rowid, ip, port in db.execute(
                "SELECT rowid, ip, port FROM logins WHERE host=? AND end IS NULL AND fail=0 AND port IS NOT NULL "
                "AND ts < ?", (host, now - 30)) if (ip, port) not in alive]
            db.executemany("UPDATE logins SET end=? WHERE rowid=?", gone)
        for kind, data in (p.get("cluster") or {}).items():
            if data is not None:
                db.execute("INSERT OR REPLACE INTO cluster VALUES(?,?,?,?)", (kind, now, host, json.dumps(data)))
    S.tx(w)

    names = addr_names()
    for h, lts, user, ip, method, ok, port, _, key, fail in logins:
        if not ok and not fail:
            via = f"ключ {key}" if key else METHODS.get(method, method)
            msg = f"Вход по SSH на {h}: {user} с {ip}{names.get(ip, '') and ' (' + names[ip] + ')'}, {via} — " \
                  f"адрес не в списке известных"
            event("warn", msg)
            if not ALERTS.silenced("ssh:" + h):
                ALERTS.notify("🔐 " + msg)
    if any(x[9] for x in logins):
        ssh_bruteforce(host, now)

    # первая настройка проверок: забираем список у агента, который их выполняет
    if checks_list() is None and p.get("checks_local"):
        try:
            S.meta_set("checks", [validate_check(c) for c in p["checks_local"]])
            st = settings()
            st["checks_runner"] = host
            S.meta_set("settings", st)
        except ValueError as e:
            print("checks import:", e, file=sys.stderr, flush=True)

    if checks_list() is not None and not settings()["checks_runner"]:
        st = settings()
        st["checks_runner"] = host
        S.meta_set("settings", st)
        event("info", f"Проверки сервисов выполняет {host}: другой хост можно выбрать в настройках")

    reply = {"ok": True}
    if checks_list() is not None:
        # остальные хосты получают пустой список, чтобы не дублировать проверки из своего agent.json
        reply["checks"] = checks_list() if host == settings()["checks_runner"] else []
    return reply
