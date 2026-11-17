import unittest

from desk import execution_quality as eq


class ExecutionQualityTests(unittest.TestCase):
    def test_maker_share_fee_bps_and_excess(self):
        now = 10_000_000.0
        t = (now - 3600) * 1000
        fills = [{"time": t, "px": "10", "sz": "30", "fee": "0.3", "crossed": False},      # maker 300 at 10 bps
                 {"time": t, "px": "10", "sz": "10", "fee": "0.4", "crossed": True},       # taker 100 at 40 bps
                 {"time": (now - 90 * 86400) * 1000, "px": "10", "sz": "1000", "fee": "9", "crossed": True}]  # too old
        out = eq.summarize(fills, now=now)
        self.assertAlmostEqual(out["maker_share"], 0.75)
        self.assertAlmostEqual(out["fee_bps"], 17.5)
        self.assertAlmostEqual(out["taker_excess"], 0.3)

    def test_nothing_traded(self):
        self.assertEqual(eq.summarize([]), {})


if __name__ == "__main__":
    unittest.main()
