"""What the desk's Mac sends the web relay: the IB account and saved HL history, by last four only."""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from desk import web_sync

ACCOUNT = "U9876543"
WALLET = "0x" + "ab" * 18 + "07f2"


def snapshot():
    position = SimpleNamespace(symbol="AAPL", asset_class="equity", quantity=10.0, avg_price=150.0, mark_price=160.0,
                               fx_to_usd=1.0, multiplier=1.0, unrealized_usd=100.0)
    option = SimpleNamespace(symbol="SPY 600C", asset_class="option", quantity=2.0, avg_price=3.0, mark_price=float("nan"),
                             fx_to_usd=1.0, multiplier=100.0, unrealized_usd=float("nan"))
    return SimpleNamespace(account=ACCOUNT, label="IB 6543", positions=[position, option], equity_usd=5000.0,
                           cash_usd=3400.0, margin_used_usd=0.0, available_usd=3400.0, unrealized_usd=100.0)


class IbPayloadTests(unittest.TestCase):
    def test_backfill_before_flex_and_only_the_last_four(self):
        with tempfile.TemporaryDirectory() as folder:
            pd.DataFrame({"date": ["2025-01-13", "2025-01-14", "2025-01-15"], "nav": [0, 1000, 1005],
                          "flows": [0, 1000, 0]}).to_csv(Path(folder) / "ib_nav_backfill_6543.csv", index=False)
            pd.DataFrame({"date": ["2025-01-15", "2025-01-16"], "nav": [1010, 1020], "flows": [0, 0]}).to_csv(
                Path(folder) / "ib_nav_history_6543.csv", index=False)
            entry = SimpleNamespace(equity_usd=5000.0, risk_usd=120.0, positions=2)
            with patch.dict(os.environ, {"DESK_IB_DATA_DIR": folder}):
                payload = web_sync.ib_payload(snapshot(), entry, {("IB 6543", "AAPL"): 0.01})
        self.assertEqual(payload["id"], "6543")
        self.assertEqual([row[0] for row in payload["nav"]], ["2025-01-13", "2025-01-14", "2025-01-15", "2025-01-16"])
        self.assertEqual(payload["nav"][2][1], 1010.0)  # Flex wins where both have the day
        self.assertEqual(payload["positions"][0]["change24h"], 0.01)
        self.assertIsNone(payload["positions"][1]["mark_price"])  # NaN is sent as null
        self.assertEqual(payload["risk"], {"equity": 5000.0, "risk": 120.0, "positions": 2})
        text = json.dumps(payload, allow_nan=False)
        self.assertNotIn(ACCOUNT, text)
        self.assertIsNone(re.search(r"(?:DU|U)\d{5,}", text))  # the relay refuses these


class SeedHistoryTests(unittest.TestCase):
    def test_one_point_per_day_sent_under_the_last_four(self):
        with tempfile.TemporaryDirectory() as folder:
            pd.DataFrame({"ts": ["2026-10-01 10:00:00+00:00", "2026-10-01 20:00:00+00:00", "2026-10-02 09:00:00+00:00"],
                          "accountValue": [100.0, 110.0, 120.0], "pnl": [0.0, 10.0, 20.0]}).to_csv(
                Path(folder) / "hl_portfolio_history_07f2.csv", index=False)
            sent = {}
            with patch.object(web_sync, "load_accounts", return_value=[SimpleNamespace(venue="hl", id=WALLET)]), \
                    patch.object(web_sync, "accounts_for", side_effect=lambda venue, accounts: accounts), \
                    patch.object(web_sync, "_post", side_effect=lambda path, body: sent.update({path: body}) or {"days": 2}):
                self.assertEqual(web_sync.seed_history(folder), {"07f2": 2})
        self.assertEqual(list(sent), ["/ingest/history/07f2"])
        self.assertEqual([point[1:] for point in sent["/ingest/history/07f2"]["points"]], [[110.0, 10.0], [120.0, 20.0]])
        self.assertNotIn(WALLET, json.dumps(sent))


class DailySyncTests(unittest.TestCase):
    def run_daily(self, read_ib):
        accounts = [SimpleNamespace(venue="hl", id=WALLET), SimpleNamespace(venue="ib", id=ACCOUNT)]
        with patch.object(web_sync, "load_accounts", return_value=accounts), \
                patch("desk.hl.account_analytics.update_portfolio_history") as hl_history, \
                patch("desk.venues.ib_flex.update_nav_history") as flex, \
                patch.object(web_sync, "read_ib", side_effect=read_ib), \
                patch.object(web_sync, "push_ib", return_value={"ok": True}) as push, \
                patch.object(web_sync, "push_nav_only", return_value={"ok": True}) as nav_only, \
                patch("desk.day_change.position_changes", return_value={}):
            report = web_sync.daily_sync("data")
        return report, hl_history, flex, push, nav_only

    def test_tws_closed_still_sends_the_nav(self):
        report, hl_history, flex, push, nav_only = self.run_daily(ConnectionRefusedError("TWS is not running"))
        hl_history.assert_called_once()
        flex.assert_called_once()
        push.assert_not_called()
        nav_only.assert_called_once()
        self.assertIn("daily NAV sent", report[-1])
        self.assertNotIn(ACCOUNT, " ".join(report))
        self.assertNotIn(WALLET, " ".join(report))

    def test_tws_open_sends_the_account(self):
        snap = snapshot()
        report, *_, push, nav_only = self.run_daily(lambda account: (None, snap))
        push.assert_called_once()
        nav_only.assert_not_called()
        self.assertEqual(report, ["HL 07f2: history saved", "IB 6543: Flex NAV saved", "IB 6543: sent to the relay (2 positions)"])

    def test_the_job_runs_daily_from_the_repository(self):
        job = web_sync.job_plist()
        self.assertEqual(job["ProgramArguments"][-2:], ["desk.web_sync", "--daily"])
        self.assertEqual(job["ProgramArguments"][0], sys.executable)
        self.assertEqual(Path(job["WorkingDirectory"]), web_sync.REPO)
        self.assertEqual(len(job["StartCalendarInterval"]), 2)


class TokenTests(unittest.TestCase):
    def test_store_token_refuses_a_terminal(self):
        with patch("sys.stdout.isatty", return_value=True), self.assertRaises(SystemExit):
            web_sync.main(["--store-token"])


if __name__ == "__main__":
    unittest.main()
