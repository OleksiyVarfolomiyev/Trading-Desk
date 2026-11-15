"""The ladders' table splits their result into spread and inventory drift, and measures markouts."""

import unittest

import pandas as pd

from desk import market_making


class LadderMetricsTests(unittest.TestCase):
    def test_ladder_pnl_is_spread_plus_inventory_drift_less_fees(self):
        # Sold 1 at 102, bought 1 at 100 (a 2 round trip), then bought 2 more at 99; the mark is 97.
        fills = [{"side": "A", "px": "102", "sz": "1", "fee": "0.1"}, {"side": "B", "px": "100", "sz": "1", "fee": "0.1"},
                 {"side": "B", "px": "99", "sz": "2", "fee": "0.1"}]
        row = market_making.metrics(fills, mark=97.0)
        self.assertAlmostEqual(row["Ladder PnL"], 102 - 100 - 198 + 2 * 97 - 0.3)   # 2 still held net, at 97
        self.assertAlmostEqual(row["Spread captured"] + row["Inventory drift"] + row["Fees"], row["Ladder PnL"])
        self.assertAlmostEqual(row["Fees"], -0.3)  # paid: negative
        self.assertLess(row["Inventory drift"], 0)  # bought into a fall

    def test_spread_pairs_fills_in_time_order_not_by_average(self):
        # A falling market: bought 1 at 110 (t0), sold 1 at 100 (t1), sold 1 at 90 (t2), bought 1 at 80 (t3).
        # By average the matched part sold at 95 and bought at 95: 0. In time order the first buy lost 10 on its
        # sell, and the second sell made 10 on its buy back: 0 as well, but a sell-first trend must not read as loss.
        fills = [{"side": "A", "px": "100", "sz": "1", "time": 0}, {"side": "B", "px": "90", "sz": "1", "time": 1},
                 {"side": "A", "px": "90", "sz": "1", "time": 2}, {"side": "B", "px": "80", "sz": "1", "time": 3}]
        self.assertAlmostEqual(market_making.round_trip_pnl(fills), 20.0)   # both short round trips made 10
        early_buys = [{"side": "B", "px": "110", "sz": "2", "time": 0}, {"side": "A", "px": "90", "sz": "1", "time": 1},
                      {"side": "A", "px": "100", "sz": "1", "time": 2}]
        self.assertAlmostEqual(market_making.round_trip_pnl(early_buys), -30.0)  # bought 110, sold 90 and 100
        self.assertAlmostEqual(market_making.round_trip_pnl(early_buys[::-1]), -30.0)  # input order does not matter

    def test_markouts_are_signed_for_the_fill(self):
        minute = 60_000
        closes = pd.Series({index * minute: 100.0 - index for index in range(30)})  # falls 1 a minute
        buy = {"side": "B", "px": "100", "time": 0}
        sell = {"side": "A", "px": "100", "time": 0}
        late = {"side": "B", "px": "80", "time": 29 * minute}                      # no candle 15 minutes on yet
        moves = market_making.markouts([buy], closes)
        self.assertAlmostEqual(moves["Markout 1m (bps)"], -100.0)    # bought, the price fell 1%
        self.assertAlmostEqual(moves["Markout 15m (bps)"], -1500.0)
        self.assertAlmostEqual(market_making.markouts([sell], closes)["Markout 1m (bps)"], 100.0)
        self.assertAlmostEqual(market_making.markouts([buy, late], closes)["Markout 15m (bps)"], -1500.0)
        self.assertTrue(market_making.markouts([buy], pd.Series(dtype=float))["Markout 1m (bps)"] !=
                        market_making.markouts([buy], pd.Series(dtype=float))["Markout 1m (bps)"])  # nothing known: NaN

    def test_the_total_weights_markouts_by_fills(self):
        frame = pd.DataFrame([{"Fills": 3, "Volume": 100.0, "Spread captured": 1.0, "Inventory drift": -2.0, "Ladder PnL": -1.1,
                               "Fees": 0.1, "Funding": 0.5, "Markout 1m (bps)": -2.0, "Markout 15m (bps)": float("nan")},
                              {"Fills": 1, "Volume": 100.0, "Spread captured": 0.0, "Inventory drift": 0.0, "Ladder PnL": 0.0,
                               "Fees": 0.0, "Funding": 0.0, "Markout 1m (bps)": 2.0, "Markout 15m (bps)": 4.0}])
        total = market_making.total_row(frame)
        self.assertAlmostEqual(total["Markout 1m (bps)"], -1.0)
        self.assertAlmostEqual(total["Markout 15m (bps)"], 4.0)
        self.assertAlmostEqual(total["Inventory drift"], -2.0)

    def test_ladder_funding_is_its_share_of_the_position_at_each_payment(self):
        # Short 10; the ladder sells 2 more (inventory -2), then buys 1 back (inventory -1).
        fills = [{"side": "A", "sz": "2", "time": 1_000}, {"side": "B", "sz": "1", "time": 3_000}]
        events = [{"time": 500, "delta": {"coin": "X", "szi": "-10", "usdc": "1.0"}},     # before any fill: none is the ladder's
                  {"time": 2_000, "delta": {"coin": "X", "szi": "-12", "usdc": "1.2"}},   # inventory -2 of -12: a sixth
                  {"time": 4_000, "delta": {"coin": "X", "szi": "-11", "usdc": "1.1"}}]   # inventory -1 of -11: an eleventh
        parts = [amount for _, amount in market_making.ladder_funding_parts(fills, events)]
        self.assertEqual(parts[0], 0.0)
        self.assertAlmostEqual(parts[1], 0.2)
        self.assertAlmostEqual(parts[2], 0.1)


if __name__ == "__main__":
    unittest.main()


class LadderFrameTests(unittest.TestCase):
    def test_a_running_ladder_builds_its_row(self):
        # 2026-11-13 the row failed to build (a local variable hid the date helper): the whole table went missing.
        from types import SimpleNamespace
        from unittest.mock import patch

        wallet = "0x" + "ab" * 20
        ladder = {"wallet": wallet, "coin": "BTC", "base": -0.02, "started": "2026-11-12 10:00"}
        fill = {"coin": "BTC", "side": "B", "px": "69000", "sz": "0.001", "fee": "0.01", "time": 1_763_000_000_000}
        state = {"assetPositions": [{"position": {"coin": "BTC", "szi": "-0.021", "positionValue": "1449"}}]}
        with patch.object(market_making, "running_ladders", return_value=[ladder]), \
                patch("desk.accounts.load_accounts", return_value=[SimpleNamespace(id=wallet, venue="hl", name="HL abab")]), \
                patch("desk.hl.account_analytics.post", return_value=state), \
                patch.object(market_making, "ladder_fill_rows", return_value=[fill]), \
                patch.object(market_making, "funding_events", return_value=[]), \
                patch.object(market_making, "minute_closes", return_value=pd.Series(dtype=float)):
            frame = market_making.market_making_frame()
        self.assertEqual(list(frame["Coin"]), ["BTC"])
        self.assertTrue(frame.iloc[0]["Since"])          # "Nov 12" (or "Nov'26" once old)
        self.assertEqual(frame.iloc[0]["Status"], "quoting")


class ConsolidatedHistoryTests(unittest.TestCase):
    def frame(self, ladders, fills):
        from types import SimpleNamespace
        from unittest.mock import patch

        wallet = "0x" + "ab" * 20
        ladders = [dict(item, wallet=wallet) for item in ladders]
        state = {"assetPositions": [{"position": {"coin": "BTC", "szi": "-0.021", "positionValue": "1449"}}]}
        with patch.object(market_making, "running_ladders", return_value=ladders), \
                patch("desk.accounts.load_accounts", return_value=[SimpleNamespace(id=wallet, venue="hl", name="HL abab")]), \
                patch("desk.hl.account_analytics.post", return_value=state), \
                patch.object(market_making, "ladder_fill_rows", return_value=fills), \
                patch.object(market_making, "funding_events", return_value=[]), \
                patch.object(market_making, "minute_closes", return_value=pd.Series(dtype=float)):
            return market_making.market_making_frame()

    def test_a_restart_does_not_reset_the_ladders_history(self):
        # Fills from before the ladder's latest start still count, and Since is the first time it quoted.
        import time

        day = 86_400_000
        now = int(time.time() * 1000)
        first = pd.Timestamp(now - 6 * day, unit="ms", tz="UTC")
        early = {"coin": "BTC", "side": "B", "px": "69000", "sz": "0.001", "fee": "0.01", "time": now - 6 * day}
        late = {"coin": "BTC", "side": "S", "px": "69100", "sz": "0.001", "fee": "0.01", "time": now - 1 * day}
        restart = (pd.Timestamp(now, unit="ms", tz="UTC") - pd.Timedelta(hours=12)).strftime("%Y-%m-%d %H:%M")
        frame = self.frame([{"coin": "BTC", "base": -0.02, "started": restart}], [early, late])
        self.assertEqual(int(frame.iloc[0]["Fills"]), 2)
        self.assertEqual(frame.iloc[0]["Since"], f"{first:%b} {first.day}")   # the first fill, not the restart 12 hours ago

    def test_a_ladder_that_was_stopped_stays_in_the_table(self):
        gone = {"coin": "LIT", "side": "B", "px": "3.2", "sz": "10", "fee": "0.01", "time": int(__import__("time").time() * 1000) - 3 * 86_400_000}
        frame = self.frame([{"coin": "BTC", "base": -0.02, "started": "2026-11-12 10:00"}], [gone])
        row = frame.set_index("Coin").loc["LIT"]
        self.assertEqual(row["Status"], "stopped")
        self.assertEqual(int(row["Fills"]), 1)


class CoinAcrossWalletsTests(unittest.TestCase):
    def test_one_coin_worked_in_two_wallets_is_one_row_with_the_whole_history(self):
        nan = float("nan")
        small = {"Account": "HL 07f2", "Coin": "ETH", "Side": "", "Status": "stopped", "Since": "Nov 13", "_first": 2_000, "Fills": 1,
                 "Volume": 20.0, "Spread captured": 0.0, "Inventory drift": 0.0, "Fees": -0.01, "Funding": 0.0, "Ladder PnL": -0.01,
                 "Captured per $1k": nan, "Markout 1m (bps)": nan, "Markout 15m (bps)": nan, "Position vs base": nan, "Quotes": nan, "Queued": nan}
        big = dict(small, Account="HL c248", Side="SHORT", Status="quoting", Since="Nov 12", _first=1_000, Fills=167, Volume=4000.0,
                   **{"Spread captured": 5.0, "Inventory drift": 1.0, "Fees": -0.5, "Ladder PnL": 5.5, "Markout 1m (bps)": -1.0,
                      "Position vs base": 0.0, "Quotes": 2, "Queued": 2})
        other = dict(big, Coin="BTC", Account="HL c248")
        frame = market_making._by_coin([small, big, other])
        eth = frame.set_index("Coin").loc["ETH"]
        self.assertEqual(len(frame), 2)                       # ETH once, BTC once
        self.assertEqual(int(eth["Fills"]), 168)
        self.assertEqual(eth["Since"], "Nov 12")              # the earliest first quote
        self.assertEqual(eth["Account"], "HL 07f2 + c248")
        self.assertEqual(eth["Status"], "quoting")            # one of its ladders is running
        self.assertAlmostEqual(eth["Ladder PnL"], 5.49)
        self.assertAlmostEqual(eth["Markout 1m (bps)"], -1.0)  # only the wallet with a reading counts
