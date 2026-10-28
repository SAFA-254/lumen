#!/usr/bin/env python3
"""Lumen server: принимает метрики от агентов, хранит в SQLite, отдаёт API и веб-интерфейс,
шлёт алерты, утреннюю сводку и отвечает на команды в Telegram. Только стандартная библиотека Python.

Использование:
  lumen-server.py               запустить сервер
  lumen-server.py passwd USER   задать пароль для входа в веб-интерфейс"""
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CONFIG_PATH = os.environ.get("LUMEN_CONFIG", "/etc/lumen/server.json")
TG_ENV_PATH = os.environ.get("LUMEN_TG_ENV", "/etc/lumen/telegram.env")
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
    "db": "/var/lib/lumen/lumen.db",
    "web": os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"),
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
    "osd_latency_ms": (250, "Задержка записи OSD больше", "мс", 1, 100000),
    "forecast_days": (14, "Предупреждать, если диск заполнится через", "дн", 1, 365),
    "net_util_pct": (80, "Загрузка сетевого канала выше", "%", 10, 100),
    "net_for_sec": (300, "…дольше", "с", 0, 7200),
    "log_err_per_min": (5, "Ошибок в журнале больше", "в мин", 0.1, 100000),
    "log_for_sec": (300, "…дольше", "с", 0, 7200),
    "smart_wear_pct": (80, "Износ SSD больше", "%", 1, 100),
    "smart_temp_c": (60, "Температура диска выше", "°C", 30, 100),
    "cert_days": (14, "Сертификат HTTPS истекает раньше чем через", "дн", 1, 365),
    "remind_h": (4, "Напоминать о незакрытом алерте каждые (0 — нет)", "ч", 0, 168),
    "iowait_pct": (30, "Ожидание диска (iowait) выше", "%", 1, 100),
    "iowait_for_sec": (600, "…дольше", "с", 0, 7200),
    "anomaly_factor": (4, "Нагрузка выше обычной для этого часа в (0 — не следить)", "раз", 0, 100),
    "agent_cpu_pct": (10, "Агент Lumen тратит CPU больше", "%", 1, 100),
    "agent_io_kbs": (5120, "Агент Lumen читает/пишет диск больше", "КБ/с", 10, 10 ** 6),
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


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.lock = threading.Lock()
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS hosts(name TEXT PRIMARY KEY, last_ts REAL, latest TEXT);
        CREATE TABLE IF NOT EXISTS raw(host TEXT, metric TEXT, ts INTEGER, v REAL);
        CREATE INDEX IF NOT EXISTS raw_idx ON raw(host, metric, ts);
        CREATE TABLE IF NOT EXISTS roll(host TEXT, metric TEXT, ts INTEGER, v REAL, vmax REAL,
                                        PRIMARY KEY(host, metric, ts)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS cluster(kind TEXT PRIMARY KEY, ts REAL, src TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS events(ts REAL, level TEXT, msg TEXT);
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS alert_log(id INTEGER PRIMARY KEY, key TEXT, level TEXT, msg TEXT,
                                             start REAL, end REAL, muted INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS alert_log_start ON alert_log(start);
        CREATE TABLE IF NOT EXISTS silence_log(id TEXT PRIMARY KEY, scope TEXT, reason TEXT, by TEXT,
                                               start REAL, end REAL);
        CREATE TABLE IF NOT EXISTS logins(host TEXT, ts REAL, user TEXT, ip TEXT, method TEXT, known INTEGER);
        CREATE INDEX IF NOT EXISTS logins_ts ON logins(ts);
        """)

    def q(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def tx(self, fn):
        with self.lock:
            self.db.execute("BEGIN")
            try:
                fn(self.db)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def meta_get(self, k, default=None):
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return json.loads(r[0][0]) if r else default

    def meta_set(self, k, v):
        self.q("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, json.dumps(v)))


S = Store(CFG["db"])


def event(level, msg):
    S.q("INSERT INTO events VALUES(?,?,?)", (time.time(), level, msg))


# ---------------------------------------------------------------- settings

def rules():
    """Действующие пороги: значения по умолчанию, затем server.json, затем правки из интерфейса."""
    r = {k: v[0] for k, v in RULES_META.items()}
    r.update(CFG.get("rules") or {})
    r.update(S.meta_get("rules_override", {}))
    return r


def settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(S.meta_get("settings", {}))
    return s


def checks_list():
    return S.meta_get("checks")  # None, пока список не перенесён с агента


def validate_check(c):
    t = c.get("type")
    name = str(c.get("name", "")).strip()[:60]
    if not name or t not in ("http", "tcp", "ping"):
        raise ValueError(f"проверка «{name}»: нужно имя и тип http/tcp/ping")
    out = {"name": name, "type": t}
    if t == "http":
        url = str(c.get("url", "")).strip()
        if not re.match(r"^https?://[^\s]+$", url):
            raise ValueError(f"проверка «{name}»: неверный URL")
        out["url"] = url
        if c.get("insecure"):
            out["insecure"] = True
        if c.get("expect_text"):
            out["expect_text"] = str(c["expect_text"])[:200]
    else:
        host = str(c.get("host", "")).strip()
        if not re.match(r"^[\w.\-:]+$", host):
            raise ValueError(f"проверка «{name}»: неверный адрес")
        out["host"] = host
        if t == "tcp":
            port = int(c.get("port", 0))
            if not 1 <= port <= 65535:
                raise ValueError(f"проверка «{name}»: неверный порт")
            out["port"] = port
    return out


def ip_known(ip, known):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for k in known:
        try:
            if addr in ipaddress.ip_network(k, strict=False):
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------- ingest

SERIES_LAST = {}
CLOCK_SKEW = {}  # host -> последние оценки расхождения часов, мс (агент присылает время отправки)


def ingest(p):
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
    latest = {k: p.get(k) for k in ("metrics", "disks", "info", "interval")}
    latest["ts"] = ts
    clock = dict(p.get("clock") or {})
    if isinstance(p.get("sent"), (int, float)):
        # время в пути до сервера — миллисекунды, медиана по 5 отправкам сглаживает задержки сети
        hist = CLOCK_SKEW.setdefault(host, [])
        hist.append((p["sent"] - now) * 1000)
        del hist[:-5]
        clock["skew_ms"] = round(sorted(hist)[len(hist) // 2], 1)
    latest["clock"] = clock
    known = settings()["ssh_known"]
    logins = [(host, float(x["ts"]), str(x["user"])[:64], str(x["ip"])[:64], str(x.get("method", ""))[:32],
               int(ip_known(x["ip"], known))) for x in (p.get("ssh_logins") or [])]

    def w(db):
        db.execute("INSERT OR REPLACE INTO hosts VALUES(?,?,?)", (host, now, json.dumps(latest)))
        db.executemany("INSERT INTO raw VALUES(?,?,?,?)", rows)
        db.executemany("INSERT INTO logins VALUES(?,?,?,?,?,?)", logins)
        for kind, data in (p.get("cluster") or {}).items():
            if data is not None:
                db.execute("INSERT OR REPLACE INTO cluster VALUES(?,?,?,?)", (kind, now, host, json.dumps(data)))
    S.tx(w)

    for h, lts, user, ip, method, ok in logins:
        if not ok:
            msg = f"Вход по SSH на {h}: {user} с {ip} ({method}) — адрес не в списке известных"
            event("warn", msg)
            if not ALERTS.silenced("ssh:" + h):
                ALERTS.notify("🔐 " + msg)

    # первая настройка проверок: забираем список у агента, который их выполняет
    if checks_list() is None and p.get("checks_local"):
        try:
            S.meta_set("checks", [validate_check(c) for c in p["checks_local"]])
            st = settings()
            st["checks_runner"] = host
            S.meta_set("settings", st)
        except ValueError as e:
            print("checks import:", e, file=sys.stderr, flush=True)

    reply = {"ok": True}
    if checks_list() is not None:
        # остальные хосты получают пустой список, чтобы не дублировать проверки из своего agent.json
        reply["checks"] = checks_list() if host == settings()["checks_runner"] else []
    return reply


# ---------------------------------------------------------------- maintenance

def rollup_and_prune():
    now = int(time.time())
    last = S.meta_get("rollup_ts", now - 3 * 86400)
    upto = now - now % ROLLUP_STEP  # только завершённые корзины
    keep = now - CFG["rollup_keep_days"] * 86400

    def w(db):
        db.execute(f"""INSERT OR REPLACE INTO roll
            SELECT host, metric, ts - ts % {ROLLUP_STEP}, AVG(v), MAX(v) FROM raw
            WHERE ts >= ? AND ts < ? GROUP BY host, metric, ts - ts % {ROLLUP_STEP}""",
                   (last - last % ROLLUP_STEP, upto))
        db.execute("DELETE FROM raw WHERE ts < ?", (now - CFG["raw_keep_hours"] * 3600,))
        db.execute("DELETE FROM roll WHERE ts < ?", (keep,))
        db.execute("DELETE FROM events WHERE ts < ?", (keep,))
        db.execute("DELETE FROM alert_log WHERE end IS NOT NULL AND end < ?", (keep,))
        db.execute("DELETE FROM silence_log WHERE end IS NOT NULL AND end < ?", (keep,))
        db.execute("DELETE FROM logins WHERE ts < ?", (keep,))
    S.tx(w)
    S.meta_set("rollup_ts", upto)


# ---------------------------------------------------------------- forecast

def linear_trend(points):
    """Наклон (в единицах в сутки) и последнее значение по методу наименьших квадратов."""
    n = len(points)
    if n < 10:
        return None, None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    den = sum((p[0] - mx) ** 2 for p in points)
    if not den:
        return None, None
    slope = sum((p[0] - mx) * (p[1] - my) for p in points) / den
    return slope * 86400, points[-1][1]


FORECAST = {"ts": 0, "items": []}


def forecasts():
    """Когда заполнятся диски хостов и Ceph при текущем темпе роста (кэш на 10 минут)."""
    now = int(time.time())
    if now - FORECAST["ts"] < 600:
        return FORECAST["items"]
    items = []
    start = now - 14 * 86400
    pairs = [(h, m) for h, m in S.q("SELECT DISTINCT host, metric FROM roll WHERE ts > ? AND "
                                     "(metric LIKE 'disk:%' OR (host='_ceph' AND metric='used_pct'))",
                                     (now - 86400,))]
    for host, metric in pairs:
        pts = S.q("SELECT ts, v FROM roll WHERE host=? AND metric=? AND ts>? ORDER BY ts", (host, metric, start))
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < 12 * 3600:
            continue
        slope, last = linear_trend(pts)
        if slope is None:
            continue
        days = (100 - last) / slope if slope > 0.01 else None
        items.append({"host": host, "target": "Ceph" if host == "_ceph" else metric[5:], "pct": round(last, 1),
                      "per_day": round(slope, 3), "days": round(days, 1) if days and days < 3650 else None,
                      "span_h": round((pts[-1][0] - pts[0][0]) / 3600)})
    FORECAST.update(ts=now, items=items)
    return items


# ---------------------------------------------------------------- telegram

# ---------------------------------------------------------------- anomalies

# метрика -> (подпись, минимальная разница с обычным уровнем, формат)
ANOMALY_METRICS = {
    "cpu": ("CPU", 15, lambda v: f"{v:.0f}%"),
    "iowait": ("iowait", 10, lambda v: f"{v:.0f}%"),
    "disk_r": ("чтение диска", 4e6, lambda v: fmt_bytes(v) + "/с"),
    "disk_w": ("запись на диск", 4e6, lambda v: fmt_bytes(v) + "/с"),
    "net_rx": ("входящий трафик", 4e6, lambda v: f"{v * 8 / 1e6:.0f} Мбит/с"),
    "net_tx": ("исходящий трафик", 4e6, lambda v: f"{v * 8 / 1e6:.0f} Мбит/с"),
}
ANOMALY = {"ts": 0, "items": []}


def anomalies(factor):
    """Метрики хостов, которые последние 15 минут в factor раз выше обычного для этого часа
    (среднее за тот же час ±1 ч в прошлые дни, нужно хотя бы 2 дня истории). Кэш на 5 минут."""
    now = int(time.time())
    if not factor:
        return []
    if now - ANOMALY["ts"] < 300:
        return ANOMALY["items"]
    items = []
    names = ",".join(f"'{m}'" for m in ANOMALY_METRICS)
    cur = {(h, m): v for h, m, v in S.q(f"SELECT host, metric, AVG(v) FROM raw WHERE ts > ? AND metric IN ({names}) "
                                         f"AND host NOT LIKE '\\_%' ESCAPE '\\' GROUP BY host, metric", (now - 900,))}
    tz = time.localtime(now).tm_gmtoff
    hour = (now + tz) % 86400 // 3600
    hours = {(hour + d) % 24 for d in (-1, 0, 1)}
    base = {}
    for h, m, ts, v in S.q(f"SELECT host, metric, ts, v FROM roll WHERE ts > ? AND ts < ? AND metric IN ({names})",
                           (now - 8 * 86400, now - 6 * 3600)):
        if (ts + tz) % 86400 // 3600 in hours:
            b = base.setdefault((h, m), {"sum": 0.0, "n": 0, "days": set()})
            b["sum"] += v
            b["n"] += 1
            b["days"].add((ts + tz) // 86400)
    for (h, m), v in cur.items():
        b = base.get((h, m))
        if not b or len(b["days"]) < 2 or v is None:
            continue
        avg = b["sum"] / b["n"]
        label, floor, f = ANOMALY_METRICS[m]
        if v > factor * avg and v - avg > floor:
            items.append({"host": h, "metric": m, "value": v, "usual": avg,
                          "msg": f"{h}: {label} намного выше обычного — {f(v)}, обычно в это время {f(avg)}"})
    ANOMALY.update(ts=now, items=items)
    return items


def uptime(days):
    """Доступность каждой проверки сервиса по дням (доля успешных проверок)."""
    now = int(time.time())
    tz = time.localtime(now).tm_gmtoff
    start = now - days * 86400
    start -= (start + tz) % 86400  # с начала суток
    out = {}
    for name, day, avg, n in S.q("SELECT metric, (ts + ?) / 86400, AVG(v), COUNT(*) FROM roll WHERE host='_chk_ok' "
                                 "AND ts >= ? GROUP BY metric, (ts + ?) / 86400", (tz, start, tz)):
        out.setdefault(name, {})[day] = (avg, n)
    today = (now + tz) // 86400
    res = {}
    for name, per in out.items():
        tot = sum(a * n for a, n in per.values()) / max(1, sum(n for _, n in per.values()))
        res[name] = {"total": round(100 * tot, 3),
                     "days": [[(d * 86400 - tz), round(100 * per[d][0], 2) if d in per else None]
                              for d in range(today - days + 1, today + 1)]}
    return res


# ---------------------------------------------------------------- telegram

def tg_env():
    env = {}
    try:
        with open(TG_ENV_PATH) as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v
    except OSError:
        pass
    return env


def tg_ctx():
    # корень USIAG установлен в системе, но в нём нет Authority Key Identifier (см. tg-notify.py)
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def tg_call(token, method, params, timeout=40):
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data,
                                timeout=timeout, context=tg_ctx()) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- alerts

class Alerts:
    """Состояние алертов: key -> {level, msg, since, log_id}. Пороговые правила срабатывают,
    только если нарушение держится дольше заданного времени."""

    def __init__(self):
        self.active = S.meta_get("alerts", {})
        self.pending = {}  # key -> время первого нарушения
        self.silences = S.meta_get("silences", [])
        self.lock = threading.Lock()

    # ---- режим обслуживания
    @staticmethod
    def scope_matches(scope, key):
        if scope == "all":
            return True
        kind, _, rest = key.partition(":")
        if scope.startswith("host:"):
            return kind in ("down", "cpu", "mem", "temp", "disk", "node", "ssh", "fc", "net", "logs", "smart",
                            "iowait", "anom", "agent", "clock") and \
                rest.split(":")[0] == scope[5:]
        kinds = {"ceph": ("ceph",), "patroni": ("pg", "etcd", "etcdlat", "pgdb"), "checks": ("chk", "cert"),
                 "backups": ("bk", "pbs"), "ssh": ("ssh",)}
        return kind in kinds.get(scope, ())

    def silenced(self, key):
        now = time.time()
        return any(s["until"] > now and self.scope_matches(s["scope"], key) for s in self.silences)

    def add_silence(self, scope, minutes, reason, who):
        now = time.time()
        sil = {"id": f"{int(now * 1000):x}", "scope": scope, "since": now, "until": now + minutes * 60,
               "reason": reason[:200], "by": who}
        with self.lock:
            self.silences.append(sil)
            S.meta_set("silences", self.silences)
        S.q("INSERT INTO silence_log VALUES(?,?,?,?,?,NULL)", (sil["id"], scope, sil["reason"], who, now))
        text = f"Обслуживание: {scope_label(scope)} на {fmt_minutes(minutes)}" + (f" — {reason}" if reason else "")
        event("info", text)
        self.notify("🔕 " + text)
        return sil

    def remove_silence(self, sid, why="снято вручную"):
        with self.lock:
            hit = [s for s in self.silences if s["id"] == sid]
            self.silences = [s for s in self.silences if s["id"] != sid]
            S.meta_set("silences", self.silences)
        for s in hit:
            S.q("UPDATE silence_log SET end=? WHERE id=?", (min(time.time(), s["until"]), sid))
            text = f"Обслуживание закончено: {scope_label(s['scope'])} ({why})"
            event("info", text)
            self.notify("🔔 " + text)
        return bool(hit)

    def notify(self, text):
        cmd = CFG.get("notify_cmd")
        if not cmd:
            return

        def send():
            try:
                r = subprocess.run([cmd, text], capture_output=True, text=True, timeout=30)
                if r.returncode:
                    print(f"notify failed: {r.stderr.strip()[-300:]}", file=sys.stderr, flush=True)
            except Exception as e:
                print(f"notify failed: {e!r}", file=sys.stderr, flush=True)
        threading.Thread(target=send, daemon=True).start()

    def evaluate(self):
        now = time.time()
        r = rules()
        found = {}  # key -> (level, msg, hold_sec)

        for name, last_ts, latest in S.q("SELECT name, last_ts, latest FROM hosts"):
            if now - last_ts > r["host_down_sec"]:
                found[f"down:{name}"] = ("crit", f"{name}: нет данных {int(now - last_ts)} с", 0)
                continue
            lt = json.loads(latest)
            m = lt.get("metrics") or {}
            if m.get("cpu", 0) > r["cpu_pct"]:
                found[f"cpu:{name}"] = ("warn", f"{name}: CPU {m['cpu']:.0f}%", r["cpu_for_sec"])
            if m.get("mem", 0) > r["mem_pct"]:
                found[f"mem:{name}"] = ("warn", f"{name}: RAM {m['mem']:.0f}%", r["mem_for_sec"])
            if m.get("temp", 0) > r["temp_c"]:
                found[f"temp:{name}"] = ("warn", f"{name}: температура {m['temp']:.0f}°C", 60)
            if m.get("net_util", 0) > r["net_util_pct"]:
                found[f"net:{name}"] = ("warn", f"{name}: сеть загружена на {m['net_util']:.0f}% "
                                                f"(канал {m.get('link_mbit', '?')} Мбит/с)", r["net_for_sec"])
            if m.get("iowait", 0) > r["iowait_pct"]:
                found[f"iowait:{name}"] = ("warn", f"{name}: ожидание диска (iowait) {m['iowait']:.0f}%",
                                           r["iowait_for_sec"])
            if m.get("agent_cpu", 0) > r["agent_cpu_pct"] or m.get("agent_io", 0) > r["agent_io_kbs"] * 1024:
                found[f"agent:{name}"] = ("warn", f"{name}: агент Lumen тратит CPU {m.get('agent_cpu', 0):.0f}%, "
                                                  f"диск {fmt_bytes(m.get('agent_io') or 0)}/с", 600)
            ck = lt.get("clock") or {}
            if ck.get("sync") is False:
                found[f"clock:{name}:sync"] = ("warn", f"{name}: время не синхронизировано (NTP)", 600)
            skew = max(abs(ck.get("skew_ms") or 0), abs(ck.get("offset_ms") or 0))
            if skew > r["clock_skew_ms"]:
                found[f"clock:{name}"] = ("warn", f"{name}: часы расходятся на {skew:.0f} мс", 120)
            if m.get("log_err", 0) > r["log_err_per_min"]:
                found[f"logs:{name}"] = ("warn", f"{name}: {m['log_err']:.0f} ошибок в журнале в минуту",
                                         r["log_for_sec"])
            for d in lt.get("disks") or []:
                pct = 100.0 * d["used"] / d["size"] if d.get("size") else 0
                if pct > r["disk_warn_pct"]:
                    lvl = "crit" if pct > r["disk_crit_pct"] else "warn"
                    found[f"disk:{name}:{d['mount']}"] = (lvl, f"{name}: диск {d['mount']} {pct:.0f}%", 0)

        for a in anomalies(r["anomaly_factor"]):
            found[f"anom:{a['host']}:{a['metric']}"] = ("warn", a["msg"], 0)

        for f in forecasts():
            if f["days"] is not None and f["days"] < r["forecast_days"]:
                key = "ceph:forecast" if f["host"] == "_ceph" else f"fc:{f['host']}:{f['target']}"
                where = "Ceph" if f["host"] == "_ceph" else f"{f['host']}: диск {f['target']}"
                found[key] = ("warn", f"{where} заполнится примерно через {f['days']:.0f} дн "
                                      f"(сейчас {f['pct']:.0f}%, +{f['per_day']:.2f}% в сутки)", 0)

        cl = {k: (ts, json.loads(d)) for k, ts, d in S.q("SELECT kind, ts, data FROM cluster")}

        if "ceph" in cl:
            ts, c = cl["ceph"]
            if now - ts > r["cluster_stale_sec"]:
                found["ceph:stale"] = ("warn", "Ceph: статус не обновлялся", 0)
            else:
                if c.get("health") != "HEALTH_OK":
                    lvl = "crit" if c.get("health") == "HEALTH_ERR" else "warn"
                    checks = "; ".join(x["msg"] for x in c.get("checks", []))[:300]
                    found["ceph:health"] = (lvl, f"Ceph {c.get('health')}: {checks}", 60)
                lat = c.get("osd_lat") or {}
                if lat:
                    worst = max(lat, key=lat.get)
                    if lat[worst] > r["osd_latency_ms"]:
                        found["ceph:lat"] = ("warn", f"Ceph: задержка записи {worst} {lat[worst]} мс", 120)

        if "patroni" in cl:
            ts, pt = cl["patroni"]
            if now - ts > r["cluster_stale_sec"]:
                found["pg:stale"] = ("warn", "Patroni: статус не обновлялся", 0)
            elif pt.get("error"):
                found["pg:api"] = ("crit", f"Patroni API недоступен: {pt['error']}", 30)
            else:
                members = pt.get("members", [])
                leaders = [m["name"] for m in members if m.get("role") in ("leader", "standby_leader")]
                if not leaders:
                    found["pg:noleader"] = ("crit", "Patroni: нет лидера", 0)
                for m in members:
                    if m.get("state") not in ("running", "streaming"):
                        found[f"pg:state:{m['name']}"] = ("crit", f"Patroni {m['name']}: {m.get('state')}", 30)
                    lag = m.get("lag")
                    if isinstance(lag, (int, float)) and lag > r["patroni_lag_bytes"]:
                        found[f"pg:lag:{m['name']}"] = ("warn", f"Patroni {m['name']}: лаг {lag // 1048576} МБ", 60)
                prev = S.meta_get("pg_leader")
                if leaders and prev and leaders[0] != prev:
                    event("warn", f"Patroni: смена лидера {prev} → {leaders[0]}")
                    self.notify(f"🔁 Patroni: смена лидера {prev} → {leaders[0]}")
                if leaders:
                    S.meta_set("pg_leader", leaders[0])
            lc_prev = S.meta_get("etcd_lc", {})
            for ep, st in (pt.get("etcd") or {}).items():
                if not st.get("ok"):
                    found[f"etcd:{ep}"] = ("crit", f"etcd {ep}: нездоров ({st.get('err', '')})", 30)
                if (st.get("fsync_ms") or 0) > r["etcd_fsync_ms"]:
                    found[f"etcdlat:{ep}"] = ("warn", f"etcd {ep}: fsync {st['fsync_ms']:.0f} мс", 60)
                lc = st.get("leader_changes")
                if lc is not None:
                    if ep in lc_prev and lc > lc_prev[ep]:
                        event("warn", f"etcd {ep}: смена лидера etcd (всего {lc})")
                    lc_prev[ep] = lc
            S.meta_set("etcd_lc", lc_prev)

        for kind, (ts, data) in cl.items():
            if kind.startswith("checks:") and now - ts <= r["cluster_stale_sec"]:
                src = kind[7:]
                for c in data:
                    if not c.get("ok"):
                        found[f"chk:{c['name']}"] = ("crit", f"Проверка «{c['name']}» не прошла: "
                                                             f"{c.get('err') or 'ошибка'} ({c['target']}, с {src})",
                                                     r["check_for_sec"])
                    cd = c.get("cert_days")
                    if c.get("ok") and cd is not None and cd < r["cert_days"]:
                        found[f"cert:{c['name']}"] = ("crit" if cd < 3 else "warn",
                                                      f"Сертификат «{c['name']}» истекает через {max(0, cd):.0f} дн "
                                                      f"({c['target']})", 0)
            if kind.startswith("smart:") and now - ts <= 3 * 3600:
                self.smart(kind[6:], data, r, found)
            if kind.startswith("postgres:") and now - ts <= r["cluster_stale_sec"]:
                h = kind[9:]
                if data.get("error"):
                    found[f"pgdb:{h}:err"] = ("warn", f"PostgreSQL на {h}: нет статистики ({data['error']})", 120)
                    continue
                if data.get("max_conn") and data["conn_total"] > data["max_conn"] * r["pg_conn_pct"] / 100:
                    found[f"pgdb:{h}:conn"] = ("warn", f"PostgreSQL на {h}: {data['conn_total']} подключений "
                                                       f"из {data['max_conn']}", 60)
                if (data.get("oldest_xact_sec") or 0) > r["pg_xact_age_sec"]:
                    found[f"pgdb:{h}:xact"] = ("warn", f"PostgreSQL на {h}: транзакция идёт "
                                                       f"{fmt_minutes(int(data['oldest_xact_sec'] // 60))}", 0)

        if "pve" in cl:
            ts, pv = cl["pve"]
            for n in pv.get("nodes", []):
                if n.get("status") != "online":
                    found[f"node:{n['name']}"] = ("crit", f"Узел {n['name']}: {n.get('status')}", 0)
            prev = S.meta_get("guest_status", {})
            cur = {str(g["vmid"]): g["status"] for g in pv.get("guests", []) if not g.get("template")}
            for vmid, st in cur.items():
                if vmid in prev and prev[vmid] != st:
                    name = next((g["name"] for g in pv["guests"] if str(g["vmid"]) == vmid), vmid)
                    event("info", f"{vmid} {name}: {prev[vmid]} → {st}")
            S.meta_set("guest_status", cur)

        if "pbs" in cl and now - cl["pbs"][0] <= 3 * 3600:
            for st in cl["pbs"][1].get("stores", []):
                n = st["name"]
                gc = st.get("gc") or {}
                if gc.get("state") and gc["state"] != "OK":
                    found[f"pbs:{n}:gc"] = ("warn", f"PBS {n}: очистка (GC) завершилась: {gc['state']}", 0)
                elif gc.get("last") and now - gc["last"] > r["pbs_gc_days"] * 86400:
                    found[f"pbs:{n}:gc"] = ("warn", f"PBS {n}: очистка (GC) не проходила {fmt_dur(now - gc['last'])}", 0)
                for kind, label, limit in (("verify", "проверка (verify)", r["pbs_verify_days"]), ("prune", "прореживание", 3)):
                    t = st.get(kind)
                    if t and t.get("status") not in (None, "OK"):
                        found[f"pbs:{n}:{kind}"] = ("warn", f"PBS {n}: {label} завершилось: {t['status']}", 0)
                    elif t and now - t["end"] > limit * 86400:
                        found[f"pbs:{n}:{kind}"] = ("warn", f"PBS {n}: {label} не проходила {fmt_dur(now - t['end'])}", 0)

        if "backups" in cl:
            ts, bk = cl["backups"]
            seen = set(S.meta_get("bk_failed_seen", []))
            for t in bk.get("tasks", []):
                # список задач кластера хранит и старые записи — сообщаем только о свежих
                if t.get("status") in (None, "OK") or t["upid"] in seen \
                        or now - (t.get("endtime") or t["starttime"]) > 86400:
                    continue
                seen.add(t["upid"])
                when = time.strftime("%d.%m %H:%M", time.localtime(t["starttime"]))
                event("crit", f"Бэкап на {t['node']} ({when}) завершился: {t['status']}")
                self.notify(f"❌ Бэкап на {t['node']} ({when}) завершился с ошибкой: {t['status']}")
            S.meta_set("bk_failed_seen", sorted(seen)[-200:])
            max_age = r["backup_max_age_h"] * 3600
            for g in bk.get("guests", []):
                if g.get("last") and now - g["last"] > max_age:
                    found[f"bk:{g['vmid']}"] = ("warn", f"Бэкап {g['vmid']} {g.get('name', '')}: "
                                                        f"последний {int((now - g['last']) / 3600)} ч назад", 0)

        # напоминания о долгих алертах считаются ниже, при отправке
        # учёт времени удержания
        fired = {}
        for key, (lvl, msg, hold) in found.items():
            first = self.pending.setdefault(key, now)
            if now - first >= hold or key in self.active:
                fired[key] = (lvl, msg)
        for key in list(self.pending):
            if key not in found:
                del self.pending[key]

        # истёкшие окна обслуживания
        for sil in [x for x in self.silences if x["until"] <= now]:
            self.remove_silence(sil["id"], "время вышло")

        icon = lambda lvl: "🔴 " if lvl == "crit" else "🟡 "
        for key, (lvl, msg) in fired.items():
            muted = self.silenced(key)
            old = self.active.get(key)
            if not old:
                cur = S.q("INSERT INTO alert_log(key, level, msg, start, end, muted) VALUES(?,?,?,?,NULL,?) "
                          "RETURNING id", (key, lvl, msg, now, int(muted)))
                self.active[key] = {"level": lvl, "msg": msg, "since": now, "notified": not muted,
                                    "log_id": cur[0][0]}
                event(lvl, msg + (" (обслуживание)" if muted else ""))
                if not muted:
                    self.notify(icon(lvl) + msg)
            else:
                if old["level"] != lvl:
                    event(lvl, msg)
                    if lvl == "crit" and old.get("log_id"):
                        S.q("UPDATE alert_log SET level='crit' WHERE id=?", (old["log_id"],))
                    if not muted:
                        self.notify(icon(lvl) + msg)
                        old["notified"] = True
                elif not muted and not old.get("notified", True):
                    # обслуживание закончилось, а проблема осталась
                    self.notify(icon(lvl) + msg + " (после обслуживания)")
                    old["notified"] = True
                    old["reminded"] = now
                elif not muted and r["remind_h"] and now - old.get("reminded", old["since"]) >= r["remind_h"] * 3600:
                    self.notify(f"⏰ Всё ещё: {msg} (длится {fmt_dur(now - old['since'])})")
                    old["reminded"] = now
                old.update(level=lvl, msg=msg)
        for key in list(self.active):
            if key not in fired:
                a = self.active.pop(key)
                if a.get("log_id"):
                    S.q("UPDATE alert_log SET end=?, msg=? WHERE id=?", (now, a["msg"], a["log_id"]))
                dur = int((now - a["since"]) / 60)
                event("ok", f"Решено: {a['msg']} ({dur} мин)")
                if a.get("notified", True) and not self.silenced(key):
                    self.notify(f"✅ Решено: {a['msg']} (длилось {dur} мин)")
        S.meta_set("alerts", self.active)


    def smart(self, host, data, r, found):
        """Алерты по SMART одного узла и мгновенное сообщение, если вырос счётчик ошибок диска."""
        base = S.meta_get("smart_base", {})
        for d in data.get("disks", []):
            if d.get("error"):
                continue
            dk = f"{host}:{d['dev']}"
            label = f"{host}: диск {d['dev']} ({d.get('model') or '?'})"
            if d.get("passed") is False:
                found[f"smart:{dk}:fail"] = ("crit", f"{label}: SMART-тест не пройден", 0)
            bad = [f"{d[c]} {SMART_NAMES[c]}" for c in ("pending", "uncorrectable") if d.get(c)]
            if bad:
                found[f"smart:{dk}:sect"] = ("warn", f"{label}: " + ", ".join(bad), 0)
            if (d.get("wear") or 0) >= r["smart_wear_pct"]:
                found[f"smart:{dk}:wear"] = ("warn", f"{label}: износ {d['wear']}%", 0)
            if (d.get("temp") or 0) > r["smart_temp_c"]:
                found[f"smart:{dk}:temp"] = ("warn", f"{label}: температура {d['temp']}°C", 300)
            sid = d.get("serial") or dk
            prev = base.get(sid, {})
            grown = [f"{SMART_NAMES[c]}: {prev[c]} → {d[c]}" for c in SMART_NAMES
                     if isinstance(d.get(c), int) and isinstance(prev.get(c), int) and d[c] > prev[c]]
            if grown:
                text = f"{label}: выросли счётчики SMART — " + "; ".join(grown)
                event("warn", text)
                if not self.silenced(f"smart:{dk}"):
                    self.notify("💽 " + text)
            base[sid] = {c: d.get(c) for c in SMART_NAMES}
        S.meta_set("smart_base", base)


def scope_label(scope):
    return {"all": "весь кластер", "ceph": "Ceph", "patroni": "PostgreSQL/etcd", "checks": "проверки сервисов",
            "backups": "бэкапы", "ssh": "входы по SSH"}.get(scope, scope.replace("host:", "хост "))


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


ALERTS = Alerts()


# ---------------------------------------------------------------- reports

def status_text():
    """Короткая сводка текущего состояния для /status и утреннего отчёта."""
    d = overview()
    lines = []
    live = [a for a in d["alerts"] if not a["muted"]]
    lines.append("✅ Всё в порядке" if not live else
                 f"⚠️ Проблем: {len(live)}\n" + "\n".join(("🔴 " if a["level"] == "crit" else "🟡 ") + a["msg"]
                                                       for a in live[:10]))
    hosts = d["hosts"]
    up = [h for h in hosts if h["age"] <= rules()["host_down_sec"]]
    lines.append(f"\nХосты: {len(up)}/{len(hosts)} в сети")
    for h in up:
        m = h.get("metrics") or {}
        disk = max([100 * x["used"] / x["size"] for x in h.get("disks") or [] if x.get("size")] or [0])
        lines.append(f"  {h['name']}: CPU {m.get('cpu', 0):.0f}%, RAM {m.get('mem', 0):.0f}%, диск {disk:.0f}%")
    c = d["cluster"]
    if "ceph" in c:
        ce = c["ceph"]
        lines.append(f"Ceph: {ce['health']}, занято {100 * ce['bytes']['used'] / ce['bytes']['total']:.0f}%, "
                     f"OSD {ce['osd']['up']}/{ce['osd']['total']}")
    if "patroni" in c:
        pt = c["patroni"]
        leader = next((m["name"] for m in pt.get("members", []) if m.get("role") == "leader"), None)
        lines.append(f"PostgreSQL: лидер {leader or 'нет'}" if not pt.get("error") else f"PostgreSQL: {pt['error']}")
    if "pve" in c:
        g = [x for x in c["pve"]["guests"] if not x.get("template")]
        lines.append(f"VM и CT: {sum(x['status'] == 'running' for x in g)}/{len(g)} запущено")
    checks = [x for k, v in c.items() if k.startswith("checks:") for x in v.get("items", [])]
    if checks:
        bad = [x["name"] for x in checks if not x["ok"]]
        lines.append(f"Сервисы: {len(checks) - len(bad)}/{len(checks)}" + (f" (не отвечают: {', '.join(bad)})" if bad else ""))
    if d["silences"]:
        lines.append("Обслуживание: " + ", ".join(f"{s['label']} ещё {fmt_dur(s['until'] - d['now'])}"
                                                  for s in d["silences"]))
    return "\n".join(lines)


def morning_summary():
    now = time.time()
    day = now - 86400
    out = [f"☀️ Lumen: сводка за сутки ({time.strftime('%d.%m %H:%M')})", "", status_text(), ""]
    rows = S.q("SELECT key, level, msg, start, end FROM alert_log WHERE start > ? OR end IS NULL OR end > ? "
               "ORDER BY start", (day, day))
    if rows:
        crit = sum(r[1] == "crit" for r in rows)
        out.append(f"Алертов за сутки: {len(rows)} (критичных {crit})")
        for key, lvl, msg, start, end in rows[-8:]:
            dur = fmt_dur((end or now) - start)
            out.append(f"  {'🔴' if lvl == 'crit' else '🟡'} {msg} — {dur}{'' if end else ', ещё идёт'}")
    else:
        out.append("Алертов за сутки не было")
    c = {k: json.loads(v) for k, v in S.q("SELECT kind, data FROM cluster")}
    bk = c.get("backups")
    if bk:
        tasks = [t for t in bk.get("tasks", []) if t["starttime"] > day]
        bad = [t for t in tasks if t.get("status") not in (None, "OK")]
        stale = [g for g in bk.get("guests", []) if now - g["last"] > rules()["backup_max_age_h"] * 3600]
        out.append(f"Бэкапы: заданий {len(tasks)}, с ошибкой {len(bad)}; "
                   + (f"устарели: {', '.join(str(g['vmid']) for g in stale)}" if stale else "все гости свежие"))
    peaks = S.q("SELECT host, metric, MAX(v) FROM raw WHERE ts > ? AND metric IN ('cpu','mem') "
                "AND host NOT LIKE '\\_%' ESCAPE '\\' GROUP BY host, metric", (int(day),))
    if peaks:
        by = {}
        for h, m, v in peaks:
            by.setdefault(h, {})[m] = v
        out.append("Пики за сутки: " + "; ".join(f"{h} CPU {v.get('cpu', 0):.0f}% RAM {v.get('mem', 0):.0f}%"
                                                for h, v in sorted(by.items())))
    gaps, first = {}, {}
    for (h,) in S.q("SELECT name FROM hosts"):
        ts = [r[0] for r in S.q("SELECT ts FROM raw WHERE host=? AND metric='cpu' AND ts > ? ORDER BY ts", (h, int(day)))]
        if ts:
            first[h] = ts[0]
            gaps[h] = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > 600]
    span = lambda a, b: f"{time.strftime('%H:%M', time.localtime(a))}–{time.strftime('%H:%M', time.localtime(b))} ({fmt_dur(b - a)})"
    same = lambda g, v: any(abs(a - g[0]) < 120 and abs(b - g[1]) < 120 for a, b in v)
    # пробел у всех хостов, которые уже присылали данные: скорее всего не работал сам Lumen (или его узел)
    shared = [g for g in sorted({g for v in gaps.values() for g in v})
              if len(alive := [h for h in gaps if first[h] < g[0]]) >= 2 and all(same(g, gaps[h]) for h in alive)]
    shared = [g for i, g in enumerate(shared) if not same(g, shared[:i])]  # у хостов начало отличается на секунды
    if shared:
        out.append("⚠️ Lumen не получал данных: " + "; ".join(span(a, b) for a, b in shared[:3]))
    for h, v in sorted(gaps.items()):
        own = [g for g in v if not any(abs(g[0] - s[0]) < 120 for s in shared)]
        if own:
            out.append(f"Нет данных от {h}: " + "; ".join(span(a, b) for a, b in own[:3]))
    fsync = S.q("SELECT metric, MAX(v) FROM raw WHERE host='_etcd' AND metric LIKE 'fsync:%' AND ts > ? "
                "GROUP BY metric", (int(day),))
    if fsync:
        out.append("etcd fsync, максимум: " + ", ".join(f"{m[6:]} {v:.0f} мс" for m, v in fsync))
    fc = [f for f in forecasts() if f["days"] is not None]
    if fc:
        out.append("Прогноз заполнения: " + "; ".join(
            f"{'Ceph' if f['host'] == '_ceph' else f['host'] + ' ' + f['target']} через ~{f['days']:.0f} дн"
            for f in sorted(fc, key=lambda f: f["days"])[:4]))
    logins = S.q("SELECT host, user, ip, COUNT(*) FROM logins WHERE ts > ? AND known=0 GROUP BY host, user, ip", (day,))
    if logins:
        out.append("Входы по SSH с незнакомых адресов: " + "; ".join(f"{h}: {u} с {ip} ×{n}" for h, u, ip, n in logins))
    return "\n".join(out)


def weekly_report():
    now = time.time()
    week = now - 7 * 86400
    out = [f"📅 Lumen: итоги недели ({time.strftime('%d.%m', time.localtime(week))}–{time.strftime('%d.%m')})", ""]
    # 5-минутные корзины, где были данные хоть от кого-то, — время, когда работал сам Lumen;
    # доступность хоста считается только по этому времени, чтобы простой Lumen не записывался хостам
    done = S.meta_get("rollup_ts", int(now))
    buckets = {}
    for h, ts in S.q("SELECT host, ts FROM roll WHERE metric='cpu' AND ts > ? "
                     "AND host NOT LIKE '\\_%' ESCAPE '\\'", (int(week),)):
        buckets.setdefault(h, set()).add(ts)
    if buckets:
        up = set().union(*buckets.values())
        total = max(1, (done - min(up)) / ROLLUP_STEP)
        own = min(100.0, 100.0 * len(up) / total)
        hosts = []
        for h, b in sorted(buckets.items()):
            first = min(b)
            hosts.append(f"{h} {100.0 * len(b) / max(1, sum(1 for t in up if t >= first)):.1f}%")
        out.append(f"Lumen собирал данные {own:.1f}% времени" + (" — были перерывы" if own < 99.5 else ""))
        out.append("Доступность хостов: " + ", ".join(hosts))
    hist = alert_history(7)
    if hist["summary"]:
        n = sum(a["count"] for a in hist["summary"])
        crit = sum(1 for a in hist["items"] if a["level"] == "crit")
        out.append(f"Алертов за неделю: {n} (критичных {crit}), чаще всего:")
        for a in hist["summary"][:5]:
            out.append(f"  {'🔴' if a['level'] == 'crit' else '🟡'} {a['msg']} — ×{a['count']}, всего {fmt_dur(a['total'])}")
    else:
        out.append("Алертов за неделю не было 🎉")
    growth = []
    for h, m, v0, v1 in S.q("""SELECT host, metric,
            (SELECT v FROM roll r2 WHERE r2.host=r.host AND r2.metric=r.metric AND ts > ? ORDER BY ts LIMIT 1),
            (SELECT v FROM roll r2 WHERE r2.host=r.host AND r2.metric=r.metric ORDER BY ts DESC LIMIT 1)
            FROM roll r WHERE ts > ? AND (metric LIKE 'disk:%' OR (host='_ceph' AND metric='used_pct'))
            GROUP BY host, metric""", (int(week), int(week))):
        if v0 is not None and v1 is not None and abs(v1 - v0) >= 0.5:
            where = "Ceph" if h == "_ceph" else f"{h} {m[5:]}"
            growth.append(f"{where} {v0:.0f}→{v1:.0f}% ({v1 - v0:+.1f})")
    out.append("Заполнение дисков: " + ("; ".join(growth) if growth else "заметно не изменилось"))
    peaks = S.q("SELECT host, MAX(vmax) FROM roll WHERE metric='net_util' AND ts > ? GROUP BY host ORDER BY host",
                (int(week),))
    if peaks:
        out.append("Пик загрузки сети: " + ", ".join(f"{h} {v:.0f}%" for h, v in peaks))
    errs = [(h, v * ROLLUP_STEP / 60) for h, v in S.q("SELECT host, SUM(v) FROM roll WHERE metric='log_err' AND ts > ? "
                                                        "GROUP BY host ORDER BY 2 DESC", (int(week),)) if v]
    if errs:
        out.append("Ошибок в журналах (примерно): " + ", ".join(f"{h} {n:.0f}" for h, n in errs[:6] if n >= 1))
    c = {k: json.loads(v) for k, v in S.q("SELECT kind, data FROM cluster WHERE kind='backups'")}
    if "backups" in c:
        tasks = [t for t in c["backups"].get("tasks", []) if t["starttime"] > week]
        bad = [t for t in tasks if t.get("status") not in (None, "OK")]
        out.append(f"Бэкапы: заданий {len(tasks)}, с ошибкой {len(bad)}")
    upd = [(k[8:], json.loads(v)) for k, v in S.q("SELECT kind, data FROM cluster WHERE kind LIKE 'updates:%' "
                                                     "ORDER BY kind")]
    if upd:
        need = [f"{h} {u['count']}" + (f" (безопасность {u['security']})" if u.get("security") else "")
                for h, u in upd if u.get("count")]
        boot = [h for h, u in upd if u.get("reboot")]
        out.append("Обновления: " + (", ".join(need) if need else "всё обновлено")
                   + (f"; ждут перезагрузки: {', '.join(boot)}" if boot else ""))
    pb = S.q("SELECT data FROM cluster WHERE kind='pbs'")
    if pb:
        for st in json.loads(pb[0][0]).get("stores", []):
            out.append(f"PBS {st['name']}: занято {100 * st['used'] / st['size']:.0f}% ({fmt_bytes(st['used'])} "
                       f"из {fmt_bytes(st['size'])}), GC {(st.get('gc') or {}).get('state') or '—'}, "
                       f"verify {(st.get('verify') or {}).get('status') or 'не было'}")
    ut = uptime(7)
    bad = [(n, u["total"]) for n, u in ut.items() if u["total"] < 99.9]
    if ut:
        out.append("Сервисы: " + ("все доступны ≥ 99.9%" if not bad else
                                  "ниже 99.9%: " + ", ".join(f"{n} {p:.2f}%" for n, p in sorted(bad, key=lambda x: x[1]))))
    (unk,), = S.q("SELECT COUNT(*) FROM logins WHERE ts > ? AND known=0", (week,))
    (tot,), = S.q("SELECT COUNT(*) FROM logins WHERE ts > ?", (week,))
    out.append(f"Входы по SSH: {tot}" + (f", с незнакомых адресов {unk}" if unk else ", все с известных адресов"))
    fc = [f for f in forecasts() if f["days"] is not None]
    if fc:
        f = min(fc, key=lambda f: f["days"])
        out.append(f"Ближе всего к заполнению: {'Ceph' if f['host'] == '_ceph' else f['host'] + ' ' + f['target']} "
                   f"— примерно через {f['days']:.0f} дн")
    return "\n".join(out)


def maybe_send_summary():
    st = settings()
    if not st.get("summary_time"):
        return
    today = time.strftime("%Y-%m-%d")
    if time.strftime("%H:%M") >= st["summary_time"] and S.meta_get("summary_date") != today:
        S.meta_set("summary_date", today)
        ALERTS.notify(morning_summary())
    # по понедельникам — ещё и итоги недели
    if st.get("weekly_report") and time.localtime().tm_wday == 0 and time.strftime("%H:%M") >= st["summary_time"] \
            and S.meta_get("weekly_date") != today:
        S.meta_set("weekly_date", today)
        ALERTS.notify(weekly_report())


# ---------------------------------------------------------------- telegram bot

HELP = """Команды Lumen:
/status — состояние кластера
/alerts — активные алерты
/mute <что> <на сколько> [причина] — обслуживание. Что: all, ceph, patroni, checks, backups, ssh или имя хоста. Время: 30m, 2h, 1d
/unmute [all] — снять обслуживание
/summary — сводка за сутки
/week — итоги недели"""


def parse_minutes(s):
    m = re.fullmatch(r"(\d+)\s*(m|min|мин|h|ч|d|д)?", s.lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2) or "m"
    return n * (1440 if unit in ("d", "д") else 60 if unit in ("h", "ч") else 1)


def bot_command(text, who):
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return status_text()
    if cmd == "/summary":
        return morning_summary()
    if cmd == "/week":
        return weekly_report()
    if cmd == "/alerts":
        act = sorted(ALERTS.active.items(), key=lambda kv: kv[1]["since"])
        if not act:
            return "Активных алертов нет"
        now = time.time()
        return "\n".join(f"{'🔕 ' if ALERTS.silenced(k) else ''}{'🔴' if a['level'] == 'crit' else '🟡'} {a['msg']} "
                         f"— {fmt_dur(now - a['since'])}" for k, a in act)
    if cmd == "/mute":
        if len(parts) < 3:
            return "Пример: /mute pve2 1h обновление ядра"
        target, minutes = parts[1], parse_minutes(parts[2])
        hosts = {r[0] for r in S.q("SELECT name FROM hosts")}
        scope = target if target in SILENCE_SCOPES else f"host:{target}" if target in hosts else None
        if not scope:
            return f"Не знаю «{target}». Можно: {', '.join(SILENCE_SCOPES)} или хост: {', '.join(sorted(hosts))}"
        if not minutes or not 1 <= minutes <= 7 * 1440:
            return "Время: например 30m, 2h, 1d (не больше 7 дней)"
        ALERTS.add_silence(scope, minutes, " ".join(parts[3:]), f"telegram:{who}")
        return f"Готово: {scope_label(scope)} на {fmt_minutes(minutes)}"
    if cmd == "/unmute":
        act = [s for s in ALERTS.silences if s["until"] > time.time()]
        if not act:
            return "Обслуживание не включено"
        if len(act) > 1 and (len(parts) < 2 or parts[1] != "all"):
            return "Включено несколько окон:\n" + "\n".join(f"  {s['label'] if 'label' in s else scope_label(s['scope'])}"
                                                            for s in act) + "\nСнять все: /unmute all"
        for s in act:
            ALERTS.remove_silence(s["id"], f"снято из Telegram")
        return "Обслуживание снято"
    return "Не понял команду. /help — список"


def bot_loop():
    """Принимает команды через отдельного бота (LUMEN_BOT_TOKEN) только из разрешённых чатов."""
    offset = None
    while True:
        env = tg_env()
        token = env.get("LUMEN_BOT_TOKEN")
        allowed = {c.strip() for c in env.get("LUMEN_BOT_CHATS", env.get("TELEGRAM_CHAT", "")).split(",") if c.strip()}
        if not token or not allowed:
            time.sleep(60)
            continue
        try:
            params = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
            if offset:
                params["offset"] = offset
            res = tg_call(token, "getUpdates", params, timeout=45)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id"))
                text = msg.get("text") or ""
                if chat not in allowed or not text.startswith("/"):
                    continue
                who = (msg.get("from") or {}).get("username") or chat
                try:
                    reply = bot_command(text, who)
                except Exception as e:
                    reply = f"Ошибка: {e}"
                for i in range(0, len(reply), 4000):
                    tg_call(token, "sendMessage", {"chat_id": chat, "text": reply[i:i + 4000]}, timeout=20)
        except Exception as e:
            print("bot error:", repr(e)[:300], file=sys.stderr, flush=True)
            time.sleep(15)


def background():
    tick = 0
    while True:
        time.sleep(15)
        tick += 1
        try:
            ALERTS.evaluate()
            if tick % 20 == 1:
                rollup_and_prune()
            if tick % 4 == 0:
                maybe_send_summary()
        except Exception as e:  # фоновый цикл не должен падать
            print("background error:", repr(e), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- auth

def hash_password(pw, salt=None, iters=310000):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), iters)
    return f"pbkdf2_sha256${iters}${salt}${dk.hex()}"


def check_password(pw, stored):
    try:
        _, iters, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(pw, salt, int(iters)), stored)
    except ValueError:
        return False


class Sessions:
    """Сессии хранятся как sha256 от токена, чтобы утечка базы не давала готовых cookie."""

    def __init__(self):
        self.data = S.meta_get("sessions", {})
        self.fails = {}  # ip -> [время неудачных попыток]
        self.lock = threading.Lock()

    @staticmethod
    def _h(tok):
        return hashlib.sha256(tok.encode()).hexdigest()

    def create(self, user, ip):
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.data[self._h(tok)] = {"user": user, "ip": ip, "created": time.time(),
                                       "exp": time.time() + CFG["session_days"] * 86400}
            self.data = {k: v for k, v in self.data.items() if v["exp"] > time.time()}
            S.meta_set("sessions", self.data)
        return tok

    def user(self, tok):
        s = self.data.get(self._h(tok)) if tok else None
        return s["user"] if s and s["exp"] > time.time() else None

    def drop(self, tok):
        with self.lock:
            self.data.pop(self._h(tok), None)
            S.meta_set("sessions", self.data)

    def blocked(self, ip):
        now = time.time()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < LOGIN_LIMIT[1]]
        return len(self.fails[ip]) >= LOGIN_LIMIT[0]

    def failed(self, ip):
        self.fails.setdefault(ip, []).append(time.time())


SESSIONS = Sessions()


def users():
    # пароль меняется командой passwd без перезапуска сервера
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("users") or {}
    except (OSError, ValueError):
        return CFG.get("users") or {}


# ---------------------------------------------------------------- API

def series(host, metric, start, end, points=240):
    span = max(1, end - start)
    step = max(15, span // points)
    table = "raw" if start >= time.time() - CFG["raw_keep_hours"] * 3600 + 600 else "roll"
    if table == "roll":
        step = max(step, ROLLUP_STEP)
    rows = S.q(f"""SELECT ts - ts % {step} AS b, AVG(v) FROM {table}
                   WHERE host=? AND metric=? AND ts>=? AND ts<=? GROUP BY b ORDER BY b""",
               (host, metric, start, end))
    return {"step": step, "points": [[b, round(v, 3)] for b, v in rows]}


def overview():
    now = time.time()
    hosts = []
    for name, last_ts, latest in S.q("SELECT name, last_ts, latest FROM hosts ORDER BY name"):
        lt = json.loads(latest)
        lt["name"] = name
        lt["age"] = now - last_ts
        lt["spark"] = {m: series(name, m, int(now - 3600), int(now), 60)["points"] for m in ("cpu", "mem")}
        hosts.append(lt)
    cluster = {}
    for k, ts, src, d in S.q("SELECT kind, ts, src, data FROM cluster"):
        data = json.loads(d)
        cluster[k] = dict(data if isinstance(data, dict) else {"items": data}, _age=now - ts, _src=src)
    events = [{"ts": t, "level": l, "msg": m}
              for t, l, m in S.q("SELECT ts, level, msg FROM events ORDER BY ts DESC LIMIT 40")]
    alerts = sorted([dict({k: v for k, v in a.items() if k != "log_id"}, key=k, muted=ALERTS.silenced(k))
                     for k, a in ALERTS.active.items()],
                    key=lambda a: (a["muted"], a["level"] != "crit", a["since"]))
    silences = [dict(x, label=scope_label(x["scope"])) for x in ALERTS.silences if x["until"] > now]
    logins = [{"host": h, "ts": t, "user": u, "ip": ip, "method": m, "known": bool(k)}
              for h, t, u, ip, m, k in S.q("SELECT * FROM logins ORDER BY ts DESC LIMIT 15")]
    return {"now": now, "hosts": hosts, "cluster": cluster, "alerts": alerts, "events": events,
            "silences": silences, "logins": logins, "forecast": forecasts()}


def annotations(scope, start, end):
    """Алерты и окна обслуживания, которые касаются графика (для подсветки на графиках)."""
    out = []
    for key, lvl, msg, s, e in S.q("SELECT key, level, msg, start, end FROM alert_log "
                                   "WHERE start < ? AND (end IS NULL OR end > ?)", (end, start)):
        if Alerts.scope_matches(scope, key):
            out.append({"kind": lvl, "msg": msg, "start": s, "end": e})
    for sid, sc, reason, s, e in S.q("SELECT id, scope, reason, start, end FROM silence_log "
                                     "WHERE start < ? AND (end IS NULL OR end > ?)", (end, start)):
        if sc == "all" or sc == scope:
            out.append({"kind": "maint", "msg": "Обслуживание: " + scope_label(sc) + (f" — {reason}" if reason else ""),
                        "start": s, "end": e})
    return out


def alert_history(days):
    since = time.time() - days * 86400
    rows = S.q("SELECT key, level, msg, start, end, muted FROM alert_log WHERE start > ? OR end IS NULL "
               "OR end > ? ORDER BY start DESC", (since, since))
    now = time.time()
    agg = {}
    for key, lvl, msg, s, e, muted in rows:
        a = agg.setdefault(key, {"key": key, "msg": msg, "count": 0, "total": 0, "last": 0, "level": "warn",
                                 "active": False})
        a["count"] += 1
        a["total"] += (e or now) - max(s, since)
        a["last"] = max(a["last"], s)
        if lvl == "crit":
            a["level"] = "crit"
        if e is None:
            a["active"] = True
    items = [{"key": k, "level": l, "msg": m, "start": s, "end": e, "muted": bool(mu)}
             for k, l, m, s, e, mu in rows[:300]]
    return {"summary": sorted(agg.values(), key=lambda a: (-a["count"], -a["total"])), "items": items}


def settings_payload():
    r = rules()
    return {"rules": [{"key": k, "value": r[k], "default": v[0], "label": v[1], "unit": v[2], "min": v[3], "max": v[4]}
                      for k, v in RULES_META.items()],
            "settings": settings(), "checks": checks_list() or [],
            "hosts": [x[0] for x in S.q("SELECT name FROM hosts ORDER BY name")],
            "bot": bool(tg_env().get("LUMEN_BOT_TOKEN"))}


def save_settings(body):
    if "rules" in body:
        over = {}
        for k, v in body["rules"].items():
            if k not in RULES_META:
                raise ValueError(f"неизвестное правило {k}")
            v = float(v)
            lo, hi = RULES_META[k][3], RULES_META[k][4]
            if not lo <= v <= hi:
                raise ValueError(f"{RULES_META[k][1]}: допустимо от {lo} до {hi}")
            if v != RULES_META[k][0]:
                over[k] = int(v) if v.is_integer() else v
        S.meta_set("rules_override", over)
    st = settings()
    if "summary_time" in body:
        t = str(body["summary_time"]).strip()
        if t and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise ValueError("время сводки в формате ЧЧ:ММ или пусто, чтобы выключить")
        st["summary_time"] = t
    if "weekly_report" in body:
        st["weekly_report"] = bool(body["weekly_report"])
    if "checks_runner" in body:
        st["checks_runner"] = str(body["checks_runner"])[:64]
    if "ssh_known" in body:
        known = []
        for x in body["ssh_known"]:
            x = str(x).strip()
            if x:
                try:
                    ipaddress.ip_network(x, strict=False)
                except ValueError:
                    raise ValueError(f"неверный адрес или подсеть: {x}")
                known.append(x)
        st["ssh_known"] = known
    S.meta_set("settings", st)
    if "checks" in body:
        S.meta_set("checks", [validate_check(c) for c in body["checks"]])
    FORECAST["ts"] = 0
    event("info", "Настройки изменены")


def parse_cookie(header):
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "lumen"
    timeout = 30  # медленный или зависший клиент не держит поток вечно

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=()):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        if CFG["tls_cert"]:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def client_ip(self):
        return self.client_address[0]

    def session_token(self):
        return parse_cookie(self.headers.get("Cookie")).get("lumen_session")

    def current_user(self):
        if not users():
            return "(без пароля)"
        return SESSIONS.user(self.session_token())

    def require_user(self, api=True):
        u = self.current_user()
        if u:
            return u
        if api:
            self.send(401, {"error": "login required"})
        else:
            self.send(302, b"", "text/plain", [("Location", "/login")])
        return None

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 4 * 1024 * 1024:
            raise ValueError("too big")
        return json.loads(self.rfile.read(n) or b"{}")

    def static(self, name):
        with open(os.path.join(CFG["web"], name), "rb") as f:
            return self.send(200, f.read(), "text/html; charset=utf-8")

    # ---- POST
    def do_POST(self):
        if self.path == "/api/push":
            return self.push()
        # свой заголовок не даёт отправить запрос обычной формой с чужой страницы
        if self.headers.get("X-Lumen") != "1":
            return self.send(403, {"error": "missing X-Lumen header"})
        try:
            body = self.read_json()
            if self.path == "/api/login":
                return self.login(body)
            if self.path == "/api/logout":
                tok = self.session_token()
                if tok:
                    SESSIONS.drop(tok)
                return self.send(200, {"ok": True}, headers=[("Set-Cookie", self.cookie("", 0))])
            user = self.require_user()
            if not user:
                return
            if self.path == "/api/silence":
                scope = str(body.get("scope", ""))
                hosts = {r[0] for r in S.q("SELECT name FROM hosts")}
                if scope not in SILENCE_SCOPES and not (scope.startswith("host:") and scope[5:] in hosts):
                    return self.send(400, {"error": "bad scope"})
                minutes = int(body.get("minutes", 60))
                if not 1 <= minutes <= 7 * 1440:
                    return self.send(400, {"error": "bad minutes"})
                return self.send(200, ALERTS.add_silence(scope, minutes, str(body.get("reason", "")),
                                                         f"{user}@{self.client_ip()}"))
            if self.path == "/api/silence/delete":
                return self.send(200, {"ok": ALERTS.remove_silence(str(body.get("id")))})
            if self.path == "/api/settings":
                save_settings(body)
                return self.send(200, settings_payload())
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        except Exception as e:
            return self.send(400, {"error": repr(e)})
        self.send(404, {"error": "not found"})

    def cookie(self, value, max_age):
        secure = "; Secure" if CFG["tls_cert"] else ""
        return f"lumen_session={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def login(self, body):
        ip = self.client_ip()
        if SESSIONS.blocked(ip):
            return self.send(429, {"error": "слишком много попыток, подождите 10 минут"})
        user, pw = str(body.get("user", "")), str(body.get("password", ""))
        stored = users().get(user)
        if not stored or not check_password(pw, stored):
            SESSIONS.failed(ip)
            time.sleep(1)
            if SESSIONS.blocked(ip):
                event("warn", f"Вход в Lumen: много неудачных попыток с {ip}")
                ALERTS.notify(f"🔐 Lumen: {LOGIN_LIMIT[0]} неудачных попыток входа с {ip}, адрес заблокирован на 10 минут")
            return self.send(403, {"error": "неверный логин или пароль"})
        tok = SESSIONS.create(user, ip)
        event("info", f"Вход в Lumen: {user} с {ip}")
        return self.send(200, {"ok": True}, headers=[("Set-Cookie", self.cookie(tok, CFG["session_days"] * 86400))])

    def push(self):
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {CFG['token']}"):
            return self.send(403, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 4 * 1024 * 1024:
                return self.send(413, {"error": "too big"})
            return self.send(200, ingest(json.loads(self.rfile.read(n))))
        except Exception as e:
            return self.send(400, {"error": repr(e)})

    # ---- GET
    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/login":
                return self.static("login.html")
            if u.path in ("/", "/index.html"):
                if self.require_user(api=False):
                    return self.static("index.html")
                return
            if u.path == "/api/health":
                # без входа: для сторожевой проверки с другого узла, только возраст последних данных
                last = S.q("SELECT MAX(last_ts) FROM hosts")[0][0] or 0
                return self.send(200, {"ok": True, "last_push_age": round(time.time() - last, 1)})
            if not u.path.startswith("/api/"):
                return self.send(404, {"error": "not found"})
            user = self.require_user()
            if not user:
                return
            now = int(time.time())
            if u.path == "/api/overview":
                return self.send(200, dict(overview(), user=user, auth=bool(users())))
            if u.path == "/api/history":
                end = int(qs.get("end", now))
                start = int(qs.get("start", end - min(int(qs.get("range", 3600)), CFG["rollup_keep_days"] * 86400)))
                metrics = qs.get("metrics", "cpu").split(",")[:30]
                out = {m: series(qs["host"], m, start, end) for m in metrics}
                if qs.get("scope"):
                    # без явного конца берём с запасом, чтобы не потерять только что начавшееся
                    out["_annotations"] = annotations(qs["scope"], start, end if "end" in qs else end + 60)
                return self.send(200, out)
            if u.path == "/api/metrics":
                rows = S.q("SELECT DISTINCT metric FROM raw WHERE host=? AND ts>?", (qs["host"], now - 3600))
                return self.send(200, sorted(r[0] for r in rows))
            if u.path == "/api/uptime":
                return self.send(200, uptime(min(int(qs.get("days", 30)), CFG["rollup_keep_days"])))
            if u.path == "/api/alerts/history":
                return self.send(200, alert_history(min(int(qs.get("days", 7)), CFG["rollup_keep_days"])))
            if u.path == "/api/settings":
                return self.send(200, settings_payload())
        except Exception as e:
            return self.send(500, {"error": repr(e)})
        self.send(404, {"error": "not found"})


class TLSServer(ThreadingHTTPServer):
    """TLS-рукопожатие выполняется в потоке запроса, а не в accept(): зависший клиент
    не блокирует приём остальных соединений."""

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx

    def finish_request(self, request, client_address):
        request.settimeout(15)
        try:
            request = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return  # сканеры портов и клиенты без TLS
        super().finish_request(request, client_address)

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


class Redirect(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        base = CFG["public_url"] or f"https://{self.headers.get('Host', '').split(':')[0]}:{CFG['port']}"
        self.send_response(301)
        self.send_header("Location", base.rstrip("/") + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET


def cmd_passwd(user):
    if not re.fullmatch(r"[\w.\-]{1,32}", user):
        sys.exit("имя пользователя: буквы, цифры, точка, дефис")
    pw = getpass.getpass(f"Новый пароль для {user}: ")
    if len(pw) < 10:
        sys.exit("пароль должен быть не короче 10 символов")
    if getpass.getpass("Повторите: ") != pw:
        sys.exit("пароли не совпадают")
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("users", {})[user] = hash_password(pw)
    tmp = CONFIG_PATH + ".new"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(cfg, f, indent=1, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)
    print(f"Пароль для {user} сохранён. Перезапуск не нужен.")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "passwd":
        return cmd_passwd(sys.argv[2])
    if not CFG["token"]:
        sys.exit("token не задан в " + CONFIG_PATH)
    threading.Thread(target=background, daemon=True).start()
    threading.Thread(target=bot_loop, daemon=True).start()
    if CFG["tls_cert"]:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CFG["tls_cert"], CFG["tls_key"])
        srv = TLSServer((CFG["listen"], CFG["port"]), Handler, ctx)
    else:
        srv = ThreadingHTTPServer((CFG["listen"], CFG["port"]), Handler)
    if CFG["redirect_port"]:
        red = ThreadingHTTPServer((CFG["listen"], CFG["redirect_port"]), Redirect)
        threading.Thread(target=red.serve_forever, daemon=True).start()
    print(f"lumen server on {'https' if CFG['tls_cert'] else 'http'}://{CFG['listen']}:{CFG['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
