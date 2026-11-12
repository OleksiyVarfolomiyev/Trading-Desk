import unittest
from types import SimpleNamespace
from unittest import mock

from desk import day_change
from desk.overview import funding_total


class PositionFundingTests(unittest.TestCase):
    def test_rate_is_from_the_holders_side_per_8_hours(self):
        long_btc = SimpleNamespace(symbol="BTC", ref="", quantity=1.0)
        short_eth = SimpleNamespace(symbol="ETH", ref="", quantity=-2.0)
        snapshot = SimpleNamespace(venue="hl", label="0x07f2", positions=[long_btc, short_eth])
        contexts = {("HL", "BTC"): {"funding": "0.0001"}, ("HL", "ETH"): {"funding": "0.0001"}}
        with mock.patch("desk.hl.account_analytics.fetch_market_contexts", return_value=contexts):
            rates = day_change.position_funding([snapshot])
        self.assertAlmostEqual(rates["0x07f2", "BTC"], -0.0008)   # a long pays when the rate is positive
        self.assertAlmostEqual(rates["0x07f2", "ETH"], 0.0008)    # a short earns it

    def test_total_weights_by_gross_value_and_skips_unpriced(self):
        self.assertAlmostEqual(funding_total([1000, -500, 200], [0.0008, -0.0004, float("nan")]), 0.0004)


if __name__ == "__main__":
    unittest.main()
