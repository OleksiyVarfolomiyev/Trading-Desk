import tempfile
import unittest
from pathlib import Path

import pandas as pd

from desk import market_caps


class MarketCaps(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "market_caps.csv"
        self.top = {"BTC": (2e12, 5e10), "BONK": (1e9, 2e8), "NOPE": (5e6, 1e5)}
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return self.top

    def refresh(self, day):
        return market_caps.refresh(pd.Timestamp(day, tz="UTC"), self.path, symbols={"BTC", "BONK"}, fetch=self.fetch)

    def test_names_match_coingecko_symbols(self):
        self.assertEqual([market_caps.gecko_symbol(n) for n in ("kBONK", "1000PEPE", "xyz:AAPL", "BTC", "kPEPE")],
                         ["BONK", "PEPE", "AAPL", "BTC", "PEPE"])

    def test_one_fetch_a_day_for_the_desk_universe_only(self):
        self.assertEqual(self.refresh("2026-11-17"), 2)
        self.assertEqual(self.refresh("2026-11-17"), 0)   # already saved today: no request
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.refresh("2026-11-18"), 2)
        self.assertEqual(self.calls, 2)
        self.assertEqual(list(market_caps.history("kBONK", self.path).index.strftime("%Y-%m-%d")), ["2026-11-17", "2026-11-18"])
        self.assertEqual(set(market_caps.latest(self.path).index), {"BTC", "BONK"})


if __name__ == "__main__":
    unittest.main()
