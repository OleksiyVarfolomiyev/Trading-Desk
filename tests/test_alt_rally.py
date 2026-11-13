"""The alt rally watch: a 24-hour alt basket move warns at 5% and recommends a cut at 8% or on a second day."""

import unittest

from desk import alt_rally


def market(moves: dict[str, float], volume: float = 1e7):
    meta = {"universe": [{"name": name} for name in moves]}
    contexts = [{"markPx": str(100 * (1 + move)), "prevDayPx": "100", "dayNtlVlm": str(volume)} for move in moves.values()]
    return meta, contexts


class AltRallyTests(unittest.TestCase):
    def test_the_basket_is_the_liquid_alts_without_majors_or_stables(self):
        meta, contexts = market({"BTC": 0.10, "ETH": 0.10, "USDC": 0.0, "SOL": 0.06, "DOGE": 0.04, "kPEPE": 0.5})
        move, rising, count = alt_rally.basket_move(meta, contexts)
        self.assertAlmostEqual(move, 0.05)            # SOL and DOGE only
        self.assertEqual((rising, count), (2, 2))

    def test_levels(self):
        now = 1_000_000.0
        self.assertEqual(alt_rally.level(0.04, [], now), "none")
        self.assertEqual(alt_rally.level(0.05, [], now), "watch")
        self.assertEqual(alt_rally.level(0.08, [], now), "cut")
        self.assertEqual(alt_rally.level(0.05, [now - 24 * 3600], now), "cut")      # a second day of it
        self.assertEqual(alt_rally.level(0.05, [now - 3600], now), "watch")         # the same stretch
        self.assertEqual(alt_rally.level(0.05, [now - 72 * 3600], now), "watch")    # too long ago

    def test_alerts_lead_with_the_signal_and_size_the_hit(self):
        title, text = alt_rally.alert("watch", 0.053, 34, 40, -0.091)
        self.assertEqual(title, "Alt rally")
        self.assertTrue(text.startswith("alts 5.3% in 24h (34 of 40 up); the book -9.1% on it."))
        title, text = alt_rally.alert("cut", 0.086, 38, 40, None)
        self.assertEqual(title, "Alt rally: cut gross")
        self.assertIn("Cut gross by 33%?", text)
        self.assertIsNone(alt_rally.alert("none", 0.01, 1, 40, 0.0))

    def test_the_book_move_is_size_times_the_24_hour_change_over_equity(self):
        by_coin = {"SOL": {"markPx": "110", "prevDayPx": "100"}}
        self.assertAlmostEqual(alt_rally.book_move([{"coin": "SOL", "szi": "-10"}], by_coin, 1000.0), -0.1)


class StockDipTests(unittest.TestCase):
    def test_the_stock_dip_mirrors_the_alt_watch_for_falls(self):
        now = 1_000_000.0
        watch, cut = alt_rally.STOCK_WATCH_MOVE, alt_rally.STOCK_CUT_MOVE
        self.assertEqual(alt_rally.level(-0.01, [], now, watch, cut), "none")
        self.assertEqual(alt_rally.level(0.03, [], now, watch, cut), "none")           # a rise is not a dip
        self.assertEqual(alt_rally.level(-0.021, [], now, watch, cut), "watch")
        self.assertEqual(alt_rally.level(-0.04, [], now, watch, cut), "cut")
        self.assertEqual(alt_rally.level(-0.021, [now - 30 * 3600], now, watch, cut), "cut")   # a second day

    def test_the_index_move_and_the_alert(self):
        meta = {"universe": [{"name": "xyz:AMD"}, {"name": "xyz:SP500"}]}
        contexts = [{"markPx": "500", "prevDayPx": "520"}, {"markPx": "6700", "prevDayPx": "6850"}]
        self.assertAlmostEqual(alt_rally.index_move(meta, contexts), 6700 / 6850 - 1)
        title, text = alt_rally.stock_alert("watch", -0.022, -0.004)
        self.assertEqual(title, "Stock dip")
        self.assertTrue(text.startswith("S&P 500 -2.2% in 24h; the stock book -0.4% of trading equity on it."))

    def test_ib_positions_are_priced_by_hl_where_listed_else_by_their_own_change(self):
        by_coin = {"xyz:NVDA": {"markPx": "95", "prevDayPx": "100"}}                     # HL: NVDA -5%
        positions = [{"symbol": "NVDA", "quantity": 2, "mark_price": 95, "change24h": -0.01},
                     {"symbol": "KYIV", "quantity": 10, "mark_price": 12, "change24h": 0.2},  # not on HL
                     {"symbol": "XXX", "quantity": 1, "mark_price": 10}]                       # no price at all
        self.assertAlmostEqual(alt_rally.ib_change(positions, by_coin), 2 * 95 * -0.05 / 0.95 + 120 * 0.2 / 1.2)
        self.assertEqual(alt_rally.stock_alert("cut", -0.04, None)[0], "Stock sell-off: cut gross")


if __name__ == "__main__":
    unittest.main()
