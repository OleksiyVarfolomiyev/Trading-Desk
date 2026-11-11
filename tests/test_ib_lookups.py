"""IB contract lookups for history fail fast once IB stops answering them."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from desk.venues import ib as ib_venue


class SlowIB:
    def __init__(self):
        self.calls = 0

    def qualifyContracts(self, contract):
        self.calls += 1
        raise TimeoutError()


class QualifyForHistoryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(ib_venue, "_details_down_until", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_timeout_skips_later_lookups_on_every_connection(self):
        first, second = SlowIB(), SlowIB()
        contract = SimpleNamespace(conId=1, secType="STK", symbol="AAPL", currency="USD")
        with self.assertRaises(ib_venue.HistoryUnavailable):
            ib_venue.qualify_for_history(first, contract)
        self.assertTrue(ib_venue.contract_lookups_down())
        for client in (first, second):
            with self.assertRaises(ib_venue.HistoryUnavailable):
                ib_venue.qualify_for_history(client, contract)
        self.assertEqual((first.calls, second.calls), (1, 0))

    def test_answered_lookups_pass_through(self):
        ib = SimpleNamespace(qualifyContracts=lambda contract: [contract])
        contract = SimpleNamespace(conId=1, secType="STK", symbol="AAPL", currency="USD")
        self.assertEqual(ib_venue.qualify_for_history(ib, contract), [contract])
        self.assertFalse(ib_venue.contract_lookups_down())


if __name__ == "__main__":
    unittest.main()


class DayChangeTotalTests(unittest.TestCase):
    def test_longs_and_shorts_over_yesterdays_gross_value(self):
        from desk.overview import day_change_total

        # Long worth 110 after +10% (was 100, +10); short worth -90 after -10% (was -100, +10);
        # a wallet token with no 24h change is left out.
        self.assertAlmostEqual(day_change_total([110.0, -90.0, 50.0], [0.10, -0.10, float("nan")]), 20 / 200)
        self.assertTrue(day_change_total([10.0], [float("nan")]) != day_change_total([10.0], [float("nan")]))  # NaN
