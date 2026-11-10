"""The desk's 24-hour change net of deposits: per-venue sources, the total and the dashboard tile."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from desk import day_change
from desk.venues import yahoo
from desk.venues.base import VenueAccount, VenuePosition


def account(venue, label, equity, positions=(), account_id="acct"):
    return VenueAccount(venue=venue, account=account_id, label=label, equity_usd=equity, cash_usd=0.0,
                        margin_used_usd=0.0, available_usd=0.0, unrealized_usd=0.0, positions=tuple(positions))


def stock(symbol, quantity, mark=100.0):
    return VenuePosition(venue="ib", account="acct", symbol=symbol, asset_class="equity", quantity=quantity,
                         avg_price=mark, mark_price=mark)


def token(symbol, quantity, price, ref):
    return VenuePosition(venue="onchain", account="w", symbol=symbol, asset_class="crypto spot",
                         quantity=quantity, avg_price=float("nan"), mark_price=price, ref=ref)


class YahooPriceChangeTests(unittest.TestCase):
    def reply(self, stamps, closes, price=110.0, currency="USD"):
        response = SimpleNamespace(ok=True, status_code=200)
        response.json = lambda: {"chart": {"result": [{
            "meta": {"regularMarketPrice": price, "currency": currency}, "timestamp": stamps,
            "indicators": {"quote": [{"close": closes}]}}]}}
        return response

    def test_price_then_is_the_last_bar_at_or_before_the_cutoff(self):
        reply = self.reply([100, 200, 300, 400], [90.0, None, 95.0, 108.0])
        with patch("requests.get", return_value=reply):
            then, now, currency = yahoo.price_change("NET", pd.Timestamp(350, unit="s", tz="UTC"))
        self.assertEqual((then, now, currency), (95.0, 110.0, "USD"))

    def test_no_bar_that_old_raises(self):
        with patch("requests.get", return_value=self.reply([500], [100.0])), self.assertRaises(ValueError):
            yahoo.price_change("NET", pd.Timestamp(100, unit="s", tz="UTC"))


class YahooExtendedPriceTests(unittest.TestCase):
    def test_last_extended_bar_and_the_pre_to_post_session(self):
        response = SimpleNamespace(ok=True, status_code=200)
        response.json = lambda: {"chart": {"result": [{
            "meta": {"currentTradingPeriod": {"pre": {"start": 100}, "post": {"end": 900}}},
            "timestamp": [700, 800, 850], "indicators": {"quote": [{"close": [733.0, 734.2, None]}]}}]}}
        with patch("requests.get", return_value=response):
            price, stamp, start, end = yahoo.extended_price("MU")
        self.assertEqual((price, stamp.timestamp(), start.timestamp(), end.timestamp()), (734.2, 800, 100, 900))


class DayChangeTests(unittest.TestCase):
    def test_wallet_change_is_value_times_r_over_one_plus_r(self):
        positions = [token("VVV", 10, 11.0, "base:0xabc")]
        key = day_change.token_keys(positions)["base:0xabc"]
        # Worth $110 after a +10% day: it was $100, so the move is +$10.
        self.assertAlmostEqual(day_change.wallet_change(positions, {key: 0.10}), 10.0)
        with self.assertRaises(ValueError):
            day_change.wallet_change(positions, {})

    def test_holdings_change_uses_yahoo_moves_and_rejects_other_currencies(self):
        with patch.object(yahoo, "price_change", return_value=(100.0, 103.0, "USD")):
            self.assertAlmostEqual(day_change.holdings_change([stock("NET", 5)], pd.Timestamp.now(tz="UTC")), 15.0)
        with patch.object(yahoo, "price_change", return_value=(100.0, 103.0, "EUR")), self.assertRaises(ValueError):
            day_change.holdings_change([stock("SAP", 5)], pd.Timestamp.now(tz="UTC"))

    def test_desk_total_adds_venues_and_names_the_ones_left_out(self):
        snapshots = [account("hl", "HL 07f2", 3000.0, account_id="0x1"),
                     account("ib", "IB 8440", 3500.0, [stock("NET", 5)]),
                     account("onchain", "HL c248 wallet", 2200.0, [token("VVV", 200, 11.0, "base:0xabc")])]
        with patch.object(day_change, "hl_change", return_value=-50.0), \
                patch.object(day_change, "holdings_change", side_effect=ValueError("no bars")), \
                patch("desk.venues.onchain.fetch_changes_24h", return_value={"base:0xabc": 0.10}):
            result = day_change.desk_day_change(snapshots)
        self.assertEqual(result.missing, ["IB 8440"])
        self.assertAlmostEqual(result.parts["HL c248 wallet"], 200.0)
        self.assertAlmostEqual(result.change, 150.0)
        # The % is over the measured accounts' value 24 hours ago: 150 / (5200 - 150).
        self.assertAlmostEqual(result.pct, 150.0 / 5050.0)

    def test_nothing_measurable_is_none(self):
        with patch.object(day_change, "hl_change", side_effect=ValueError("down")):
            self.assertIsNone(day_change.desk_day_change([account("hl", "HL 07f2", 3000.0)]))


if __name__ == "__main__":
    unittest.main()
