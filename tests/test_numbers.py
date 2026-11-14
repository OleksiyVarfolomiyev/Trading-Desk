"""Display helpers shared by every client surface (desk/numbers.py)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class MarketNameTests(unittest.TestCase):
    def test_venue_prefixes_drop_from_tickers_only(self):
        from desk.numbers import market_frame, market_names

        self.assertEqual(market_names("xyz:AAPL"), "AAPL")
        self.assertEqual(market_names("io:IONQ -$12, para:ANSEM +$3 (2)"), "IONQ -$12, ANSEM +$3 (2)")
        self.assertEqual(market_names("Nov 12, 10:30 am"), "Nov 12, 10:30 am")      # a time is not a venue
        self.assertEqual(market_names("note: BTC"), "note: BTC")                    # prose has a space after the colon
        self.assertEqual(market_names("https://example.com"), "https://example.com")
        self.assertEqual(market_names(5.0), 5.0)
        import pandas as pd

        frame = pd.DataFrame({"Ticker": ["xyz:AAPL", "BTC"], "Value": [1.0, 2.0]}, index=["xyz:AAPL", "BTC"])
        shown = market_frame(frame)
        self.assertEqual(list(shown["Ticker"]), ["AAPL", "BTC"])
        self.assertEqual(list(shown.index), ["AAPL", "BTC"])
        self.assertEqual(list(frame["Ticker"]), ["xyz:AAPL", "BTC"])                # the data itself is untouched
