"""Движок алертов: правила, удержание, обслуживание, уведомления."""
import json
import subprocess
import sys
import threading
import time

from .config import CFG, SMART_NAMES
from .store import S, event
from .fmt import fmt_bytes, fmt_dur, fmt_minutes, scope_label
from .analytics import anomalies, forecasts
from .settings import backup_age, rules


class Alerts:
    """Состояние алертов: key -> {level, msg, since, log_id}. Пороговые правила срабатывают,
    только если нарушение держится дольше заданного времени."""

    def __init__(self):
        self.active = S.meta_get("alerts", {})
        self.pending = {}  # key -> время первого нарушения
        self.clearing = {}  # key -> с какого момента проблемы нет, а алерт ещё открыт
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
                            "iowait", "anom", "agent", "clock", "dlat", "if") and \
                rest.split(":")[0] == scope[5:]
        kinds = {"ceph": ("ceph", "stor"), "patroni": ("pg", "etcd", "etcdlat", "pgdb"), "checks": ("chk", "cert"),
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
            if m.get("swap", 0) > r["swap_pct"]:
                found[f"mem:{name}:swap"] = ("warn", f"{name}: swap занят на {m['swap']:.0f}%", r["mem_for_sec"])
            ncpu = (lt.get("info") or {}).get("cpus") or 1
            if m.get("load5", 0) / ncpu > r["load_per_cpu"]:
                found[f"cpu:{name}:load"] = ("warn", f"{name}: load average {m['load5']:.1f} на {ncpu} ядер", 300)
            if (m.get("disk_lat") or 0) > r["disk_lat_ms"]:
                found[f"dlat:{name}"] = ("warn", f"{name}: диск отвечает медленно — {m['disk_lat']:.0f} мс на операцию",
                                         900)
            self.host_changes(name, lt)
            for i in lt.get("ifaces") or []:
                if (i.get("err_min") or 0) > r["net_err_per_min"]:
                    found[f"if:{name}:{i['name']}:err"] = ("warn", f"{name}: {i['err_min']:.0f} ошибок в минуту "
                                                                   f"на интерфейсе {i['name']}", 300)
            for key, v in self.iface_problems(name, lt.get("ifaces") or [], now).items():
                found[key] = v
            psi = m.get("mem_psi") or 0
            if psi > r["mem_psi_pct"]:
                found[f"mem:{name}:psi"] = ("warn", f"{name}: не хватает памяти — процессы ждут подкачку с диска "
                                                    f"{psi:.0f}% времени", r["mem_for_sec"])
            # при нехватке памяти агент тоже подкачивает свой код с диска — это следствие, о нём не пишем
            if m.get("agent_cpu", 0) > r["agent_cpu_pct"] or \
                    (m.get("agent_io", 0) > r["agent_io_kbs"] * 1024 and psi <= r["mem_psi_pct"]):
                found[f"agent:{name}"] = ("warn", f"{name}: агент Luxmon тратит CPU {m.get('agent_cpu', 0):.0f}%, "
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
                if d.get("ro") and d["mount"] != "/boot/efi":
                    found[f"disk:{name}:{d['mount']}:ro"] = ("crit", f"{name}: файловая система {d['mount']} "
                                                                     f"стала только для чтения", 0)
                ip = d.get("inodes") or 0
                if ip > r["disk_warn_pct"]:
                    found[f"disk:{name}:{d['mount']}:inodes"] = ("crit" if ip > r["disk_crit_pct"] else "warn",
                                                                 f"{name}: на {d['mount']} заканчиваются inode — "
                                                                 f"занято {ip:.0f}%", 0)
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
            slow = []
            for ep, st in (pt.get("etcd") or {}).items():
                if not st.get("ok"):
                    found[f"etcd:{ep}"] = ("crit", f"etcd {ep}: нездоров ({st.get('err', '')})", 30)
                if (st.get("fsync_ms") or 0) > r["etcd_fsync_ms"]:
                    slow.append(f"{ep.split(':')[0]} {st['fsync_ms']:.0f} мс")
                lc = st.get("leader_changes")
                if lc is not None:
                    if ep in lc_prev and lc > lc_prev[ep]:
                        event("warn", f"etcd {ep}: смена лидера etcd (всего {lc})")
                    lc_prev[ep] = lc
            S.meta_set("etcd_lc", lc_prev)
            # все участники etcd пишут одно и то же, поэтому тормозят вместе — один алерт на всех
            if slow:
                found["etcdlat"] = ("warn", "etcd: медленная запись на диск (fsync) — " + ", ".join(slow),
                                    r["etcd_for_sec"])

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
            q = [(json.loads(x).get("info") or {}).get("quorate")
                 for (x,) in S.q("SELECT latest FROM hosts WHERE last_ts > ?", (now - r["host_down_sec"],))]
            if False in q:
                found["node:quorum"] = ("crit", "Кластер Proxmox без кворума", 30)
            for n in pv.get("nodes", []):
                if n.get("status") != "online":
                    found[f"node:{n['name']}"] = ("crit", f"Узел {n['name']}: {n.get('status')}", 0)
            prev = S.meta_get("guest_status", {})
            cur = {str(g["vmid"]): g["status"] for g in pv.get("guests", []) if not g.get("template")}
            for vmid, st in cur.items():
                if vmid in prev and prev[vmid] != st:
                    name = next((g["name"] for g in pv["guests"] if str(g["vmid"]) == vmid), vmid)
                    event("info", f"{vmid} {name}: {prev[vmid]} → {st}")
                    if not self.silenced(f"down:{name}"):
                        if st == "stopped":
                            self.notify(f"⏹ {vmid} {name} остановлена (была {prev[vmid]})")
                        elif st == "running":
                            self.notify(f"▶️ {vmid} {name} запущена")
            S.meta_set("guest_status", cur)
            agents = {h for (h,) in S.q("SELECT name FROM hosts")}
            ups = S.meta_get("guest_uptime", {})
            for g in pv.get("guests", []):
                vid = str(g["vmid"])
                if g.get("status") == "running" and g.get("uptime") is not None:
                    # гостей с агентом Luxmon ловит сам агент по аптайму. Узлы видят аптайм с расхождением
                    # до минуты, а данные Proxmox приходят то с одного узла, то с другого — мелкий откат не в счёт
                    if vid in ups and g["uptime"] + 300 < ups[vid] and cur.get(vid) == prev.get(vid) \
                            and g["name"] not in agents:
                        event("warn", f"{vid} {g['name']}: перезапустилась (работает {fmt_dur(g['uptime'])})")
                        self.notify(f"🔄 {vid} {g['name']} перезапустилась")
                    ups[vid] = g["uptime"]
            S.meta_set("guest_uptime", ups)
            for g in pv.get("guests", []):
                # у гостей без агента Luxmon берём CPU и память из Proxmox
                if g.get("status") != "running" or g.get("template") or g["name"] in agents:
                    continue
                if 100 * (g.get("cpu") or 0) > r["cpu_pct"]:
                    found[f"cpu:{g['name']}"] = ("warn", f"{g['name']}: CPU {100 * g['cpu']:.0f}%", r["cpu_for_sec"])
                if g.get("maxmem") and 100 * g["mem"] / g["maxmem"] > r["mem_pct"]:
                    found[f"mem:{g['name']}"] = ("warn", f"{g['name']}: RAM {100 * g['mem'] / g['maxmem']:.0f}%",
                                                 r["mem_for_sec"])
            for st in pv.get("storage", []):
                if st.get("maxdisk") and st.get("plugintype") not in ("rbd", "cephfs"):  # Ceph считаем отдельно
                    pct = 100.0 * st["disk"] / st["maxdisk"]
                    if pct > r["disk_warn_pct"]:
                        where = st["storage"] + ("" if st.get("shared") else f" на {st['node']}")
                        found[f"stor:{where}"] = ("crit" if pct > r["disk_crit_pct"] else "warn",
                                                  f"Хранилище {where} заполнено на {pct:.0f}%", 0)

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
                if g.get("last") and backup_age(g["last"], now) > max_age:
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
        # чтобы алерт не дребезжал, считаем его решённым, только когда проблемы нет какое-то время
        for key, a in self.active.items():
            if key in found:
                self.clearing.pop(key, None)
                a["hold"] = found[key][2]
            elif now - self.clearing.setdefault(key, now) < min(r["clear_sec"], a.get("hold", 0)):
                fired[key] = (a["level"], a["msg"])

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
                                    "log_id": cur[0][0], "hold": found[key][2]}
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
                self.clearing.pop(key, None)
                if a.get("log_id"):
                    S.q("UPDATE alert_log SET end=?, msg=? WHERE id=?", (now, a["msg"], a["log_id"]))
                dur = int((now - a["since"]) / 60)
                event("ok", f"Решено: {a['msg']} ({dur} мин)")
                if a.get("notified", True) and not self.silenced(key):
                    self.notify(f"✅ Решено: {a['msg']} (длилось {dur} мин)")
        S.meta_set("alerts", self.active)


    def host_changes(self, name, lt):
        """Перезагрузка хоста, новое ядро, изменения в /etc/passwd — сообщаем один раз."""
        info = lt.get("info") or {}
        base = S.meta_get("host_base", {})
        old = base.get(name, {})
        up = info.get("uptime")
        if up is not None and old.get("uptime") is not None and up < old["uptime"]:
            event("warn", f"{name}: перезагрузился (работает {fmt_dur(up)})")
            self.notify(f"🔄 {name} перезагрузился")
        if info.get("kernel") and old.get("kernel") and info["kernel"] != old["kernel"]:
            event("info", f"{name}: ядро {old['kernel']} → {info['kernel']}")
        pw = info.get("passwd") or {}
        if pw.get("hash") and old.get("passwd") and pw["hash"] != old["passwd"]:
            was, now_users = set(old.get("users", [])), set(pw.get("users", []))
            diff = [f"+{u}" for u in sorted(now_users - was)] + [f"−{u}" for u in sorted(was - now_users)]
            text = f"{name}: изменился /etc/passwd" + (f" ({', '.join(diff)})" if diff else " (uid или shell)")
            event("warn", text)
            if not self.silenced(f"ssh:{name}"):
                self.notify("👤 " + text)
        base[name] = {"uptime": up, "kernel": info.get("kernel") or old.get("kernel"),
                      "passwd": pw.get("hash") or old.get("passwd"), "users": pw.get("users") or old.get("users", [])}
        S.meta_set("host_base", base)

    def iface_problems(self, name, ifs, now):
        """Линк упал или скорость стала ниже обычной. Обычная — максимум, что видели; через сутки
        на новой скорости считаем её обычной (кабель или порт поменяли намеренно)."""
        base = S.meta_get("iface_base", {})
        hb = base.setdefault(name, {})
        out = {}
        for i in ifs:
            b = hb.setdefault(i["name"], {"speed": None, "seen_up": False, "low_since": None})
            if i["up"]:
                b["seen_up"] = True
            elif b["seen_up"]:
                out[f"if:{name}:{i['name']}:down"] = ("crit", f"{name}: линк {i['name']} упал", 60)
                continue
            sp = i.get("speed")
            if not sp:
                continue
            if b["speed"] is None or sp >= b["speed"]:
                b["speed"], b["low_since"] = sp, None
            else:
                b["low_since"] = b["low_since"] or now
                if now - b["low_since"] > 86400:
                    b["speed"], b["low_since"] = sp, None
                else:
                    out[f"if:{name}:{i['name']}:speed"] = ("warn", f"{name}: скорость линка {i['name']} упала "
                                                                   f"{b['speed']} → {sp} Мбит/с", 60)
        S.meta_set("iface_base", base)
        return out

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


ALERTS = Alerts()
