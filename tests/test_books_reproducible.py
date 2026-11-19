"""A book build that left something out must not replace a whole one, and a failed Yahoo fetch is not "no closes"."""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import beta_attribution, web_sync  # noqa: E402
from desk.venues import ib_statement  # noqa: E402


def reply(status, body=None):
    response = mock.Mock(status_code=status, ok=status < 400)
    response.json.return_value = body or {}
    response.raise_for_status.side_effect = requests.HTTPError(f"{status}") if status >= 400 else None
    return response


class KeepPreviousBooksTests(unittest.TestCase):
    def saved(self, age_seconds):
        folder = Path(tempfile.mkdtemp())
        path = folder / "books.json"
        path.write_text("{}")
        old = time.time() - age_seconds
        import os

        os.utime(path, (old, old))
        return path

    def test_whole_build_is_published(self):
        self.assertFalse(web_sync.keep_previous_books({"incomplete": {}}, self.saved(60)))

    def test_partial_build_keeps_a_recent_whole_one(self):
        self.assertTrue(web_sync.keep_previous_books({"incomplete": {"stocks": ["HL c248"]}}, self.saved(3600)))

    def test_partial_build_is_published_once_the_saved_one_is_old(self):
        self.assertFalse(web_sync.keep_previous_books({"incomplete": {"stocks": ["HL c248"]}},
                                                      self.saved(web_sync.INCOMPLETE_KEEP_SECONDS + 60)))

    def test_partial_build_with_nothing_saved_is_published(self):
        self.assertFalse(web_sync.keep_previous_books({"incomplete": {"stocks": ["x"]}}, Path("/nonexistent/books.json")))


class YahooStrictTests(unittest.TestCase):
    start, end = pd.Timestamp("2026-01-01", tz="UTC"), pd.Timestamp("2026-02-01", tz="UTC")

    def test_throttled_request_raises_when_strict(self):
        with mock.patch.object(ib_statement.requests, "get", return_value=reply(429)), mock.patch("time.sleep"):
            with self.assertRaises(requests.HTTPError):
                ib_statement.yahoo_closes("AAPL", self.start, self.end, strict=True)

    def test_throttled_request_is_empty_when_not_strict(self):
        with mock.patch.object(ib_statement.requests, "get", return_value=reply(429)), mock.patch("time.sleep"):
            self.assertTrue(ib_statement.yahoo_closes("AAPL", self.start, self.end).empty)

    def test_unknown_symbol_is_empty_even_when_strict(self):
        with mock.patch.object(ib_statement.requests, "get", return_value=reply(404)):
            self.assertTrue(ib_statement.yahoo_closes("COOP", self.start, self.end, strict=True).empty)


class AssetClosesTests(unittest.TestCase):
    def test_a_dex_prefixed_market_reads_its_own_dex(self):
        seen = {}

        def candles(coin, dex="", lookback_days=0):
            seen["call"] = (coin, dex)
            return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=3, tz="UTC"), "close": [1.0, 2.0, 3.0]})

        with mock.patch("desk.hl.trade_analytics.fetch_daily_candles", candles):
            for asset, dex in (("io:IONQ", "io"), ("para:SOFI", "para"), ("xyz:AAPL", "xyz")):
                beta_attribution.asset_closes_for(SimpleNamespace(kind="Stocks"), asset, "hl", 100)
                self.assertEqual(seen["call"], (asset, dex))


if __name__ == "__main__":
    unittest.main()
