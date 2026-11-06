import time
import unittest
from unittest import mock

from common import ALERTS, Clock, S, payload, reset
from luxmon.analytics import backup_work_age
from luxmon.settings import backup_age, save_settings
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

    ANOM = [{"host": "n1", "metric": "net_tx", "value": 1e7, "usual": 1e5, "msg": "n1: исходящий трафик намного выше"}]

    def test_anomaly_is_unusual_and_quiet(self):
        with Clock() as c, mock.patch("luxmon.alerts.anomalies", return_value=self.ANOM):
            ingest(payload(now=c.now))
            ALERTS.evaluate()
            self.assertEqual(ALERTS.active["anom:n1:net_tx"]["level"], "info")
            self.assertEqual(self.sent, [])  # «необычное» в Telegram не уходит
            (lvl, muted), = S.q("SELECT level, muted FROM alert_log WHERE key='anom:n1:net_tx'")
            self.assertEqual((lvl, muted), ("info", 0))  # на полосе за сутки оно видно
        with Clock() as c, mock.patch("luxmon.alerts.anomalies", return_value=[]):
            c.advance(3600)
            ingest(payload(now=c.now))
            ALERTS.evaluate()
            self.assertNotIn("anom:n1:net_tx", ALERTS.active)
            self.assertEqual(self.sent, [])  # и «Решено» по нему тоже

    def test_anomaly_notified_when_enabled(self):
        save_settings({"notify_anomalies": True})
        with Clock() as c, mock.patch("luxmon.alerts.anomalies", return_value=self.ANOM):
            ingest(payload(now=c.now))
            ALERTS.evaluate()
            self.assertEqual(self.sent, ["🔵 n1: исходящий трафик намного выше"])

    def test_silence_one_alert(self):
        with Clock() as c:
            ingest(payload(now=c.now, disks=[{"mount": "/", "used": 96, "size": 100},
                                             {"mount": "/data", "used": 96, "size": 100}]))
            ALERTS.evaluate()
            sil = ALERTS.add_silence("key:disk:n1:/", 60, "", "test")
            self.assertEqual(sil["title"], "тревога «n1: диск / 96%»")
            self.assertTrue(ALERTS.silenced("disk:n1:/"))
            self.assertFalse(ALERTS.silenced("disk:n1:/data"))

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
    def setUp(self):
        reset()

    def test_setting_switches_weekend_counting(self):
        friday = time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1))
        monday = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
        self.assertEqual(backup_age(friday, monday), 3 * 24 * 3600)
        save_settings({"backup_weekdays_only": True})
        self.assertEqual(backup_age(friday, monday), 24 * 3600)

    def test_weekend_not_counted(self):
        friday = time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1))
        monday = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
        self.assertEqual(backup_work_age(friday, monday), 24 * 3600)
        self.assertEqual(backup_work_age(friday, monday, weekdays_only=False), 3 * 24 * 3600)

    def test_weekday_counted_fully(self):
        monday = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, -1))
        tuesday = time.mktime((2026, 11, 3, 12, 0, 0, 0, 0, -1))
        self.assertEqual(backup_work_age(monday, tuesday), 24 * 3600)


if __name__ == "__main__":
    unittest.main()
