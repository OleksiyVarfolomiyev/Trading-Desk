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


class SqueezeTests(unittest.TestCase):
    def test_a_short_up_15_percent_with_shorts_paying_500_percent_is_squeezed(self):
        self.assertTrue(alt_rally.squeeze("short", 0.29, -27.38))      # BERA, Nov 14
        self.assertFalse(alt_rally.squeeze("short", 0.29, 0.11))       # a pop without crowding
        self.assertFalse(alt_rally.squeeze("short", 0.05, -27.38))     # crowded but not running
        self.assertTrue(alt_rally.squeeze("long", -0.2, 6.0))          # the mirror for a long
        self.assertFalse(alt_rally.squeeze("long", 0.29, -27.38))

    def test_the_alert_leads_with_the_squeeze_and_shows_the_stop(self):
        title, text = alt_rally.squeeze_alert("BERA", "short", 0.288, -27.38, 0.1304, 0.1412)
        self.assertEqual(title, "Squeeze: BERA short")
        self.assertEqual(text, "24h 29%, funding -7.5% d, SL 8.3%")
        self.assertIn("no SL", alt_rally.squeeze_alert("xyz:AMD", "long", -0.2, 6.0, 500, None)[1])


class StallTests(unittest.TestCase):
    def test_twelve_flat_hours_is_a_stall_and_the_alert_says_cut(self):
        self.assertTrue(alt_rally.stalled([0.130, 0.132, 0.128, 0.131] * 3))          # within +-3%
        self.assertFalse(alt_rally.stalled([0.120, 0.125, 0.130, 0.135] * 3))         # still running
        self.assertFalse(alt_rally.stalled([0.13] * 6))                                # too few hours
        squeeze_then_flat = [0.106] * 12 + [0.106 + 0.002 * i for i in range(12)] + [0.130, 0.132, 0.128, 0.131] * 3
        self.assertTrue(alt_rally.stalled_squeeze(squeeze_then_flat, "short"))       # ran +20%, then flat
        self.assertFalse(alt_rally.stalled_squeeze([0.13] * 36, "short"))             # flat all along: no squeeze
        self.assertFalse(alt_rally.stalled_squeeze(squeeze_then_flat, "long"))        # it ran for a long, not against
        title, text = alt_rally.stall_alert("BERA", "short", -31.37, 0.1304, 0.1412)
        self.assertEqual(title, "Squeeze stalled: BERA short")
        self.assertEqual(text, "flat 12h, funding -8.6% d, SL 8.3%")


if __name__ == "__main__":
    unittest.main()


class MomentumTests(unittest.TestCase):
    def test_a_fast_bounce_or_drop_fires_and_a_quiet_tape_does_not(self):
        from desk import alt_rally

        def candle(t, low, high, close):
            return {"t": t * 300_000, "l": low, "h": high, "c": close}
        bounce = [candle(0, 63000, 63500, 63200), candle(1, 61970, 62600, 62063), candle(13, 64500, 64900, 64812)]
        move, minutes, start, price = alt_rally.burst(bounce)
        self.assertAlmostEqual(move, 64812 / 61970 - 1)     # 4.6% off the low
        self.assertEqual((start, price, minutes), (61970, 64812, 65.0))
        drop = [candle(0, 64000, 65000, 64900), candle(6, 62000, 62500, 62400)]
        self.assertLess(alt_rally.burst(drop)[0], -0.03)
        quiet = [candle(0, 64000, 64500, 64200), candle(6, 63800, 64400, 64300)]
        self.assertIsNone(alt_rally.burst(quiet))


class SelloffBeforeTests(unittest.TestCase):
    def test_the_fall_into_the_bounce_low(self):
        from desk import alt_rally

        hour = 3_600_000
        hourly = [{"t": i * hour, "h": 70000 - i * 300, "l": 69500 - i * 300} for i in range(25)]   # a day of selling
        self.assertLess(alt_rally.selloff_before(hourly, 24 * hour), alt_rally.SELLOFF_MOVE)
        flat = [{"t": i * hour, "h": 64100, "l": 63900} for i in range(25)]
        self.assertGreater(alt_rally.selloff_before(flat, 24 * hour), alt_rally.SELLOFF_MOVE)


class VolumeRatioTests(unittest.TestCase):
    def test_the_moves_volume_against_the_usual_candle(self):
        from desk import alt_rally

        candles = [{"t": i, "v": 10} for i in range(20)] + [{"t": 20 + i, "v": 40} for i in range(4)]
        self.assertAlmostEqual(alt_rally.volume_ratio(candles, 20), 4.0)


class TrailStopTests(unittest.TestCase):
    def test_the_stop_sits_behind_the_best_price(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "execution"))
        import hl_daemon

        self.assertAlmostEqual(hl_daemon.trail_stop(1.0, 100.0, 0.015), 98.5)     # long: under the peak
        self.assertAlmostEqual(hl_daemon.trail_stop(-1.0, 100.0, 0.015), 101.5)   # short: over the trough


class FundingReadTests(unittest.TestCase):
    def test_quiet_funding_is_a_natural_flow_and_paid_funding_a_squeeze(self):
        from desk import alt_rally

        self.assertIn("natural flow", alt_rally.funding_read(0.05, 0.03))
        self.assertIn("shorts crowded", alt_rally.funding_read(0.05, -0.5))
        self.assertIn("longs crowded", alt_rally.funding_read(-0.05, 0.5))
