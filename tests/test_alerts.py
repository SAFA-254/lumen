import time
import unittest

from common import ALERTS, Clock, S, payload, reset
from luxmon.analytics import backup_work_age
from luxmon.alerts import Alerts
from luxmon.ingest import ingest


class AlertEngine(unittest.TestCase):
    def setUp(self):
        self.sent = reset()

    def tick(self, clock, sec=60, **kw):
        clock.advance(sec)
        ingest(payload(now=clock.now, **kw))
        ALERTS.evaluate()

    def test_threshold_waits_for_hold_time(self):
        with Clock() as c:
            for _ in range(4):
                self.tick(c, metrics={"cpu": 99})
            self.assertNotIn("cpu:n1", ALERTS.active)
            for _ in range(2):
                self.tick(c, metrics={"cpu": 99})
            self.assertIn("cpu:n1", ALERTS.active)
            self.assertEqual(sum("CPU 99%" in x for x in self.sent), 1)

    def test_short_spike_does_not_alert(self):
        with Clock() as c:
            self.tick(c, metrics={"cpu": 99})
            self.tick(c, metrics={"cpu": 5})
            for _ in range(10):
                self.tick(c, metrics={"cpu": 5})
            self.assertEqual(self.sent, [])

    def test_alert_clears_after_quiet_period(self):
        with Clock() as c:
            for _ in range(7):
                self.tick(c, metrics={"cpu": 99})
            self.tick(c, metrics={"cpu": 5})
            self.assertIn("cpu:n1", ALERTS.active)  # не дребезжит сразу после спада
            for _ in range(6):
                self.tick(c, metrics={"cpu": 5})
            self.assertNotIn("cpu:n1", ALERTS.active)
            self.assertTrue(any(x.startswith("✅ Решено") for x in self.sent))
            (end,), = S.q("SELECT end FROM alert_log WHERE key='cpu:n1'")
            self.assertIsNotNone(end)

    def test_host_down(self):
        with Clock() as c:
            ingest(payload(now=c.now))
            c.advance(91)
            ALERTS.evaluate()
            self.assertEqual(ALERTS.active["down:n1"]["level"], "crit")

    def test_disk_levels(self):
        with Clock() as c:
            ingest(payload(now=c.now, disks=[{"mount": "/", "used": 90, "size": 100}]))
            ALERTS.evaluate()
            self.assertEqual(ALERTS.active["disk:n1:/"]["level"], "warn")
            ingest(payload(now=c.now, disks=[{"mount": "/", "used": 96, "size": 100}]))
            ALERTS.evaluate()
            self.assertEqual(ALERTS.active["disk:n1:/"]["level"], "crit")

    def test_silence_mutes_notification_but_records_alert(self):
        with Clock() as c:
            ALERTS.add_silence("host:n1", 60, "работы", "test")
            self.sent.clear()
            ingest(payload(now=c.now, disks=[{"mount": "/", "used": 96, "size": 100}]))
            ALERTS.evaluate()
            self.assertIn("disk:n1:/", ALERTS.active)
            self.assertEqual(self.sent, [])
            (muted,), = S.q("SELECT muted FROM alert_log WHERE key='disk:n1:/'")
            self.assertEqual(muted, 1)

    def test_problem_reported_after_silence_ends(self):
        with Clock() as c:
            ALERTS.add_silence("host:n1", 10, "", "test")
            ingest(payload(now=c.now, disks=[{"mount": "/", "used": 96, "size": 100}]))
            ALERTS.evaluate()
            c.advance(601)
            ingest(payload(now=c.now, disks=[{"mount": "/", "used": 96, "size": 100}]))
            ALERTS.evaluate()
            self.assertTrue(any("после обслуживания" in x for x in self.sent))

    def test_scope_matches(self):
        m = Alerts.scope_matches
        self.assertTrue(m("all", "anything:x"))
        self.assertTrue(m("host:n1", "disk:n1:/"))
        self.assertFalse(m("host:n1", "disk:n2:/"))
        self.assertFalse(m("host:n1", "ceph:health"))
        self.assertTrue(m("ceph", "stor:local"))
        self.assertTrue(m("backups", "bk:101"))
        self.assertFalse(m("ceph", "bk:101"))

    def test_ceph_and_patroni(self):
        with Clock() as c:
            cluster = {"ceph": {"health": "HEALTH_ERR", "checks": [{"msg": "беда"}]},
                       "patroni": {"members": [{"name": "db1", "role": "replica", "state": "running"}]}}
            ingest(payload(now=c.now, cluster=cluster))
            ALERTS.evaluate()
            self.assertIn("pg:noleader", ALERTS.active)
            c.advance(61)
            ingest(payload(now=c.now, cluster=cluster))
            ALERTS.evaluate()
            self.assertEqual(ALERTS.active["ceph:health"]["level"], "crit")


class BackupAge(unittest.TestCase):
    def test_weekend_not_counted(self):
        friday = time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1))
        monday = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
        self.assertEqual(backup_work_age(friday, monday), 24 * 3600)

    def test_weekday_counted_fully(self):
        monday = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
        tuesday = time.mktime((2026, 11, 3, 12, 0, 0, 0, 0, -1))
        self.assertEqual(backup_work_age(monday, tuesday), 24 * 3600)


if __name__ == "__main__":
    unittest.main()
