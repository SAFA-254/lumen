"""Вычисления по истории: свёртка данных, прогноз дисков, аномалии, доступность, возраст бэкапов."""
import time

from .config import CFG, ROLLUP_STEP
from .store import S
from .fmt import fmt_bytes


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


def backup_work_age(last, now):
    # задание бэкапов идёт только по будням, поэтому субботу и воскресенье в возраст не считаем
    age, t = 0, last
    while t < now:
        d = time.localtime(t)
        nxt = min(now, time.mktime((d.tm_year, d.tm_mon, d.tm_mday + 1, 0, 0, 0, 0, 0, -1)))
        if d.tm_wday < 5:
            age += nxt - t
        t = nxt
    return age
