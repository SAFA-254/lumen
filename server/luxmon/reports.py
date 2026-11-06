"""Текстовые отчёты: статус, утренняя сводка, итоги недели."""
import json
import time

from .config import ROLLUP_STEP
from .store import S
from .fmt import fmt_bytes, fmt_dur
from .analytics import forecasts, uptime
from .settings import backup_age, rules, settings
from .alerts import ALERTS
from .queries import alert_history, overview


LV_ICON = {"crit": "🔴", "warn": "🟡", "info": "🔵"}


def status_text():
    """Короткая сводка текущего состояния для /status и утреннего отчёта."""
    d = overview()
    lines = []
    live = [a for a in d["alerts"] if not a["muted"] and a["level"] != "info"]
    odd = [a for a in d["alerts"] if not a["muted"] and a["level"] == "info"]
    lines.append("✅ Всё в порядке" if not live else
                 f"⚠️ Проблем: {len(live)}\n" + "\n".join(("🔴 " if a["level"] == "crit" else "🟡 ") + a["msg"]
                                                       for a in live[:10]))
    if odd:
        lines.append(f"🔵 Необычное (выше обычного для этого часа): {len(odd)}")
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
    out = [f"☀️ Luxmon: сводка за сутки ({time.strftime('%d.%m %H:%M')})", "", status_text(), ""]
    rows = S.q("SELECT key, level, msg, start, end FROM alert_log WHERE start > ? OR end IS NULL OR end > ? "
               "ORDER BY start", (day, day))
    if rows:
        crit = sum(r[1] == "crit" for r in rows)
        out.append(f"Алертов за сутки: {len(rows)} (критичных {crit})")
        for key, lvl, msg, start, end in rows[-8:]:
            dur = fmt_dur((end or now) - start)
            out.append(f"  {LV_ICON.get(lvl, '🟡')} {msg} — {dur}{'' if end else ', ещё идёт'}")
    else:
        out.append("Алертов за сутки не было")
    c = {k: json.loads(v) for k, v in S.q("SELECT kind, data FROM cluster")}
    bk = c.get("backups")
    if bk:
        tasks = [t for t in bk.get("tasks", []) if t["starttime"] > day]
        bad = [t for t in tasks if t.get("status") not in (None, "OK")]
        stale = [g for g in bk.get("guests", []) if backup_age(g["last"], now) > rules()["backup_max_age_h"] * 3600]
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
    # пробел у всех хостов, которые уже присылали данные: скорее всего не работал сам Luxmon (или его узел)
    shared = [g for g in sorted({g for v in gaps.values() for g in v})
              if len(alive := [h for h in gaps if first[h] < g[0]]) >= 2 and all(same(g, gaps[h]) for h in alive)]
    shared = [g for i, g in enumerate(shared) if not same(g, shared[:i])]  # у хостов начало отличается на секунды
    if shared:
        out.append("⚠️ Luxmon не получал данных: " + "; ".join(span(a, b) for a, b in shared[:3]))
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
    logins = S.q("SELECT host, user, ip, COUNT(*) FROM logins WHERE ts > ? AND known=0 AND fail=0 GROUP BY host, user, ip", (day,))
    if logins:
        out.append("Входы по SSH с незнакомых адресов: " + "; ".join(f"{h}: {u} с {ip} ×{n}" for h, u, ip, n in logins))
    return "\n".join(out)


def weekly_report():
    now = time.time()
    week = now - 7 * 86400
    out = [f"📅 Luxmon: итоги недели ({time.strftime('%d.%m', time.localtime(week))}–{time.strftime('%d.%m')})", ""]
    # 5-минутные корзины, где были данные хоть от кого-то, — время, когда работал сам Luxmon;
    # доступность хоста считается только по этому времени, чтобы простой Luxmon не записывался хостам
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
        out.append(f"Luxmon собирал данные {own:.1f}% времени" + (" — были перерывы" if own < 99.5 else ""))
        out.append("Доступность хостов: " + ", ".join(hosts))
    hist = alert_history(7)
    if hist["summary"]:
        n = sum(a["count"] for a in hist["summary"])
        crit = sum(1 for a in hist["items"] if a["level"] == "crit")
        out.append(f"Алертов за неделю: {n} (критичных {crit}), чаще всего:")
        for a in hist["summary"][:5]:
            out.append(f"  {LV_ICON.get(a['level'], '🟡')} {a['msg']} — ×{a['count']}, всего {fmt_dur(a['total'])}")
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
    (unk,), = S.q("SELECT COUNT(*) FROM logins WHERE ts > ? AND known=0 AND fail=0", (week,))
    (tot,), = S.q("SELECT COUNT(*) FROM logins WHERE ts > ? AND fail=0", (week,))
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
