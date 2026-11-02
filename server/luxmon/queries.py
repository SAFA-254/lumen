"""Чтение данных для API: ряды, сессии SSH, сводное состояние, история алертов."""
import json
import re
import time

from .config import CFG, HOURLY_STEP, ROLLUP_STEP
from .store import S, event
from .fmt import scope_label
from .analytics import forecasts
from .settings import backup_age, rules
from .alerts import ALERTS, Alerts
from .ingest import METHODS, addr_names


def series(host, metric, start, end, points=240):
    span = max(1, end - start)
    step = max(15, span // points)
    now = time.time()
    # самая подробная таблица, в которой ещё есть начало периода
    if start >= now - CFG["raw_keep_hours"] * 3600 + 600:
        table = "raw"
    elif start >= now - CFG["rollup_keep_days"] * 86400 + 600:
        table, step = "roll", max(step, ROLLUP_STEP)
    else:
        table, step = "hourly", max(step, HOURLY_STEP)
    rows = S.q(f"""SELECT ts - ts % {step} AS b, AVG(v) FROM {table}
                   WHERE host=? AND metric=? AND ts>=? AND ts<=? GROUP BY b ORDER BY b""",
               (host, metric, start, end))
    return {"step": step, "points": [[b, round(v, 3)] for b, v in rows]}


def ssh_sessions(limit, since=0, fails=False):
    """Сессии SSH: кто, откуда (адрес и имя), куда (хост и его адрес), чем вошёл, когда вышел."""
    names, dest = addr_names(), {}
    for ip, n in names.items():
        dest.setdefault(n, ip)
    rows = S.q("SELECT host, ts, user, ip, method, known, port, end, key, fail FROM logins WHERE ts > ? "
               + ("" if fails else "AND fail=0 ") + "ORDER BY ts DESC LIMIT ?", (since, limit))
    return [{"host": h, "host_ip": dest.get(h), "ts": t, "user": u, "ip": ip, "src": names.get(ip),
             "method": METHODS.get(m, m), "key": key, "known": bool(k), "end": end, "fail": bool(f),
             "tracked": port is not None}
            for h, t, u, ip, m, k, port, end, key, f in rows]


def ssh_fails(since):
    return [{"host": h, "ip": ip, "n": n, "users": (users or "").split(","), "last": last}
            for h, ip, n, users, last in S.q(
                "SELECT host, ip, COUNT(*), GROUP_CONCAT(DISTINCT user), MAX(ts) FROM logins "
                "WHERE fail=1 AND ts > ? GROUP BY host, ip ORDER BY MAX(ts) DESC", (since,))]


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
    for g in cluster.get("backups", {}).get("guests", []):
        g["work_age"] = backup_age(g["last"], now)
        g["stale"] = g["work_age"] > rules()["backup_max_age_h"] * 3600
    events = [{"ts": t, "level": l, "msg": m}
              for t, l, m in S.q("SELECT ts, level, msg FROM events ORDER BY ts DESC LIMIT 40")]
    alerts = sorted([dict({k: v for k, v in a.items() if k != "log_id"}, key=k, muted=ALERTS.silenced(k))
                     for k, a in ALERTS.active.items()],
                    key=lambda a: (a["muted"], a["level"] != "crit", a["since"]))
    silences = [dict(x, label=scope_label(x["scope"])) for x in ALERTS.silences if x["until"] > now]
    logins = ssh_sessions(150, now - 7 * 86400)  # панель прокручивается — показываем неделю
    return {"now": now, "hosts": hosts, "cluster": cluster, "alerts": alerts, "events": events,
            "silences": silences, "logins": logins, "ssh_fails": ssh_fails(now - 86400), "forecast": forecasts(),
            "day": day_strip(now)}


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
    for nid, ts, sc, text, by in S.q("SELECT id, ts, scope, text, by FROM notes WHERE ts >= ? AND ts <= ? "
                                     "AND (scope = ? OR scope = 'all') ORDER BY ts", (start, end, scope)):
        out.append({"kind": "note", "id": nid, "msg": text, "by": by, "start": ts, "end": ts, "all": sc == "all"})
    return out


NOTE_SCOPE = re.compile(r"all|ceph|patroni|checks|host:[\w.\-]{1,64}|vm:\d{1,9}")


def add_note(body, by):
    """Заметка на графиках: «обновил ядро», «перенёс VM» — потом видно, отчего изменилась нагрузка."""
    scope, text = str(body.get("scope", "")), str(body.get("text", "")).strip()[:300]
    ts = float(body.get("ts") or time.time())
    if not NOTE_SCOPE.fullmatch(scope):
        raise ValueError("неверная область заметки")
    if not text:
        raise ValueError("пустая заметка")
    if abs(ts - time.time()) > 400 * 86400:
        raise ValueError("время заметки вне хранимой истории")
    S.q("INSERT INTO notes(ts, scope, text, by) VALUES(?,?,?,?)", (ts, scope, text, by))
    event("info", f"Заметка ({scope_label(scope) if scope != 'all' else 'все графики'}): {text}")


def delete_note(nid):
    S.q("DELETE FROM notes WHERE id=?", (int(nid),))


def day_strip(now):
    # тревоги за сутки и периоды, когда сам сервер не получал данные (тогда и алерты не записывались)
    since = now - 86400
    alerts = [{"level": l, "msg": m, "start": a, "end": e} for l, m, a, e in S.q(
        "SELECT level, msg, start, end FROM alert_log WHERE muted=0 AND (end IS NULL OR end > ?)", (since,))]
    # по хостам: так запрос идёт по индексу, а не просматривает всю таблицу raw под блокировкой базы
    stamps = {t for (h,) in S.q("SELECT name FROM hosts") for (t,) in S.q(
        "SELECT ts FROM raw WHERE host=? AND metric='cpu' AND ts > ?", (h, int(since)))}
    gaps, prev = [], since
    for t in sorted(stamps) + [now]:
        if t - prev > 300:
            gaps.append([prev, t])
        prev = t
    return {"alerts": alerts, "gaps": gaps}


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
