"""Wallet tokens outside HL: performance is the price move of what was held, so deposits are not gains."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from desk.accounts import Account
from desk.desk_accounts import DeskAccounts, wallet_performance
from desk.venues.onchain_history import load_returns, price_moves

WALLET = "0x" + "ab" * 18 + "07f2"
DAYS = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")


class PriceMoveTests(unittest.TestCase):
    def test_a_deposit_earns_nothing_until_the_next_day(self):
        # 10 tokens, then 90 more arrive on day 3 (a deposit). Price 10, 11, 11, 12.1, 12.1.
        balances = pd.DataFrame({"base:0xabc": [10.0, 10.0, 100.0, 100.0, 100.0]}, index=DAYS)
        prices = pd.Series([10.0, 11.0, 11.0, 12.1, 12.1], index=DAYS)
        moves, unpriced = price_moves(balances, price_of=lambda key, days: prices)
        self.assertEqual(unpriced, [])
        self.assertEqual(moves["pnl_usd"].round(6).tolist(), [10.0, 0.0, 110.0, 0.0])  # not 990 on the deposit day
        self.assertEqual(moves["base_usd"].round(6).tolist(), [100.0, 110.0, 1100.0, 1210.0])
        self.assertAlmostEqual(((1 + moves["pnl_usd"] / moves["base_usd"]).prod()) - 1, 0.21, places=6)

    def test_an_unpriced_token_adds_nothing(self):
        balances = pd.DataFrame({"base:0xabc": [1.0] * 5}, index=DAYS)
        moves, unpriced = price_moves(balances, price_of=lambda key, days: pd.Series(index=days, dtype=float))
        self.assertEqual(unpriced, ["base:0xabc"])
        self.assertEqual(moves["pnl_usd"].sum(), 0.0)


class WalletPerformanceTests(unittest.TestCase):
    def test_histories_are_built_from_the_saved_daily_moves(self):
        with tempfile.TemporaryDirectory() as folder:
            pd.DataFrame({"date": ["2026-01-02", "2026-01-03"], "pnl_usd": [10.0, -5.0], "base_usd": [100.0, 110.0]}).to_csv(
                Path(folder) / "onchain_returns_07f2.csv", index=False)
            pd.DataFrame({"date": ["2026-01-01", "2026-01-02", "2026-01-03"], "value_usd": [100.0, 110.0, 105.0]}).to_csv(
                Path(folder) / "onchain_value_backfill_07f2.csv", index=False)
            accounts = [Account(venue="hl", id=WALLET, label="")]
            histories = wallet_performance(accounts, folder)
            self.assertEqual(list(histories), ["07f2 wallet"])
            frame = histories["07f2 wallet"]
            self.assertEqual(frame["pnl"].tolist(), [10.0, 5.0])
            self.assertEqual(frame["ts"].dt.strftime("%Y-%m-%d").tolist(), ["2026-01-03", "2026-01-04"])
            self.assertEqual(frame["accountValue"].iloc[-1], 105.0)
            self.assertEqual(len(load_returns(Path(folder), WALLET)), 2)

    def test_no_saved_moves_means_no_wallet_row_and_a_note(self):
        with tempfile.TemporaryDirectory() as folder:
            instance = object.__new__(DeskAccounts)
            instance.accounts = [SimpleNamespace(venue="hl", id=WALLET, onchain=True, name="HL 07f2")]
            instance.data_dir, instance.notes = Path(folder), []
            self.assertEqual(instance.wallet_histories(), {})
            self.assertIn("--returns", instance.notes[0])


if __name__ == "__main__":
    unittest.main()


class DayReturnTests(unittest.TestCase):
    def test_24h_return_is_each_accounts_change_over_its_value_a_day_ago(self):
        from desk.dashboard import with_day_returns
        from desk.day_change import DayChange

        frame = pd.DataFrame({"Account": ["HL 07f2", "07f2 wallet", "Desk (HL + IB)", "Desk incl. wallets"],
                              "Value": [1100.0, 500.0, 1100.0, 1600.0], "Return 7D": [0.0] * 4})
        change = DayChange(change=90.0, value=1600.0, parts={"HL 07f2": 100.0, "07f2 wallet": -10.0})
        out = with_day_returns(frame, change)
        self.assertEqual(list(out.columns), ["Account", "Value", "Return 24h", "Return 7D"])
        self.assertAlmostEqual(out["Return 24h"][0], 0.10)             # 100 on 1,000 a day ago
        self.assertAlmostEqual(out["Return 24h"][1], -10 / 510)         # a loss over the larger earlier value
        self.assertAlmostEqual(out["Return 24h"][2], 0.10)             # the HL + IB row leaves wallets out
        self.assertAlmostEqual(out["Return 24h"][3], 90 / 1510)         # the whole desk
        self.assertTrue(with_day_returns(frame, None)["Return 24h"].isna().all())
