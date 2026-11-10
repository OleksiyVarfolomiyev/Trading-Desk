"""Rebuilding IB account value from an activity statement's transactions."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from desk.venues import ib_statement

STATEMENT = """Statement,Header,Field Name,Field Value
Account Information,Header,Field Name,Field Value
Account Information,Data,Account,U98768440 (Custom Consolidated)
Deposits & Withdrawals,Header,Currency,Account,Settle Date,Description,Amount
Deposits & Withdrawals,Data,USD,U98768440,2025-01-13,Electronic Fund Transfer,1000
Deposits & Withdrawals,Data,USD,U98768440,2025-01-14,Cancellation,-200
Deposits & Withdrawals,Data,USD,U98768440,2025-01-14,Electronic Fund Transfer,200
Deposits & Withdrawals,Data,Total,,,,1000
Trades,Header,DataDiscriminator,Asset Category,Currency,Account,Symbol,Date/Time,Quantity,T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code
Trades,Data,Order,Stocks,USD,U98768440,ABC,"2025-01-14, 10:00:00",5,100,101,-500,-1,501,0,5,O
Trades,Header,DataDiscriminator,Asset Category,Currency,Account,Symbol,Date/Time,Quantity,T. Price,,Proceeds,Comm in USD,,,MTM in USD,Code
Trades,Data,Order,Forex,USD,U98768440,EUR.USD,"2025-01-15, 04:00:00",100,1.1,,-110,-2,,,0,AFx
Dividends,Header,Currency,Account,Date,Description,Amount
Dividends,Data,USD,U98768440,2025-01-15,ABC Cash Dividend,3
Financial Instrument Information,Header,Asset Category,Symbol,Description,Conid,Security ID,Underlying,Listing Exch,Multiplier,Type
Financial Instrument Information,Data,Stocks,ABC,ABC INC,1,US0,ABC,NYSE,1,COMMON
"""


class StatementRebuildTests(unittest.TestCase):
    def test_cash_positions_and_prices_add_up_to_each_days_value(self):
        def closes(ticker, start, end):
            days = pd.bdate_range("2025-01-10", "2025-01-20")
            return pd.Series(110.0 if ticker == "ABC" else 1.2, index=days)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "statement.csv"
            path.write_text(STATEMENT)
            with patch.object(ib_statement, "yahoo_closes", side_effect=closes):
                frame, notes = ib_statement.rebuild_nav(path)
            self.assertEqual(ib_statement.statement_account(path), "U98768440")
        by_day = frame.set_index("date")
        self.assertAlmostEqual(by_day.loc["2025-01-13", "nav"], 1000.0)
        # Jan 14: 1000 - 501 cash + 5 shares at 110; the cancelled and re-sent deposit nets to zero flow.
        self.assertAlmostEqual(by_day.loc["2025-01-14", "nav"], 499 + 550)
        self.assertEqual(by_day.loc["2025-01-14", "flows"], 0.0)
        # Jan 15: bought 100 EUR for 110 USD plus 2 USD commission, and a 3 USD dividend.
        self.assertAlmostEqual(by_day.loc["2025-01-15", "nav"], 499 - 112 + 3 + 100 * 1.2 + 550)
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
