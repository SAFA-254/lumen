#!/usr/bin/env python3
"""Lumen server: принимает метрики от агентов, хранит в SQLite, отдаёт API и веб-интерфейс,
шлёт алерты в Telegram. Только стандартная библиотека Python."""
import base64
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CONFIG_PATH = os.environ.get("LUMEN_CONFIG", "/etc/lumen/server.json")
DEFAULTS = {
    "listen": "0.0.0.0",
    "port": 8095,
    "token": "",
    "db": "/var/lib/lumen/lumen.db",
    "web": os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"),
    "ui_user": "",
    "ui_password": "",
    "notify_cmd": "",
    "raw_keep_hours": 48,
    "rollup_keep_days": 30,
    "rules": {
        "host_down_sec": 90,
        "cpu_pct": 90, "cpu_for_sec": 300,
        "mem_pct": 92, "mem_for_sec": 300,
        "disk_warn_pct": 85, "disk_crit_pct": 95,
        "temp_c": 85,
        "patroni_lag_bytes": 16 * 1024 * 1024,
        "backup_max_age_h": 36,
        "cluster_stale_sec": 300,
        "check_for_sec": 60,
        "etcd_fsync_ms": 100,
        "osd_latency_ms": 250,
    },
}

ROLLUP_STEP = 300
SERIES_MIN_GAP = 20  # кластерные ряды присылают несколько агентов — пишем не чаще раза в 20 с
SILENCE_SCOPES = ("all", "ceph", "patroni", "checks", "backups")


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            user = json.load(f)
        rules = user.pop("rules", {})
        cfg.update(user)
        cfg["rules"].update(rules)
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


# ---------------------------------------------------------------- ingest

SERIES_LAST = {}


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

    def w(db):
        db.execute("INSERT OR REPLACE INTO hosts VALUES(?,?,?)", (host, now, json.dumps(latest)))
        db.executemany("INSERT INTO raw VALUES(?,?,?,?)", rows)
        for kind, data in (p.get("cluster") or {}).items():
            if data is not None:
                db.execute("INSERT OR REPLACE INTO cluster VALUES(?,?,?,?)", (kind, now, host, json.dumps(data)))
    S.tx(w)


# ---------------------------------------------------------------- maintenance

def rollup_and_prune():
    now = int(time.time())
    last = S.meta_get("rollup_ts", now - 3 * 86400)
    upto = now - now % ROLLUP_STEP  # только завершённые корзины

    def w(db):
        db.execute(f"""INSERT OR REPLACE INTO roll
            SELECT host, metric, ts - ts % {ROLLUP_STEP}, AVG(v), MAX(v) FROM raw
            WHERE ts >= ? AND ts < ? GROUP BY host, metric, ts - ts % {ROLLUP_STEP}""",
                   (last - last % ROLLUP_STEP, upto))
        db.execute("DELETE FROM raw WHERE ts < ?", (now - CFG["raw_keep_hours"] * 3600,))
        db.execute("DELETE FROM roll WHERE ts < ?", (now - CFG["rollup_keep_days"] * 86400,))
        db.execute("DELETE FROM events WHERE ts < ?", (now - CFG["rollup_keep_days"] * 86400,))
    S.tx(w)
    S.meta_set("rollup_ts", upto)


# ---------------------------------------------------------------- alerts

class Alerts:
    """Состояние алертов: key -> {level, msg, since}. Пороговые правила срабатывают,
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
            return kind in ("down", "cpu", "mem", "temp", "disk", "node") and rest.split(":")[0] == scope[5:]
        kinds = {"ceph": ("ceph",), "patroni": ("pg", "etcd", "etcdlat"), "checks": ("chk",), "backups": ("bk",)}
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
        r = CFG["rules"]
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
            for d in lt.get("disks") or []:
                pct = 100.0 * d["used"] / d["size"] if d.get("size") else 0
                if pct > r["disk_warn_pct"]:
                    lvl = "crit" if pct > r["disk_crit_pct"] else "warn"
                    found[f"disk:{name}:{d['mount']}"] = (lvl, f"{name}: диск {d['mount']} {pct:.0f}%", 0)

        cl = {k: (ts, json.loads(d)) for k, ts, d in S.q("SELECT kind, ts, data FROM cluster")}

        if "ceph" in cl:
            ts, c = cl["ceph"]
            if now - ts > r["cluster_stale_sec"]:
                found["ceph:stale"] = ("warn", "Ceph: статус не обновлялся", 0)
            elif c.get("health") != "HEALTH_OK":
                lvl = "crit" if c.get("health") == "HEALTH_ERR" else "warn"
                checks = "; ".join(x["msg"] for x in c.get("checks", []))[:300]
                found["ceph:health"] = (lvl, f"Ceph {c.get('health')}: {checks}", 60)

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
                etcd = pt.get("etcd") or {}
                for ep, st in etcd.items():
                    if not st.get("ok"):
                        found[f"etcd:{ep}"] = ("crit", f"etcd {ep}: нездоров ({st.get('err', '')})", 30)

        for kind, (ts, results) in cl.items():
            if not kind.startswith("checks:") or now - ts > r["cluster_stale_sec"]:
                continue
            src = kind[7:]
            for c in results:
                if not c.get("ok"):
                    found[f"chk:{c['name']}"] = ("crit", f"Проверка «{c['name']}» не прошла: {c.get('err') or 'ошибка'}"
                                                         f" ({c['target']}, с {src})", r["check_for_sec"])

        if "ceph" in cl:
            ts, c = cl["ceph"]
            lat = c.get("osd_lat") or {}
            if lat and now - ts <= r["cluster_stale_sec"]:
                worst = max(lat, key=lat.get)
                if lat[worst] > r["osd_latency_ms"]:
                    found["ceph:lat"] = ("warn", f"Ceph: задержка записи {worst} {lat[worst]} мс", 120)

        if "patroni" in cl:
            ts, pt = cl["patroni"]
            lc_prev = S.meta_get("etcd_lc", {})
            for ep, st in (pt.get("etcd") or {}).items():
                if (st.get("fsync_ms") or 0) > r["etcd_fsync_ms"]:
                    found[f"etcdlat:{ep}"] = ("warn", f"etcd {ep}: fsync {st['fsync_ms']:.0f} мс", 60)
                lc = st.get("leader_changes")
                if lc is not None:
                    if ep in lc_prev and lc > lc_prev[ep]:
                        event("warn", f"etcd {ep}: смена лидера etcd (всего {lc})")
                    lc_prev[ep] = lc
            S.meta_set("etcd_lc", lc_prev)

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
                self.active[key] = {"level": lvl, "msg": msg, "since": now, "notified": not muted}
                event(lvl, msg + (" (обслуживание)" if muted else ""))
                if not muted:
                    self.notify(icon(lvl) + msg)
            else:
                if old["level"] != lvl:
                    event(lvl, msg)
                    if not muted:
                        self.notify(icon(lvl) + msg)
                        old["notified"] = True
                elif not muted and not old.get("notified", True):
                    # обслуживание закончилось, а проблема осталась
                    self.notify(icon(lvl) + msg + " (после обслуживания)")
                    old["notified"] = True
                old.update(level=lvl, msg=msg)
        for key in list(self.active):
            if key not in fired:
                a = self.active.pop(key)
                dur = int((now - a["since"]) / 60)
                event("ok", f"Решено: {a['msg']} ({dur} мин)")
                if a.get("notified", True) and not self.silenced(key):
                    self.notify(f"✅ Решено: {a['msg']} (длилось {dur} мин)")
        S.meta_set("alerts", self.active)


def scope_label(scope):
    return {"all": "весь кластер", "ceph": "Ceph", "patroni": "Patroni/etcd", "checks": "проверки сервисов",
            "backups": "бэкапы"}.get(scope, scope.replace("host:", "хост "))


def fmt_minutes(m):
    return f"{m // 60} ч" + (f" {m % 60} мин" if m % 60 else "") if m >= 60 else f"{m} мин"


ALERTS = Alerts()


def background():
    tick = 0
    while True:
        time.sleep(15)
        tick += 1
        try:
            ALERTS.evaluate()
            if tick % 20 == 1:
                rollup_and_prune()
        except Exception as e:  # фоновый цикл не должен падать
            print("background error:", repr(e), file=sys.stderr, flush=True)


# ---------------------------------------------------------------- API

def series(host, metric, start, end, points=240):
    span = end - start
    step = max(15, span // points)
    table = "raw" if span <= CFG["raw_keep_hours"] * 3600 - 600 else "roll"
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
    alerts = sorted([dict(a, key=k, muted=ALERTS.silenced(k)) for k, a in ALERTS.active.items()],
                    key=lambda a: (a["muted"], a["level"] != "crit", a["since"]))
    silences = [dict(x, label=scope_label(x["scope"])) for x in ALERTS.silences if x["until"] > now]
    return {"now": now, "hosts": hosts, "cluster": cluster, "alerts": alerts, "events": events,
            "silences": silences}


class Handler(BaseHTTPRequestHandler):
    server_version = "lumen"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def ui_auth_ok(self):
        if not CFG["ui_password"]:
            return True
        want = base64.b64encode(f"{CFG['ui_user']}:{CFG['ui_password']}".encode()).decode()
        if self.headers.get("Authorization") == f"Basic {want}":
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="lumen"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 4 * 1024 * 1024:
            raise ValueError("too big")
        return json.loads(self.rfile.read(n) or b"{}")

    def ui_post(self):
        # свой заголовок не даёт отправить запрос обычной формой с чужой страницы
        if self.headers.get("X-Lumen") != "1":
            return self.send(403, {"error": "missing X-Lumen header"})
        if not self.ui_auth_ok():
            return
        try:
            body = self.read_json()
            if self.path == "/api/silence":
                scope = str(body.get("scope", ""))
                hosts = {r[0] for r in S.q("SELECT name FROM hosts")}
                if scope not in SILENCE_SCOPES and not (scope.startswith("host:") and scope[5:] in hosts):
                    return self.send(400, {"error": "bad scope"})
                minutes = int(body.get("minutes", 60))
                if not 1 <= minutes <= 7 * 1440:
                    return self.send(400, {"error": "bad minutes"})
                who = self.headers.get("X-Forwarded-For") or self.client_address[0]
                return self.send(200, ALERTS.add_silence(scope, minutes, str(body.get("reason", "")), who))
            if self.path == "/api/silence/delete":
                return self.send(200, {"ok": ALERTS.remove_silence(str(body.get("id")))})
        except Exception as e:
            return self.send(400, {"error": repr(e)})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/push":
            return self.ui_post()
        if self.headers.get("Authorization") != f"Bearer {CFG['token']}":
            return self.send(403, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 4 * 1024 * 1024:
                return self.send(413, {"error": "too big"})
            ingest(json.loads(self.rfile.read(n)))
        except Exception as e:
            return self.send(400, {"error": repr(e)})
        self.send(200, {"ok": True})

    def do_GET(self):
        if not self.ui_auth_ok():
            return
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/api/overview":
                return self.send(200, overview())
            if u.path == "/api/history":
                now = int(time.time())
                rng = min(int(qs.get("range", 3600)), CFG["rollup_keep_days"] * 86400)
                metrics = qs.get("metrics", "cpu").split(",")[:20]
                out = {m: series(qs["host"], m, now - rng, now) for m in metrics}
                return self.send(200, out)
            if u.path == "/api/metrics":
                rows = S.q("SELECT DISTINCT metric FROM raw WHERE host=? AND ts>?",
                           (qs["host"], int(time.time()) - 3600))
                return self.send(200, sorted(r[0] for r in rows))
            if u.path in ("/", "/index.html"):
                with open(os.path.join(CFG["web"], "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
        except Exception as e:
            return self.send(500, {"error": repr(e)})
        self.send(404, {"error": "not found"})


def main():
    if not CFG["token"]:
        sys.exit("token не задан в " + CONFIG_PATH)
    threading.Thread(target=background, daemon=True).start()
    srv = ThreadingHTTPServer((CFG["listen"], CFG["port"]), Handler)
    print(f"lumen server on {CFG['listen']}:{CFG['port']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
