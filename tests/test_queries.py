import unittest

from common import Clock, S, payload, reset
from luxmon.ingest import ingest
from luxmon.analytics import rollup_and_prune
from luxmon.queries import add_note, annotations, day_strip, delete_note, series


class DayStrip(unittest.TestCase):
    def setUp(self):
        reset()

    def test_gap_when_no_host_sent_data(self):
        with Clock() as c:
            first = c.now
            ingest(payload(now=c.now))
            c.advance(1000)
            ingest(payload(now=c.now))
            gaps = day_strip(c.now)["gaps"]
        self.assertIn([int(first), int(first) + 1000], gaps)

    def test_other_host_fills_the_gap(self):
        with Clock() as c:
            first = c.now
            ingest(payload("n1", c.now))
            c.advance(250)
            ingest(payload("n2", c.now))
            c.advance(250)
            ingest(payload("n1", c.now))
            gaps = day_strip(c.now)["gaps"]
        self.assertEqual([g for g in gaps if g[0] >= first], [])

    def test_query_uses_host_index(self):
        plan = S.q("EXPLAIN QUERY PLAN SELECT ts FROM raw WHERE host=? AND metric='cpu' AND ts > ?", ("n1", 0))
        self.assertIn("raw_idx (host=? AND metric=? AND ts>?)", plan[0][3])


class History(unittest.TestCase):
    def setUp(self):
        reset()

    def test_hourly_rollup_and_long_ranges(self):
        with Clock() as c:
            rollup_and_prune()  # первый запуск: перенос накопленного
            for i in range(30):
                ingest(payload(now=c.now, metrics={"cpu": 10 + i}))
                c.advance(240)
            rollup_and_prune()
            rows = S.q("SELECT ts, v, vmax FROM hourly WHERE host='n1' AND metric='cpu' ORDER BY ts")
            self.assertGreaterEqual(len(rows), 1)
            self.assertTrue(all(r[0] % 3600 == 0 and r[2] >= r[1] for r in rows))
            # за 90 дней берём часовые данные, за сутки — сырые
            self.assertEqual(series("n1", "cpu", int(c.now - 90 * 86400), int(c.now))["step"] % 3600, 0)
            self.assertLess(series("n1", "cpu", int(c.now - 3600), int(c.now))["step"], 300)
        S.q("DELETE FROM hourly")


class Notes(unittest.TestCase):
    def setUp(self):
        reset()

    def test_note_shown_on_its_charts_and_global_everywhere(self):
        with Clock() as c:
            add_note({"scope": "host:n1", "text": "обновил ядро", "ts": c.now - 60}, "admin")
            add_note({"scope": "all", "text": "переезд стойки"}, "admin")
            n1 = [a["msg"] for a in annotations("host:n1", c.now - 3600, c.now + 1) if a["kind"] == "note"]
            n2 = [a["msg"] for a in annotations("host:n2", c.now - 3600, c.now + 1) if a["kind"] == "note"]
            self.assertEqual((n1, n2), (["обновил ядро", "переезд стойки"], ["переезд стойки"]))
            delete_note(S.q("SELECT id FROM notes WHERE scope='all'")[0][0])
            self.assertEqual(len([a for a in annotations("host:n2", c.now - 3600, c.now + 1) if a["kind"] == "note"]), 0)

    def test_bad_note_rejected(self):
        for body in ({"scope": "host:../x", "text": "a"}, {"scope": "ceph", "text": "  "}, {"scope": "ceph", "text": "a", "ts": 1}):
            with self.assertRaises(ValueError):
                add_note(body, "admin")


if __name__ == "__main__":
    unittest.main()


class AlertMetric(unittest.TestCase):
    def test_mapping(self):
        from luxmon.queries import alert_metric
        self.assertEqual(alert_metric("anom:pve1:net_tx"), ("pve1", "net_tx", "бит"))
        self.assertEqual(alert_metric("cpu:pve1:load"), ("pve1", "load1", ""))
        self.assertEqual(alert_metric("disk:pve1:/var"), ("pve1", "disk:/var", "%"))
        self.assertEqual(alert_metric("chk:web"), ("_checks", "web", "мс"))
        self.assertIsNone(alert_metric("disk:pve1:/:ro"))
        self.assertIsNone(alert_metric("bk:100"))

    def test_overview_alert_has_spark(self):
        from common import ALERTS, Clock, payload, reset
        from luxmon.ingest import ingest
        from luxmon.queries import overview
        reset()
        with Clock() as c:
            for _ in range(8):
                c.advance(60)
                ingest(payload(now=c.now, metrics={"cpu": 99}))
                ALERTS.evaluate()
            a = next(x for x in overview()["alerts"] if x["key"] == "cpu:n1")
        self.assertEqual((a["host"], a["metric"], a["unit"]), ("n1", "cpu", "%"))
        self.assertGreaterEqual(len(a["spark"]), 5)
