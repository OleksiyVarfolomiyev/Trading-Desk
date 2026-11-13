"""On the desk server (no TWS) the risk book takes IB from the Mac's last push to the relay."""

import os
import unittest
from unittest import mock

import pandas as pd

from desk import risk
from desk.accounts import Account


def payload(minutes_old: float) -> dict:
    return {"label": "IB 8440", "as_of": (pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=minutes_old)).isoformat(),
            "account": {"equity": 3600.0, "cash": 100.0, "margin_used": 50.0, "available": 3000.0, "unrealized": 20.0},
            "risk": {"equity": 3600.0, "risk": 249.0, "positions": 2},
            "positions": [{"symbol": "MU", "asset_class": "equity", "quantity": 1.0, "avg_price": 700.0, "mark_price": 816.0,
                           "multiplier": 1, "unrealized": 116.0},
                          {"symbol": "ES", "asset_class": "futures", "quantity": -1.0, "avg_price": 5000.0,
                           "mark_price": 5010.0, "multiplier": 50, "unrealized": -500.0}]}


class IbFromRelayTests(unittest.TestCase):
    account = Account(venue="ib", id="U0008440", label="IB 8440")

    def test_entry_and_positions_come_from_the_push(self):
        entry, snapshot = risk.ib_entry_from_relay(self.account, payload(5))
        self.assertEqual((entry.equity_usd, entry.risk_usd, entry.positions), (3600.0, 249.0, 2))
        self.assertEqual([position.notional_usd for position in snapshot.positions], [816.0, -1.0 * 5010.0 * 50])
        self.assertIn("5 minutes ago", entry.notes[0])

    def test_a_stale_push_raises_so_the_account_is_unreachable(self):
        with self.assertRaises(ValueError):
            risk.ib_entry_from_relay(self.account, payload(60 * (risk.IB_RELAY_STALE_HOURS + 1)))

    def test_the_book_uses_the_relay_when_told_and_marks_it(self):
        with mock.patch.dict(os.environ, {"DESK_IB_SOURCE": "relay"}), \
                mock.patch("desk.web_sync.relay_get", return_value=payload(1)), \
                mock.patch.object(risk, "ib_book_entry", side_effect=AssertionError("TWS must not be read")):
            book = risk.other_venue_book("hl", accounts=[self.account])
        self.assertTrue(book.ib_from_relay)
        self.assertEqual(book.risk_usd, 249.0)
        self.assertEqual(book.unreachable, [])


if __name__ == "__main__":
    unittest.main()
