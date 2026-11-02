import json
import unittest

from common import ALERTS, Clock, S, payload, reset
from luxmon.ingest import ip_known, ingest
from luxmon.settings import save_settings, validate_check


class Ingest(unittest.TestCase):
    def setUp(self):
        self.sent = reset()

    def test_stores_metrics_and_disk_percent(self):
        with Clock() as c:
            ingest(payload(now=c.now))
        rows = dict(S.q("SELECT metric, v FROM raw WHERE host='n1'"))
        self.assertEqual(rows["cpu"], 5)
        self.assertEqual(rows["disk:/"], 10)

    def test_non_numeric_metric_ignored(self):
        with Clock() as c:
            ingest(payload(now=c.now, metrics={"cpu": "много", "mem": 1}))
        self.assertEqual([r[0] for r in S.q("SELECT metric FROM raw WHERE metric IN ('cpu','mem')")], ["mem"])

    def test_repeated_logins_are_not_duplicated(self):
        login = {"kind": "in", "ts": 100.0, "user": "root", "ip": "10.0.0.1", "method": "publickey", "port": 22}
        with Clock() as c:
            ingest(payload(now=c.now, ssh_logins=[login]))
            ingest(payload(now=c.now, ssh_logins=[login]))
        self.assertEqual(S.q("SELECT COUNT(*) FROM logins")[0][0], 1)

    def test_unknown_address_notifies_known_does_not(self):
        with Clock() as c:
            save_settings({"ssh_known": ["10.0.0.0/8 лаборатория"]})
            ingest(payload(now=c.now, ssh_logins=[
                {"kind": "in", "ts": 1.0, "user": "root", "ip": "10.1.1.1", "method": "publickey"}]))
            self.assertEqual(self.sent, [])
            ingest(payload(now=c.now, ssh_logins=[
                {"kind": "in", "ts": 2.0, "user": "root", "ip": "8.8.8.8", "method": "password"}]))
            self.assertEqual(len(self.sent), 1)
            self.assertIn("8.8.8.8", self.sent[0])

    def test_bruteforce_reported_once(self):
        with Clock() as c:
            fails = [{"kind": "fail", "ts": c.now - 100 + i, "user": "a", "ip": "9.9.9.9"} for i in range(6)]
            ingest(payload(now=c.now, ssh_logins=fails))
            ingest(payload(now=c.now, ssh_logins=[{"kind": "fail", "ts": c.now - 1, "user": "a", "ip": "9.9.9.9"}]))
        self.assertEqual(sum("неудачных попыток" in x for x in self.sent), 1)

    def test_logout_closes_session_and_lost_logout_is_detected(self):
        with Clock() as c:
            ingest(payload(now=c.now, ssh_logins=[
                {"kind": "in", "ts": c.now - 100, "user": "u", "ip": "1.1.1.1", "port": 5},
                {"kind": "in", "ts": c.now - 100, "user": "u", "ip": "2.2.2.2", "port": 6}]))
            ingest(payload(now=c.now, ssh_logins=[{"kind": "out", "ts": c.now, "user": "u", "ip": "1.1.1.1", "port": 5}],
                           ssh_open=[["2.2.2.2", 6]]))
            open_ips = [r[0] for r in S.q("SELECT ip FROM logins WHERE end IS NULL")]
            self.assertEqual(open_ips, ["2.2.2.2"])
            ingest(payload(now=c.now, ssh_open=[]))
            self.assertEqual(S.q("SELECT COUNT(*) FROM logins WHERE end IS NULL")[0][0], 0)

    def test_checks_go_only_to_runner(self):
        with Clock() as c:
            r1 = ingest(payload("n1", c.now, checks_local=[{"name": "w", "type": "ping", "host": "1.1.1.1"}]))
            r2 = ingest(payload("n2", c.now))
        self.assertEqual(r1["checks"][0]["name"], "w")
        self.assertEqual(r2["checks"], [])

    def test_checks_runner_chosen_when_empty(self):
        with Clock() as c:
            save_settings({"checks": [{"name": "w", "type": "ping", "host": "1.1.1.1"}], "checks_runner": ""})
            r1 = ingest(payload("n2", c.now))
            r2 = ingest(payload("n1", c.now))
        self.assertEqual((len(r1["checks"]), r2["checks"]), (1, []))

    def test_clock_skew_is_median(self):
        with Clock() as c:
            for s in (0.1, 0.1, 50, 0.1, 0.1):
                ingest(payload(now=c.now, sent=c.now + s))
        latest = json.loads(S.q("SELECT latest FROM hosts")[0][0])
        self.assertAlmostEqual(latest["clock"]["skew_ms"], 100, delta=1)
        ALERTS.evaluate()


class Validation(unittest.TestCase):
    def test_ping_host_cannot_be_an_option(self):
        with self.assertRaises(ValueError):
            validate_check({"name": "x", "type": "ping", "host": "-f"})

    def test_http_needs_url(self):
        with self.assertRaises(ValueError):
            validate_check({"name": "x", "type": "http", "url": "file:///etc/passwd"})

    def test_tcp_port_range(self):
        with self.assertRaises(ValueError):
            validate_check({"name": "x", "type": "tcp", "host": "a", "port": 70000})

    def test_ip_known(self):
        self.assertTrue(ip_known("10.1.2.3", ["10.0.0.0/8 лаборатория"]))
        self.assertFalse(ip_known("11.1.2.3", ["10.0.0.0/8"]))
        self.assertFalse(ip_known("не адрес", ["10.0.0.0/8"]))


if __name__ == "__main__":
    unittest.main()
