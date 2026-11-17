import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

from desk import books

DAYS = pd.date_range("2026-11-01", periods=4, freq="D", tz="UTC")


def _path(rows):
    frame = pd.DataFrame(rows, columns=["ts", "signed", "px", "fee", "after"])
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)
    frame["day"] = frame["ts"].dt.normalize()
    return frame


class CoinDayPnl(unittest.TestCase):
    def test_holding_through_days_earns_the_price_change(self):
        closes = pd.Series([100.0, 110.0, 99.0, 99.0], index=DAYS)
        frame = books.coin_day_pnl(-2.0, _path([]), closes, pd.Series(dtype=float), DAYS)
        self.assertEqual(list(frame["price"]), [0.0, -20.0, 22.0, 0.0])
        self.assertEqual(frame["trading"].sum(), 0.0)

    def test_fills_are_marked_against_the_days_close_less_fees(self):
        closes = pd.Series([100.0, 110.0, 120.0, 120.0], index=DAYS)
        # Buy 1 at 105 on day 2 (close 110): +5 less 0.1 fee; then hold 1 into day 3 (+10).
        path = _path([("2026-11-02 12:00", 1.0, 105.0, 0.1, 1.0)])
        frame = books.coin_day_pnl(0.0, path, closes, pd.Series(dtype=float), DAYS)
        self.assertAlmostEqual(frame.loc[DAYS[1], "trading"], 5.0)
        self.assertAlmostEqual(frame.loc[DAYS[1], "fees"], -0.1)
        self.assertEqual(frame.loc[DAYS[1], "price"], 0.0)   # flat going into day 2
        self.assertEqual(frame.loc[DAYS[2], "price"], 10.0)
        # A round trip's total is exactly what the trades made: bought 105, worth 120 at the end, less the fee.
        self.assertAlmostEqual(frame["price"].sum() + frame["trading"].sum() + frame["fees"].sum(), 120 - 105 - 0.1)

    def test_funding_lands_on_its_day(self):
        closes = pd.Series(100.0, index=DAYS)
        funding = pd.Series([0.5, -0.2], index=DAYS[[1, 3]])
        frame = books.coin_day_pnl(1.0, _path([]), closes, funding, DAYS)
        self.assertEqual(list(frame["funding"]), [0.0, 0.5, 0.0, -0.2])


class IbNamePnl(unittest.TestCase):
    def test_walks_today_back_through_fills(self):
        positions = [SimpleNamespace(symbol="MU", quantity=2.0, ref="r1")]
        fills = pd.DataFrame([{"time": "2026-11-03 15:00:00+00:00", "symbol": "MU", "side": "BOT", "quantity": 1.0,
                               "price": 100.0, "commission": -1.0}])
        closes = pd.Series([90.0, 95.0, 101.0, 101.0], index=DAYS)
        frame, notes = books.ib_name_pnl(positions, fills, DAYS, lambda symbol, ref: closes)
        self.assertEqual(notes, [])
        # One share held before the buy: +5 and +6 from price, then the buy at 100 marked at 101 less $1.
        self.assertEqual(list(frame["held"]), [1.0, 1.0, 2.0, 2.0])
        self.assertAlmostEqual(frame["price"].sum(), 11.0)
        self.assertAlmostEqual(frame["trading"].sum(), 1.0)   # the buy at 100 marked at 101
        self.assertAlmostEqual(frame["fees"].sum(), -1.0)

    def test_name_without_closes_is_left_to_ib_other(self):
        positions = [SimpleNamespace(symbol="ZZZ", quantity=1.0, ref="")]
        frame, notes = books.ib_name_pnl(positions, pd.DataFrame(), DAYS, lambda symbol, ref: None)
        self.assertTrue(frame.empty)
        self.assertIn("ZZZ", notes[0])


class Kinds(unittest.TestCase):
    def test_books(self):
        self.assertEqual(books.holding_kind("ib", "MU"), "Stocks")
        self.assertEqual(books.holding_kind("hl", "xyz:AMD"), "Stocks")
        self.assertEqual(books.holding_kind("hl", "BTC"), "Crypto")
        self.assertEqual(books.holding_kind("hl", "PAXG"), "Stocks")     # gold on the main DEX is TradFi
        self.assertEqual(books.holding_kind("wallet", "VVV"), "Wallet")   # held, in no book
        self.assertEqual(books.position_kind("crypto perp"), "Crypto")
        self.assertEqual(books.position_kind("tradfi perp"), "Stocks")

    def test_window_sum(self):
        series = pd.Series([1.0, 2.0, 3.0, 4.0], index=DAYS)
        self.assertEqual(books.window_sum(series, 2), 7.0)
        self.assertTrue(np.isnan(books.window_sum(pd.Series(dtype=float), 2)))


if __name__ == "__main__":
    unittest.main()


class StatementFillTests(unittest.TestCase):
    def test_reads_stock_trades_in_eastern_time(self):
        import tempfile
        from pathlib import Path

        text = ("Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,T. Price,C. Price,"
                "Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
                "Trades,Data,Order,Stocks,USD,MU,\"2026-11-09, 09:30:00\",2,100,101,-200,-1,200,0,2,O\n"
                "Trades,Data,Order,Stocks,EUR,RHMd,\"2026-11-09, 09:30:00\",1,500,501,-500,-1,500,0,1,O\n"
                "Trades,Data,Order,Forex,USD,EUR.USD,\"2026-11-09, 09:30:00\",10,1.1,1.1,-11,0,0,0,0,\n")
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "ib_statement_0000_20260101_20261113.csv").write_text(text)
            fills, covered = books.ib_statement_fills(folder)
        self.assertEqual(list(fills["symbol"]), ["MU"])                  # USD stocks only
        self.assertEqual(fills.iloc[0]["side"], "BOT")
        self.assertTrue(fills.iloc[0]["time"].startswith("2026-11-09 14:30"))   # 09:30 New York is 14:30 UTC
        self.assertEqual(str(covered.date()), "2026-11-14")


class ChartReturns(unittest.TestCase):
    def book(self, kind):
        days = pd.date_range("2026-01-01", periods=6, tz="UTC")
        coins = pd.DataFrame({"day": days, "value": [100.0, 100.0, 0.0, 0.0, 100.0, 100.0]})
        return SimpleNamespace(kind=kind, days=days, coins=coins, equity=pd.Series(100.0, index=days),
                               returns=pd.Series(0.01, index=days[1:]))

    def test_stocks_skip_the_days_after_holding_nothing(self):
        out = books.chart_returns(self.book("Stocks"))
        self.assertEqual(list(out.index.day), [2, 3, 6])   # days 4 and 5 follow a flat close; day 6 follows a held one

    def test_crypto_untouched(self):
        self.assertEqual(len(books.chart_returns(self.book("Crypto"))), 5)
