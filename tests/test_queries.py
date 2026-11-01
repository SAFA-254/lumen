import unittest

from common import Clock, S, payload, reset
from luxmon.ingest import ingest
from luxmon.queries import day_strip


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


if __name__ == "__main__":
    unittest.main()
