#!/usr/bin/env python3
"""Lumen agent: собирает метрики хоста (и по желанию Proxmox, Ceph, Patroni/etcd, бэкапы,
проверки сервисов, входы по SSH, статистику PostgreSQL, SMART дисков, ошибки журнала) и отправляет их на сервер.
Только стандартная библиотека Python."""
import json
import os
import platform
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CONFIG_PATH = os.environ.get("LUMEN_AGENT_CONFIG", "/etc/lumen/agent.json")
CFG = {
    "server": "http://127.0.0.1:8095",
    "token": "",
    "host": socket.gethostname(),
    "interval": 15,
    "modules": ["system"],
    "cluster_interval": 60,
    "backup_storage": "",
    "backup_interval": 300,
    "patroni_url": "",
    "etcd_urls": [],
    "checks": [],
    "checks_interval": 30,
    "ssh_interval": 60,
    "pg_interval": 30,
    "smart_interval": 1800,
    "ca_file": "",            # сертификат сервера Lumen для проверки HTTPS
}
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH) as f:
        CFG.update(json.load(f))

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs"}
WHOLE_DISK = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")


def read(path):
    with open(path) as f:
        return f.read()


def in_container():
    try:
        return b"container=" in open("/proc/1/environ", "rb").read()
    except OSError:
        return os.path.exists("/run/systemd/container")


CONTAINER = in_container()


def run_json(cmd, timeout=15):
    out = subprocess.run(cmd, capture_output=True, timeout=timeout, check=True).stdout
    return json.loads(out)


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- system

class Counters:
    """Хранит предыдущие значения счётчиков, чтобы считать скорости."""

    def __init__(self):
        self.prev = {}

    def rate(self, key, value, now):
        p = self.prev.get(key)
        self.prev[key] = (value, now)
        if not p or now <= p[1] or value < p[0]:
            return None
        return (value - p[0]) / (now - p[1])


C = Counters()

# Кластерные временные ряды текущего цикла: {"_ceph": {"osd.3": 1.0, ...}, ...}
SERIES = {}


def series(group, values):
    SERIES.setdefault(group, {}).update({k: v for k, v in values.items() if isinstance(v, (int, float))})


def cpu():
    f = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:9]]
    total, idle, iowait = sum(f), f[3], f[4]
    p = C.prev.get("cpu")
    C.prev["cpu"] = (total, idle, iowait)
    if not p or total <= p[0]:
        return None, None
    dt = total - p[0]
    busy = dt - (idle - p[1]) - (iowait - p[2])
    return round(100.0 * busy / dt, 1), round(100.0 * (iowait - p[2]) / dt, 1)


def memory():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    total = m["MemTotal"]
    avail = m.get("MemAvailable", m["MemFree"])
    swap_total = m.get("SwapTotal", 0)
    swap_used = swap_total - m.get("SwapFree", 0)
    return total, total - avail, swap_total, swap_used


def disks():
    out, seen = [], set()
    for line in read("/proc/mounts").splitlines():
        dev, mnt, fs = line.split()[:3]
        if fs not in REAL_FS or dev in seen or mnt.startswith(("/var/lib/docker", "/snap")):
            continue
        seen.add(dev)
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        size = st.f_blocks * st.f_frsize
        if size == 0:
            continue
        used = size - st.f_bfree * st.f_frsize
        out.append({"mount": mnt, "size": size, "used": used})
    return out


def net_ifaces():
    names = os.listdir("/sys/class/net")
    phys = [n for n in names if os.path.exists(f"/sys/class/net/{n}/device")]
    if phys:
        return phys
    return [n for n in names if n != "lo" and not n.startswith(("veth", "docker", "br-", "fw", "tap"))]


def link_mbit(n):
    try:
        v = int(read(f"/sys/class/net/{n}/speed"))
    except (OSError, ValueError):
        return None
    return v if v > 0 else None


def net(now):
    """Суммарные скорости и загрузка самого занятого физического канала в % от его скорости."""
    rx = tx = 0
    util = link = None
    for n in net_ifaces():
        try:
            r = int(read(f"/sys/class/net/{n}/statistics/rx_bytes"))
            t = int(read(f"/sys/class/net/{n}/statistics/tx_bytes"))
        except OSError:
            continue
        rx += r
        tx += t
        speed = None if CONTAINER else link_mbit(n)  # у veth в контейнере скорость условная
        rr, tr = C.rate("rx:" + n, r, now), C.rate("tx:" + n, t, now)
        if speed and rr is not None and tr is not None:
            u = 100.0 * max(rr, tr) * 8 / (speed * 1e6)
            if util is None or u > util:
                util, link = u, speed
    return C.rate("rx", rx, now), C.rate("tx", tx, now), util, link


def diskio(now):
    if CONTAINER:
        return None, None
    rd = wr = 0
    for line in read("/proc/diskstats").splitlines():
        f = line.split()
        if WHOLE_DISK.match(f[2]):
            rd += int(f[5]) * 512
            wr += int(f[9]) * 512
    return C.rate("dr", rd, now), C.rate("dw", wr, now)


def temperature():
    best = None
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return None
    for h in os.listdir(base):
        try:
            name = read(f"{base}/{h}/name").strip()
        except OSError:
            continue
        if name not in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal"):
            continue
        for fn in os.listdir(f"{base}/{h}"):
            if fn.startswith("temp") and fn.endswith("_input"):
                try:
                    t = int(read(f"{base}/{h}/{fn}")) / 1000.0
                except (OSError, ValueError):
                    continue
                best = t if best is None else max(best, t)
    return best


def info():
    osname = ""
    try:
        for line in read("/etc/os-release").splitlines():
            if line.startswith("PRETTY_NAME="):
                osname = line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return {
        "os": osname,
        "kernel": platform.release(),
        "cpus": os.cpu_count(),
        "uptime": float(read("/proc/uptime").split()[0]),
        "container": CONTAINER,
    }


def system(now):
    c, io = cpu()
    mt, mu, st, su = memory()
    rx, tx, util, link = net(now)
    dr, dw = diskio(now)
    l1, l5, l15 = os.getloadavg()
    m = {
        "cpu": c, "iowait": io,
        "mem": round(100.0 * mu / mt, 1), "mem_used": mu, "mem_total": mt,
        "swap": round(100.0 * su / st, 1) if st else 0.0,
        "load1": l1, "load5": l5,
        "net_rx": rx, "net_tx": tx,
        "net_util": round(util, 1) if util is not None else None, "link_mbit": link,
        "disk_r": dr, "disk_w": dw,
        "temp": temperature(),
    }
    return {k: v for k, v in m.items() if v is not None}


# ---------------------------------------------------------------- cluster modules

def mod_pve():
    res = run_json(["pvesh", "get", "/cluster/resources", "--output-format", "json"])
    nodes, guests, storage = [], [], []
    for r in res:
        t = r.get("type")
        if t == "node":
            nodes.append({"name": r["node"], "status": r.get("status"), "cpu": r.get("cpu"),
                          "mem": r.get("mem"), "maxmem": r.get("maxmem"), "uptime": r.get("uptime")})
        elif t in ("qemu", "lxc"):
            g = {k: r.get(k) for k in ("vmid", "name", "type", "node", "status", "cpu", "maxcpu",
                                       "mem", "maxmem", "disk", "maxdisk", "uptime", "template")}
            guests.append(g)
            if g["status"] == "running" and not g["template"]:
                now, vid = time.time(), g["vmid"]
                rates = {k: C.rate(f"vm{vid}:{k}", r.get(src) or 0, now)
                         for k, src in (("net_rx", "netin"), ("net_tx", "netout"),
                                        ("disk_r", "diskread"), ("disk_w", "diskwrite"))}
                g.update(rates)
                series(f"_vm{vid}", dict(rates, cpu=round(100 * (g["cpu"] or 0), 2),
                                         mem=round(100 * g["mem"] / g["maxmem"], 2) if g["maxmem"] else None,
                                         mem_used=g["mem"]))
        elif t == "storage" and r.get("status") == "available":
            storage.append({k: r.get(k) for k in ("storage", "node", "plugintype", "disk", "maxdisk", "shared")})
    # общий storage показываем один раз
    uniq, seen = [], set()
    for s in storage:
        key = s["storage"] if s.get("shared") else (s["storage"], s["node"])
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    guests.sort(key=lambda g: g["vmid"])
    return {"nodes": nodes, "guests": guests, "storage": uniq}


def mod_ceph():
    s = run_json(["ceph", "status", "-f", "json"], timeout=20)
    df = run_json(["ceph", "df", "-f", "json"], timeout=20)
    perf = run_json(["ceph", "osd", "perf", "-f", "json"], timeout=20)
    perf = perf.get("osdstats", perf).get("osd_perf_infos", [])
    osd_lat = {f"osd.{o['id']}": o["perf_stats"]["commit_latency_ms"] for o in sorted(perf, key=lambda o: o["id"])}
    h = s.get("health", {})
    pg = s.get("pgmap", {})
    om = s.get("osdmap", {})
    om = om.get("osdmap", om)  # старые версии вкладывают osdmap дважды
    out = {
        "health": h.get("status"),
        "checks": [{"name": k, "severity": v.get("severity"), "msg": v.get("summary", {}).get("message", "")}
                   for k, v in h.get("checks", {}).items()],
        "osd": {"total": om.get("num_osds"), "up": om.get("num_up_osds"), "in": om.get("num_in_osds")},
        "mons": s.get("quorum_names", []),
        "pgs": {"total": pg.get("num_pgs"), "states": pg.get("pgs_by_state", [])},
        "bytes": {"used": pg.get("bytes_used"), "total": pg.get("bytes_total")},
        "io": {"rd": pg.get("read_bytes_sec", 0), "wr": pg.get("write_bytes_sec", 0),
               "rd_ops": pg.get("read_op_per_sec", 0), "wr_ops": pg.get("write_op_per_sec", 0)},
        "pools": [{"name": p["name"], "stored": p["stats"].get("stored", p["stats"].get("bytes_used")),
                   "pct": round(100.0 * p["stats"].get("percent_used", 0), 2),
                   "avail": p["stats"].get("max_avail")} for p in df.get("pools", [])],
    }
    io = out["io"]
    used_pct = round(100.0 * out["bytes"]["used"] / out["bytes"]["total"], 3) if out["bytes"]["total"] else None
    series("_ceph", dict(osd_lat, io_rd=io["rd"], io_wr=io["wr"], ops_rd=io["rd_ops"], ops_wr=io["wr_ops"],
                         used_pct=used_pct))
    out["osd_lat"] = osd_lat
    return out


def prom_values(text, names):
    """Значения нужных метрик из текстового формата Prometheus (без меток)."""
    out = {}
    for line in text.splitlines():
        if line and line[0] != "#":
            k, _, v = line.partition(" ")
            if k in names:
                out[k] = float(v)
    return out


ETCD_METRICS = ("etcd_disk_wal_fsync_duration_seconds_sum", "etcd_disk_wal_fsync_duration_seconds_count",
                "etcd_disk_backend_commit_duration_seconds_sum", "etcd_disk_backend_commit_duration_seconds_count",
                "etcd_server_has_leader", "etcd_server_leader_changes_seen_total")


def avg_ms(key, total, count):
    """Средняя длительность операции по разнице счётчиков _sum/_count.
    Возвращает (значение, свежее ли оно). Если операций с прошлого раза не было
    (etcd простаивает), отдаёт последнее известное значение с fresh=False."""
    p = C.prev.get(key)
    if p and count == p[1]:
        return p[2], False
    avg = round((total - p[0]) / (count - p[1]) * 1000, 2) if p and count > p[1] else None
    C.prev[key] = (total, count, avg)
    return avg, avg is not None


def mod_patroni():
    out = {}
    pts = {}
    try:
        c = http_json(CFG["patroni_url"].rstrip("/") + "/cluster")
        out["scope"] = c.get("scope")
        out["members"] = [{k: m.get(k) for k in ("name", "role", "state", "host", "timeline", "lag")}
                          for m in c.get("members", [])]
        for m in out["members"]:
            if isinstance(m.get("lag"), (int, float)):
                pts["lag:" + m["name"]] = m["lag"]
    except Exception as e:
        out["error"] = str(e)[:200]
    etcd = {}
    for url in CFG["etcd_urls"]:
        ep = url.split("//")[-1]
        ip = ep.split(":")[0]
        t0 = time.time()
        try:
            h = http_json(url.rstrip("/") + "/health", timeout=5)
            st = {"ok": str(h.get("health")) == "true", "ms": round((time.time() - t0) * 1000, 1),
                  "err": h.get("reason", "")}
            pts["rtt:" + ip] = st["ms"]
        except Exception as e:
            st = {"ok": False, "ms": None, "err": str(e)[:120]}
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/metrics", timeout=5) as r:
                pm = prom_values(r.read().decode(), ETCD_METRICS)
            for k, name in (("fsync", "wal_fsync"), ("commit", "backend_commit")):
                v, fresh = avg_ms(f"{k}:{ip}", pm[f"etcd_disk_{name}_duration_seconds_sum"],
                                  pm[f"etcd_disk_{name}_duration_seconds_count"])
                st[k + "_ms"] = v
                if fresh:  # на график — только интервалы, где были операции
                    pts[f"{k}:{ip}"] = v
            st["leader_changes"] = int(pm.get("etcd_server_leader_changes_seen_total", 0))
            st["has_leader"] = pm.get("etcd_server_has_leader") == 1
        except Exception:
            pass  # /metrics может быть закрыт — тогда только health
        etcd[ep] = st
    out["etcd"] = etcd
    series("_etcd", pts)
    return out


CERT_CACHE = {}  # (host, port) -> (когда проверяли, notAfter)


def cert_days(url):
    """Сколько дней осталось до истечения сертификата HTTPS (раз в час на адрес)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or not u.hostname:
        return None
    key = (u.hostname, u.port or 443)
    hit = CERT_CACHE.get(key)
    if not hit or time.time() - hit[0] > 3600:
        ctx = ssl._create_unverified_context()  # срок интересен и у самоподписанных
        with socket.create_connection(key, timeout=5) as sock, ctx.wrap_socket(sock, server_hostname=u.hostname) as t:
            der = t.getpeercert(binary_form=True)
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
            f.write(ssl.DER_cert_to_PEM_cert(der))
            f.flush()
            not_after = ssl.cert_time_to_seconds(ssl._ssl._test_decode_cert(f.name)["notAfter"])
        hit = CERT_CACHE[key] = (time.time(), not_after)
    return round((hit[1] - time.time()) / 86400, 1)


def check_one(c):
    t = c.get("type", "http")
    res = {"name": c["name"], "type": t, "target": c.get("url") or f"{c.get('host')}:{c.get('port', '')}".rstrip(":"),
           "ok": False, "ms": None, "err": ""}
    timeout = c.get("timeout", 5)
    t0 = time.time()
    try:
        if t == "http":
            ctx = ssl._create_unverified_context() if c.get("insecure") else None
            req = urllib.request.Request(c["url"], headers={"User-Agent": "lumen-check"})
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    code = r.status
                    body = r.read(65536).decode(errors="replace")
            except urllib.error.HTTPError as e:
                code, body = e.code, ""
            res["code"] = code
            ok_codes = c.get("expect_status")
            res["ok"] = code in ok_codes if ok_codes else code < 400
            if res["ok"] and c.get("expect_text") and c["expect_text"] not in body:
                res["ok"], res["err"] = False, "нет ожидаемого текста"
            elif not res["ok"]:
                res["err"] = f"HTTP {code}"
        elif t == "tcp":
            socket.create_connection((c["host"], c["port"]), timeout=timeout).close()
            res["ok"] = True
        elif t == "ping":
            p = subprocess.run(["ping", "-c", "1", "-W", str(timeout), c["host"]], capture_output=True, text=True,
                               timeout=timeout + 2)
            m = re.search(r"time=([\d.]+)", p.stdout)
            res["ok"] = p.returncode == 0
            if m:
                res["ms"] = float(m.group(1))
            if not res["ok"]:
                res["err"] = "нет ответа"
        else:
            res["err"] = "неизвестный тип"
    except Exception as e:
        res["err"] = (getattr(e, "reason", None) and str(e.reason) or str(e))[:120]
    if res["ok"] and res["ms"] is None:
        res["ms"] = round((time.time() - t0) * 1000, 1)
    if t == "http" and res["ok"]:
        try:
            res["cert_days"] = cert_days(c["url"])
        except Exception:
            pass
    return res


# Список проверок задаётся на сервере (в настройках Lumen); пока сервер его не прислал,
# используется локальный список из agent.json.
SERVER_CHECKS = None


def active_checks():
    return SERVER_CHECKS if SERVER_CHECKS is not None else CFG["checks"]


def mod_checks():
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(check_one, active_checks()))
    series("_checks", {r["name"]: r["ms"] for r in results if r["ok"]})
    return results


def mod_backups():
    node = socket.gethostname()
    tasks = run_json(["pvesh", "get", "/cluster/tasks", "--output-format", "json"])
    tasks = [{k: t.get(k) for k in ("upid", "node", "starttime", "endtime", "status")}
             for t in tasks if t.get("type") == "vzdump"]
    tasks.sort(key=lambda t: t["starttime"], reverse=True)
    guests = []
    if CFG["backup_storage"]:
        content = run_json(["pvesh", "get", f"/nodes/{node}/storage/{CFG['backup_storage']}/content",
                            "--content", "backup", "--output-format", "json"], timeout=60)
        last = {}
        for b in content:
            v = b.get("vmid")
            if v is None:
                continue
            g = last.setdefault(v, {"vmid": v, "last": 0, "count": 0, "size": 0, "name": ""})
            g["count"] += 1
            if b.get("ctime", 0) > g["last"]:
                g.update(last=b["ctime"], size=b.get("size", 0), name=b.get("notes") or "",
                         verified=(b.get("verification") or {}).get("state"))
        guests = sorted(last.values(), key=lambda g: g["vmid"])
    return {"tasks": tasks[:15], "guests": guests, "storage": CFG["backup_storage"]}


SSH_STATE = {"pending": []}
SSH_ACCEPTED = re.compile(r"Accepted (\S+) for (\S+) from (\S+) port")
JOURNAL = {"cursor": None}


def journal(args):
    out = subprocess.run(["journalctl", "--no-pager", "-q", "--show-cursor"] + args,
                         capture_output=True, text=True, timeout=20).stdout.splitlines()
    cursor = None
    if out and out[-1].startswith("-- cursor: "):
        cursor = out.pop()[len("-- cursor: "):]
    return out, cursor


def read_journal():
    """Все новые записи журнала с прошлого вызова — одним проходом для модулей ssh и logs.
    Без фильтров: тогда курсор сдвигается всегда. С фильтром (-t sshd, -p 3) при отсутствии
    подходящих записей курсор не выдаётся, и каждый следующий вызов заново перечитывал журнал
    с прежнего места — в VM с диском в Ceph это давало ~15 МБ/с чтения по сети."""
    now = time.time()
    if JOURNAL["cursor"] is None:
        _, JOURNAL["cursor"] = journal(["-n", "0"])  # начинаем с текущего конца журнала
        LOG_STATE["ts"] = now
        return
    lines, cursor = journal(["--after-cursor", JOURNAL["cursor"], "-o", "json", "-n", "5000",
                             "--output-fields=MESSAGE,PRIORITY,SYSLOG_IDENTIFIER,_COMM"])
    if cursor:
        JOURNAL["cursor"] = cursor
    want_ssh, want_logs = "ssh" in CFG["modules"], "logs" in CFG["modules"]
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("MESSAGE")
        if isinstance(msg, list):  # journald отдаёт не-UTF8 сообщения массивом байт
            msg = bytes(msg).decode(errors="replace")
        msg = str(msg or "").strip()
        ident = e.get("SYSLOG_IDENTIFIER") or e.get("_COMM") or "?"
        ts = int(e.get("__REALTIME_TIMESTAMP") or now * 1e6) / 1e6
        if want_ssh and ident in ("sshd", "sshd-session"):
            m = SSH_ACCEPTED.search(msg)
            if m:
                SSH_STATE["pending"].append({"ts": ts, "method": m.group(1), "user": m.group(2), "ip": m.group(3)})
        if want_logs and str(e.get("PRIORITY", "6")).isdigit() and int(e["PRIORITY"]) <= 3:
            LOG_STATE["count"] += 1
            r = LOG_STATE["recent"].setdefault((ident, re.sub(r"\d+", "#", msg)[:160]), {"ident": ident, "n": 0})
            r.update(n=r["n"] + 1, last=ts, msg=msg[:300])


def mod_ssh():
    """Новые успешные входы по SSH (только после запуска агента)."""
    read_journal()
    return None  # входы уходят отдельным полем ssh_logins


# Счётчики SMART, рост которых означает проблему с диском или кабелем
SMART_COUNTERS = {5: "realloc", 197: "pending", 198: "uncorrectable", 187: "reported_uncorrect", 199: "crc"}


def smart_disk(dev):
    p = subprocess.run(["smartctl", "-j", "-a", "/dev/" + dev], capture_output=True, text=True, timeout=60)
    d = json.loads(p.stdout)
    if (d.get("smartctl") or {}).get("exit_status", 0) & 3:  # не открылся или не ответил
        return None
    attrs = {a["id"]: a for a in (d.get("ata_smart_attributes") or {}).get("table", [])}
    ssd = d.get("rotation_rate") == 0
    out = {"dev": dev, "model": d.get("model_name") or d.get("model_family") or "", "serial": d.get("serial_number"),
           "size": (d.get("user_capacity") or {}).get("bytes"), "ssd": ssd,
           "passed": (d.get("smart_status") or {}).get("passed"),
           "temp": (d.get("temperature") or {}).get("current"),
           "hours": (d.get("power_on_time") or {}).get("hours"),
           "err_log": ((d.get("ata_smart_error_log") or {}).get("summary") or {}).get("count")}
    for aid, name in SMART_COUNTERS.items():
        if aid in attrs:
            out[name] = attrs[aid]["raw"]["value"]
    nv = d.get("nvme_smart_health_information_log")
    if nv:
        out["wear"] = nv.get("percentage_used")
        out["uncorrectable"] = nv.get("media_errors")
    elif ssd:
        # остаток ресурса: у разных производителей в разных атрибутах (сырое значение или нормализованное)
        for aid, raw in ((231, True), (169, True), (202, False), (233, False), (177, False)):
            a = attrs.get(aid)
            v = a and (a["raw"]["value"] if raw else a["value"])
            if a and 0 < v <= 100:
                out["wear"] = 100 - v
                break
    return out


def mod_smart():
    names = [l.split()[0] for l in subprocess.run(["lsblk", "-dno", "NAME,TYPE"], capture_output=True, text=True,
                                                    timeout=10).stdout.splitlines() if l.endswith(" disk")]
    disks = []
    for n in names:
        if WHOLE_DISK.match(n):
            try:
                d = smart_disk(n)
            except Exception as e:
                d = {"dev": n, "error": str(e)[:120]}
            if d:
                disks.append(d)
    return {"disks": disks}


LOG_STATE = {"ts": 0, "count": 0, "recent": {}, "rate": None}
LOG_WINDOW = 3600


def mod_logs():
    """Ошибки (priority err и выше) в журнале: скорость в минуту и самые частые за последний час."""
    first = JOURNAL["cursor"] is None
    read_journal()
    if first:
        return None
    now = time.time()
    recent = LOG_STATE["recent"]
    for k in [k for k, r in recent.items() if r["last"] < now - LOG_WINDOW]:
        del recent[k]
    LOG_STATE["rate"] = round(LOG_STATE["count"] * 60 / max(1.0, now - LOG_STATE["ts"]), 2)
    LOG_STATE["count"], LOG_STATE["ts"] = 0, now
    top = sorted(recent.values(), key=lambda r: (-r["n"], -r["last"]))[:8]
    return {"hour": sum(r["n"] for r in recent.values()), "top": top}


PG_QUERY = """SELECT json_build_object(
  'version', current_setting('server_version'),
  'in_recovery', pg_is_in_recovery(),
  'max_conn', current_setting('max_connections')::int,
  'conn_total', (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'),
  'conn_active', (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND state = 'active'),
  'conn_idle_tx', (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'
                   AND state LIKE 'idle in transaction%'),
  'oldest_xact_sec', (SELECT coalesce(extract(epoch FROM max(now() - xact_start)), 0) FROM pg_stat_activity
                      WHERE backend_type = 'client backend' AND xact_start IS NOT NULL),
  'dbs', (SELECT json_agg(json_build_object('name', datname, 'size', pg_database_size(datname)) ORDER BY datname)
          FROM pg_database WHERE NOT datistemplate),
  'xact', (SELECT sum(xact_commit + xact_rollback) FROM pg_stat_database),
  'blks_hit', (SELECT sum(blks_hit) FROM pg_stat_database),
  'blks_read', (SELECT sum(blks_read) FROM pg_stat_database),
  'deadlocks', (SELECT sum(deadlocks) FROM pg_stat_database),
  'checkpoints', (SELECT num_timed + num_requested FROM pg_stat_checkpointer)
)"""


def mod_postgres():
    try:
        # смена пользователя без runuser/PAM, чтобы не засорять журнал входов каждые 30 секунд
        out = subprocess.run(["psql", "-XAtq", "-d", "postgres", "-c", PG_QUERY], user="postgres", group="postgres",
                             extra_groups=[], env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                             capture_output=True, text=True, timeout=20, cwd="/")
        if out.returncode:
            raise RuntimeError(out.stderr.strip()[-200:] or f"psql exit {out.returncode}")
        d = json.loads(out.stdout)
    except Exception as e:
        return {"error": str(e)[:200]}
    now = time.time()
    d["db_size"] = sum(x["size"] for x in d.get("dbs") or [])
    d["tps"] = C.rate("pg:xact", d["xact"] or 0, now)
    hit, rd = C.rate("pg:hit", d["blks_hit"] or 0, now), C.rate("pg:read", d["blks_read"] or 0, now)
    d["cache_hit"] = round(100.0 * hit / (hit + rd), 2) if hit is not None and rd is not None and hit + rd else None
    d["checkpoints_per_h"] = (C.rate("pg:ckpt", d["checkpoints"] or 0, now) or 0) * 3600
    series("_pg_" + CFG["host"], {k: d.get(k) for k in ("conn_total", "conn_active", "conn_idle_tx", "tps",
                                                          "cache_hit", "db_size", "oldest_xact_sec")})
    return d


MODULES = {
    "pve": (mod_pve, "cluster_interval"),
    "ceph": (mod_ceph, "cluster_interval"),
    "patroni": (mod_patroni, "interval"),
    "backups": (mod_backups, "backup_interval"),
    "checks": (mod_checks, "checks_interval"),
    "ssh": (mod_ssh, "ssh_interval"),
    "postgres": (mod_postgres, "pg_interval"),
    "smart": (mod_smart, "smart_interval"),
    "logs": (mod_logs, "interval"),
}
CLUSTER_KEY = {"checks": "checks:{host}", "postgres": "postgres:{host}", "smart": "smart:{host}", "logs": "logs:{host}"}  # у каждого хоста свой ключ


PUSH_CTX = ssl.create_default_context(cafile=CFG["ca_file"]) if CFG["ca_file"] else None


def push(payload):
    req = urllib.request.Request(CFG["server"].rstrip("/") + "/api/push",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + CFG["token"]})
    with urllib.request.urlopen(req, timeout=10, context=PUSH_CTX) as r:
        return json.loads(r.read() or b"{}")


def main():
    global SERVER_CHECKS
    last_run = {}
    system(time.time())  # первые значения счётчиков
    errors = 0
    while True:
        t0 = time.time()
        payload = {"host": CFG["host"], "ts": int(t0), "interval": CFG["interval"],
                   "metrics": system(t0), "disks": disks(), "info": info(), "cluster": {}}
        modules = list(CFG["modules"])
        if active_checks() and "checks" not in modules:
            modules.append("checks")  # сервер назначил этот хост выполнять проверки
        for name in modules:
            if name not in MODULES or (name == "checks" and not active_checks()):
                continue
            fn, ivkey = MODULES[name]
            if t0 - last_run.get(name, 0) < CFG[ivkey]:
                continue
            last_run[name] = t0
            try:
                res = fn()
                if res is not None:
                    payload["cluster"][CLUSTER_KEY.get(name, name).format(host=CFG["host"])] = res
            except Exception as e:
                print(f"module {name}: {e!r}", file=sys.stderr, flush=True)
        if LOG_STATE["rate"] is not None:
            payload["metrics"]["log_err"] = LOG_STATE["rate"]
            LOG_STATE["rate"] = None
        payload["series"] = dict(SERIES)
        SERIES.clear()
        if CFG["checks"]:
            payload["checks_local"] = CFG["checks"]
        if SSH_STATE["pending"]:
            payload["ssh_logins"] = SSH_STATE["pending"]
        try:
            reply = push(payload)
            SSH_STATE["pending"] = []
            if "checks" in reply:
                SERVER_CHECKS = reply["checks"]
            errors = 0
        except Exception as e:
            errors += 1
            if errors in (1, 10) or errors % 100 == 0:
                print(f"push failed ({errors}): {e!r}", file=sys.stderr, flush=True)
            for name in payload["cluster"]:  # повторить кластерные модули в следующем цикле
                last_run.pop(name.split(":")[0], None)
        time.sleep(max(1.0, CFG["interval"] - (time.time() - t0)))


if __name__ == "__main__":
    main()
