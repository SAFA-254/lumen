"""Агент (agent/luxmon-agent.py): разбор /proc, /sys, журнала и вывода pvesh, ceph, smartctl, apt
на сохранённых образцах. Файлы и команды подменяются, поэтому тесты идут и не на Linux."""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_agent():
    """Агент при загрузке читает конфиг и зовёт os.sysconf; resource и sysconf есть только в Unix."""
    patches = [mock.patch.dict(os.environ, {"LUXMON_AGENT_CONFIG": os.path.join(tempfile.gettempdir(), "нет-такого.json")})]
    if not hasattr(os, "sysconf"):
        patches.append(mock.patch.object(os, "sysconf", lambda name: 100 if name == "SC_CLK_TCK" else 4096, create=True))
    if "resource" not in sys.modules:
        try:
            import resource  # noqa: F401
        except ImportError:
            patches.append(mock.patch.dict(sys.modules, {"resource": types.ModuleType("resource")}))
    for p in patches:
        p.start()
    try:
        spec = importlib.util.spec_from_file_location("luxmon_agent", os.path.join(ROOT, "agent", "luxmon-agent.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for p in reversed(patches):
            p.stop()


agent = load_agent()


class Base(unittest.TestCase):
    def setUp(self):
        agent.C.prev.clear()
        agent.SERIES.clear()
        agent.DISK_OPS.clear()
        agent.SSH_STATE["pending"] = []
        agent.LOG_STATE.update(ts=0, count=0, recent={}, rate=None)
        agent.JOURNAL.update(cursor=None, saved=None)
        agent.JF.update(proc=None, dropped=0, started=0, retry=0)
        agent.JF["q"].clear()
        for name, value in (("CONTAINER", False),):
            p = mock.patch.object(agent, name, value)
            p.start()
            self.addCleanup(p.stop)

    def files(self, d):
        """Подменить чтение файлов: путь -> содержимое; остальное — как будто файла нет."""
        def read(path):
            if path not in d:
                raise FileNotFoundError(path)
            return d[path]
        p = mock.patch.object(agent, "read", read)
        p.start()
        self.addCleanup(p.stop)


def done(stdout):
    return types.SimpleNamespace(stdout=stdout, returncode=0)


class System(Base):
    def test_cpu_from_two_samples(self):
        self.files({"/proc/stat": "cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3\n"})
        self.assertEqual(agent.cpu(), (None, None))  # по одному замеру скорость не посчитать
        self.files({"/proc/stat": "cpu  200 0 200 1300 300 0 0 0 0 0\n"})
        self.assertEqual(agent.cpu(), (20.0, 20.0))

    def test_memory_uses_available(self):
        self.files({"/proc/meminfo": "MemTotal:  8000000 kB\nMemFree:  1000000 kB\nMemAvailable:  6000000 kB\n"
                                     "SwapTotal:  1000000 kB\nSwapFree:  750000 kB\nHugePages_Total:  0\n"})
        self.assertEqual(agent.memory(), (8000000 * 1024, 2000000 * 1024, 1000000 * 1024, 250000 * 1024))

    def test_diskio_whole_disks_and_rbd_latency(self):
        def stats(sda, rbd):
            return (f"   8       0 sda {sda}\n   8       1 sda1 99 0 99 99 99 0 99 99 0 0 0\n"
                    f" 252       0 rbd0 {rbd}\n   7       0 loop0 5 0 5 5 5 0 5 5 0 0 0\n")
        self.files({"/proc/diskstats": stats("100 0 1000 50 100 0 2000 50 0 0 0", "0 0 0 0 0 0 0 0 0 0 0")})
        agent.diskio(1000.0)
        self.files({"/proc/diskstats": stats("150 0 3000 150 150 0 6000 150 0 0 0", "100 0 99999 400 0 0 0 0 0 0 0")})
        rd, wr, lat = agent.diskio(1010.0)
        # rbd на узле — диски гостей, их байты уже идут по сети: в сумму не входят, а задержка учитывается
        self.assertEqual(rd, 2000 * 512 / 10)
        self.assertEqual(wr, 4000 * 512 / 10)
        self.assertEqual(lat, 4.0)

    def test_diskio_ignores_few_operations(self):
        self.files({"/proc/diskstats": "8 0 sda 10 0 10 10 10 0 10 10 0 0 0\n"})
        agent.diskio(1.0)
        self.files({"/proc/diskstats": "8 0 sda 20 0 20 900 20 0 20 900 0 0 0\n"})
        self.assertIsNone(agent.diskio(2.0)[2])

    def test_mem_pressure(self):
        self.files({"/proc/pressure/memory": "some avg10=0.00 avg60=1.50 avg300=0.10 total=123\n"
                                             "full avg10=0.00 avg60=0.70 avg300=0.00 total=45\n"})
        self.assertEqual(agent.mem_pressure(), 1.5)
        self.files({})
        self.assertIsNone(agent.mem_pressure())

    def test_passwd_digest_ignores_passwords(self):
        base = "root:x:0:0:root:/root:/bin/bash\nbob:x:1000:1000::/home/bob:/bin/bash\n"
        self.files({"/etc/passwd": base})
        d = agent.passwd_digest()
        self.assertEqual(d["users"], ["bob", "root"])
        self.files({"/etc/passwd": base.replace("bob:x:", "bob:$6$hash:")})
        self.assertEqual(agent.passwd_digest(), d)
        self.files({"/etc/passwd": base.replace("/home/bob:/bin/bash", "/home/bob:/bin/sh")})
        self.assertNotEqual(agent.passwd_digest()["hash"], d["hash"])

    def test_pve_quorate(self):
        self.files({"/etc/pve/.members": json.dumps({"cluster": {"name": "c", "quorate": 1}})})
        self.assertIs(agent.pve_quorate(), True)
        self.files({"/etc/pve/.members": "{"})
        self.assertIsNone(agent.pve_quorate())
        self.files({})
        self.assertIsNone(agent.pve_quorate())


class Ssh(Base):
    def test_open_connections_from_proc_net(self):
        self.files({
            "/proc/net/tcp": "  sl  local_address rem_address   st\n"
                             "   0: 0100007F:0016 0500000A:C350 01 0\n"     # к sshd, установлено
                             "   1: 00000000:0016 00000000:0000 0A 0\n"     # LISTEN
                             "   2: 0100007F:1F90 0500000A:C351 01 0\n",    # не порт 22
            "/proc/net/tcp6": "  sl  local_address rem_address   st\n"
                              "   0: 00000000000000000000000000000000:0016 0000000000000000FFFF00000600000A:0050 01 0\n",
        })
        self.assertEqual(agent.ssh_open(), [["10.0.0.5", 50000], ["10.0.0.6", 80]])

    def journal_lines(self, *entries):
        return [json.dumps(e) if isinstance(e, dict) else e for e in entries]

    def test_journal_ssh_and_errors(self):
        agent.JOURNAL["cursor"] = "c1"
        ts = "1700000000000000"
        lines = self.journal_lines(
            {"MESSAGE": "Accepted publickey for root from 10.0.0.5 port 50000 ssh2: ED25519 SHA256:abc",
             "SYSLOG_IDENTIFIER": "sshd", "__REALTIME_TIMESTAMP": ts},
            {"MESSAGE": "Disconnected from user root 10.0.0.5 port 50000", "SYSLOG_IDENTIFIER": "sshd-session"},
            # несуществующий пользователь пишется дважды — попытка должна посчитаться один раз
            {"MESSAGE": "Invalid user admin from 1.2.3.4 port 4444", "SYSLOG_IDENTIFIER": "sshd"},
            {"MESSAGE": "Failed password for invalid user admin from 1.2.3.4 port 4444", "SYSLOG_IDENTIFIER": "sshd"},
            {"MESSAGE": "Failed password for root from 1.2.3.4 port 4445", "SYSLOG_IDENTIFIER": "sshd"},
            {"MESSAGE": "Accepted password for root from 9.9.9.9 port 1", "SYSLOG_IDENTIFIER": "cron"},  # не sshd
            {"MESSAGE": "disk 12 error", "SYSLOG_IDENTIFIER": "kernel", "PRIORITY": "3"},
            {"MESSAGE": "disk 34 error", "SYSLOG_IDENTIFIER": "kernel", "PRIORITY": "3"},
            {"MESSAGE": [0xFF, 0x41], "_COMM": "weird", "PRIORITY": "2"},
            {"MESSAGE": "просто информация", "SYSLOG_IDENTIFIER": "kernel", "PRIORITY": "6"},
            {"MESSAGE": "без приоритета", "SYSLOG_IDENTIFIER": "kernel", "__CURSOR": "c2"},
            "не json",
        )
        with mock.patch.dict(agent.CFG, modules=["system", "ssh", "logs"]), \
                mock.patch.object(agent, "journal_lines", return_value=lines), \
                mock.patch.object(agent, "ssh_key_names", return_value={"SHA256:abc": "me@laptop"}):
            agent.read_journal()
        self.assertEqual(agent.JOURNAL["cursor"], "c2")
        ev = agent.SSH_STATE["pending"]
        self.assertEqual([e["kind"] for e in ev], ["in", "out", "fail", "fail"])
        self.assertEqual(ev[0], {"ts": 1700000000.0, "kind": "in", "method": "publickey", "user": "root",
                                 "ip": "10.0.0.5", "port": 50000, "key": "me@laptop"})
        self.assertEqual((ev[2]["user"], ev[2]["method"]), ("admin", "invalid user"))
        self.assertEqual((ev[3]["user"], ev[3]["method"], ev[3]["port"]), ("root", "password", 4445))
        # ошибки: числа в тексте не делят одну и ту же ошибку на разные
        self.assertEqual(agent.LOG_STATE["count"], 3)
        recent = agent.LOG_STATE["recent"]
        self.assertEqual(recent[("kernel", "disk # error")]["n"], 2)
        self.assertIn(("weird", "�A"), recent)

    def popen(self, poll=None, returncode=None):
        proc = types.SimpleNamespace(stdout=iter([]), poll=lambda: poll, returncode=returncode)
        return mock.patch.object(agent.subprocess, "Popen", return_value=proc)

    def test_first_read_starts_at_journal_end(self):
        with self.popen() as p:
            self.assertEqual(agent.journal_lines(1000), [])
        args = p.call_args[0][0]
        self.assertIn("-f", args)
        self.assertEqual(args[-2:], ["-n", "0"])

    def test_follows_from_saved_cursor(self):
        agent.JOURNAL["cursor"] = "s=1"
        with self.popen() as p:
            agent.journal_lines(1000)
        self.assertEqual(p.call_args[0][0][-2:], ["--after-cursor", "s=1"])

    def test_rotated_cursor_resets(self):
        # journalctl сразу вышел с ошибкой — курсор устарел; перезапуск не раньше чем через минуту
        agent.JOURNAL["cursor"] = "old"
        agent.JF.update(proc=types.SimpleNamespace(poll=lambda: 1, returncode=1), started=1000)
        with self.popen() as p:
            agent.journal_lines(1005)
            p.assert_not_called()
            agent.journal_lines(1066)
        self.assertIsNone(agent.JOURNAL["cursor"])
        self.assertEqual(p.call_args[0][0][-2:], ["-n", "0"])

    def test_killed_journalctl_keeps_cursor(self):
        agent.JOURNAL["cursor"] = "s=1"
        agent.JF.update(proc=types.SimpleNamespace(poll=lambda: -15, returncode=-15), started=1000)
        agent.journal_lines(1005)
        self.assertEqual(agent.JOURNAL["cursor"], "s=1")

    def test_reader_queue_bounded(self):
        q = agent.collections.deque()
        with mock.patch.object(agent, "JF_MAX", 3):
            agent.jf_reader(types.SimpleNamespace(stdout=iter(["1", "2", "3", "4", "5"])), q)
        self.assertEqual(list(q), ["3", "4", "5"])
        self.assertEqual(agent.JF["dropped"], 2)

    def test_lines_drained_from_queue(self):
        agent.JF.update(proc=types.SimpleNamespace(poll=lambda: None))
        agent.JF["q"].extend(["a", "b"])
        self.assertEqual(agent.journal_lines(1000), ["a", "b"])
        self.assertEqual(agent.journal_lines(1001), [])


class Etcd(Base):
    def test_prom_values(self):
        text = ("# HELP etcd_server_has_leader x\n# TYPE etcd_server_has_leader gauge\n"
                "etcd_server_has_leader 1\n"
                'etcd_disk_wal_fsync_duration_seconds_bucket{le="0.001"} 5\n'
                "etcd_disk_wal_fsync_duration_seconds_sum 0.5\n\n")
        self.assertEqual(agent.prom_values(text, agent.ETCD_METRICS),
                         {"etcd_server_has_leader": 1.0, "etcd_disk_wal_fsync_duration_seconds_sum": 0.5})

    def test_avg_ms_keeps_last_value_when_idle(self):
        self.assertEqual(agent.avg_ms("f", 10.0, 100), (None, False))
        self.assertEqual(agent.avg_ms("f", 10.5, 110), (50.0, True))
        self.assertEqual(agent.avg_ms("f", 10.5, 110), (50.0, False))  # операций не было — не новая точка


class Smart(Base):
    def smart(self, data):
        with mock.patch.object(agent.subprocess, "run", return_value=done(json.dumps(data))):
            return agent.smart_disk("sda")

    def test_sata_ssd(self):
        d = self.smart({"model_name": "Samsung SSD", "serial_number": "S1", "rotation_rate": 0,
                        "user_capacity": {"bytes": 500}, "smart_status": {"passed": True},
                        "temperature": {"current": 35}, "power_on_time": {"hours": 1000},
                        "ata_smart_error_log": {"summary": {"count": 0}},
                        "ata_smart_attributes": {"table": [
                            {"id": 5, "value": 100, "raw": {"value": 0}},
                            {"id": 197, "value": 100, "raw": {"value": 2}},
                            {"id": 177, "value": 97, "raw": {"value": 12}}]}})
        self.assertEqual(d["realloc"], 0)
        self.assertEqual(d["pending"], 2)
        self.assertEqual(d["wear"], 3)  # 177 — нормализованное значение: осталось 97 %
        self.assertTrue(d["ssd"])
        self.assertEqual((d["temp"], d["hours"], d["err_log"]), (35, 1000, 0))

    def test_nvme(self):
        d = self.smart({"model_name": "NVMe", "rotation_rate": 0,
                        "nvme_smart_health_information_log": {"percentage_used": 4, "media_errors": 1}})
        self.assertEqual((d["wear"], d["uncorrectable"]), (4, 1))

    def test_hdd_has_no_wear(self):
        d = self.smart({"model_name": "HDD", "rotation_rate": 7200,
                        "ata_smart_attributes": {"table": [{"id": 177, "value": 50, "raw": {"value": 1}}]}})
        self.assertNotIn("wear", d)
        self.assertFalse(d["ssd"])

    def test_device_not_opened(self):
        self.assertIsNone(self.smart({"smartctl": {"exit_status": 2}}))


class Updates(Base):
    APT = ("NOTE: This is only a simulation!\n"
           "Inst libssl3 [3.0.11-1] (3.0.13-1 Debian-Security:12/stable-security [amd64])\n"
           "Inst vim [2:9.0-1] (2:9.0-2 Debian:12/stable [amd64])\n"
           "Conf libssl3 (3.0.13-1 Debian-Security:12/stable-security [amd64])\n")

    def updates(self, boot, exists=()):
        real_exists = os.path.exists
        with mock.patch.object(agent.subprocess, "run", return_value=done(self.APT)), \
                mock.patch.object(agent.os.path, "exists", lambda p: p in exists or (
                    real_exists(p) and not p.startswith("/var/run"))), \
                mock.patch.object(agent.os, "listdir", lambda p: boot if p == "/boot" else []), \
                mock.patch.object(agent.platform, "release", return_value="6.8.12-4-pve"):
            return agent.mod_updates()

    def test_count_and_security(self):
        r = self.updates([])
        self.assertEqual((r["count"], r["security"], r["packages"]), (2, 1, ["libssl3", "vim"]))
        self.assertFalse(r["reboot"])

    def test_new_kernel_compared_numerically(self):
        # по строкам 6.8.12-10 < 6.8.12-4 — сравнивать надо по числам
        r = self.updates(["vmlinuz-6.8.12-10-pve", "vmlinuz-6.8.12-4-pve", "initrd.img-6.8.12-10-pve"])
        self.assertTrue(r["reboot"])
        self.assertIn("6.8.12-10-pve", r["reason"])
        self.assertFalse(self.updates(["vmlinuz-6.8.12-4-pve", "vmlinuz-6.8.12-3-pve"])["reboot"])

    def test_reboot_required_file(self):
        self.files({"/var/run/reboot-required.pkgs": "libc6\nsystemd\n"})
        r = self.updates([], exists=("/var/run/reboot-required",))
        self.assertEqual((r["reboot"], r["reason"]), (True, "libc6, systemd"))


class Cluster(Base):
    def test_pve(self):
        res = [
            {"type": "node", "node": "pve1", "status": "online", "cpu": 0.1, "mem": 1, "maxmem": 2, "uptime": 5},
            {"type": "qemu", "vmid": 105, "name": "db", "node": "pve2", "status": "running", "cpu": 0.25,
             "maxcpu": 4, "mem": 512, "maxmem": 1024, "netin": 1000, "netout": 0, "diskread": 0, "diskwrite": 0},
            {"type": "lxc", "vmid": 101, "name": "tpl", "node": "pve1", "status": "stopped", "template": 1},
            {"type": "storage", "storage": "ceph", "node": "pve1", "status": "available", "shared": 1},
            {"type": "storage", "storage": "ceph", "node": "pve2", "status": "available", "shared": 1},
            {"type": "storage", "storage": "local", "node": "pve1", "status": "available", "shared": 0},
            {"type": "storage", "storage": "local", "node": "pve2", "status": "available", "shared": 0},
            {"type": "storage", "storage": "nfs", "node": "pve1", "status": "unknown", "shared": 1},
        ]
        with mock.patch.object(agent, "run_json", return_value=res):
            out = agent.mod_pve()
        self.assertEqual([n["name"] for n in out["nodes"]], ["pve1"])
        self.assertEqual([g["vmid"] for g in out["guests"]], [101, 105])
        # общий storage — один раз, локальный — у каждого узла, недоступный не показывается
        self.assertEqual(sorted((s["storage"], s["node"]) for s in out["storage"]),
                         [("ceph", "pve1"), ("local", "pve1"), ("local", "pve2")])
        self.assertEqual(agent.SERIES["_vm105"], {"cpu": 25.0, "mem": 50.0, "mem_used": 512})
        self.assertNotIn("_vm101", agent.SERIES)

    def ceph(self, status):
        df = {"pools": [{"name": "rbd", "stats": {"stored": 100, "percent_used": 0.125, "max_avail": 900}}]}
        perf = {"osdstats": {"osd_perf_infos": [{"id": 1, "perf_stats": {"commit_latency_ms": 7}},
                                                {"id": 0, "perf_stats": {"commit_latency_ms": 3}}]}}

        def run_json(cmd, timeout=15):
            return {"status": status, "df": df, "osd": perf}[cmd[1]]
        with mock.patch.object(agent, "run_json", run_json), mock.patch.dict(agent.RBD, thread=object(), ts=0):
            return agent.mod_ceph()

    def test_ceph(self):
        out = self.ceph({"health": {"status": "HEALTH_WARN", "checks": {
                             "OSD_DOWN": {"severity": "HEALTH_WARN", "summary": {"message": "1 osds down"}}}},
                         "osdmap": {"osdmap": {"num_osds": 3, "num_up_osds": 2, "num_in_osds": 3}},  # старый формат
                         "quorum_names": ["a", "b"],
                         "pgmap": {"num_pgs": 128, "bytes_used": 25, "bytes_total": 100, "read_bytes_sec": 10}})
        self.assertEqual(out["health"], "HEALTH_WARN")
        self.assertEqual(out["checks"], [{"name": "OSD_DOWN", "severity": "HEALTH_WARN", "msg": "1 osds down"}])
        self.assertEqual(out["osd"], {"total": 3, "up": 2, "in": 3})
        self.assertEqual(out["pools"], [{"name": "rbd", "stored": 100, "pct": 12.5, "avail": 900}])
        self.assertEqual(list(out["osd_lat"]), ["osd.0", "osd.1"])
        self.assertEqual(agent.SERIES["_ceph"]["used_pct"], 25.0)
        self.assertEqual(agent.SERIES["_ceph"]["io_rd"], 10)
        self.assertNotIn("images", out)  # статистика rbd устарела или ещё не собрана

    def test_ceph_new_osdmap_format(self):
        out = self.ceph({"osdmap": {"num_osds": 4, "num_up_osds": 4, "num_in_osds": 4}, "pgmap": {}})
        self.assertEqual(out["osd"], {"total": 4, "up": 4, "in": 4})
        self.assertNotIn("used_pct", agent.SERIES["_ceph"])  # без bytes_total процент не считаем

    def test_backups(self):
        tasks = [{"type": "vzdump", "upid": "u1", "node": "pve1", "starttime": 10, "endtime": 20, "status": "OK"},
                 {"type": "qmstart", "upid": "u2", "starttime": 30},
                 {"type": "vzdump", "upid": "u3", "node": "pve2", "starttime": 40, "status": "running"}]
        content = [{"vmid": 105, "ctime": 100, "size": 1, "notes": "старый"},
                   {"vmid": 105, "ctime": 200, "size": 2, "notes": "db", "verification": {"state": "ok"}},
                   {"vmid": 104, "ctime": 150, "size": 3},
                   {"volid": "без vmid"}]

        def run_json(cmd, timeout=15):
            return tasks if cmd[2] == "/cluster/tasks" else content
        with mock.patch.object(agent, "run_json", run_json), mock.patch.dict(agent.CFG, backup_storage="pbs"):
            out = agent.mod_backups()
        self.assertEqual([t["upid"] for t in out["tasks"]], ["u3", "u1"])
        self.assertEqual(out["guests"], [
            {"vmid": 104, "last": 150, "count": 1, "size": 3, "name": "", "verified": None},
            {"vmid": 105, "last": 200, "count": 2, "size": 2, "name": "db", "verified": "ok"}])


class Registry(unittest.TestCase):
    def test_modules_registered_consistently(self):
        """Новый модуль = функция + MODULES + CLUSTER_KEY (если на хост) + интервал в CFG."""
        for name, (fn, interval) in agent.MODULES.items():
            self.assertTrue(callable(fn), name)
            self.assertIn(interval, agent.CFG, name)
        for name, key in agent.CLUSTER_KEY.items():
            self.assertIn(name, agent.MODULES)
            self.assertEqual(key, name + ":{host}")


if __name__ == "__main__":
    unittest.main()
