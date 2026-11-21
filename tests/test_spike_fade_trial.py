import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from execution import spike_fade_trial as trial
from execution.hl_trade import AccountSnapshot


class SpikeFadeTrialTests(unittest.TestCase):
    def test_only_new_closed_spikes_are_eligible(self):
        now = 3 * trial.BAR_MS
        paper = {"signals": [
            {"coin": "DOGE", "spike": True, "bar": trial.BAR_MS},
            {"coin": "PUMP", "spike": True, "bar": 2 * trial.BAR_MS},
            {"coin": "WIF", "spike": True, "bar": now},
            {"coin": "PENGU", "spike": False, "bar": 2 * trial.BAR_MS},
        ]}
        self.assertEqual(trial.candidates(paper, now - 1, now), ["PUMP"])

    def test_risk_limit_and_attempt_are_in_place_before_any_send(self):
        now = 3 * trial.BAR_MS
        paper = {"rule": [0.12, 3, 4], "signals": [
            {"coin": "PUMP", "spike": True, "bar": 2 * trial.BAR_MS},
        ]}
        state = {"wallet": "07f2", "budget": 250, "armed_at": now - 1}
        daemon, ht = Mock(), Mock()
        wallet = "07f2"
        daemon.wallet.return_value = wallet
        daemon.exchanges = {wallet: Mock()}
        daemon.ladders.ladders = {}
        daemon.state.wallets = [wallet]
        daemon.state.markets.return_value = {"PUMP": SimpleNamespace(mark_price=1)}
        daemon._other_book.return_value = SimpleNamespace(equity_usd=0, risk_usd=0)
        ht.kill_switch_engaged.return_value = False
        ht.fetch_open_orders.return_value = []
        ht._current_position_quantity.return_value = 0
        ht.MIN_ORDER_VALUE_USD = 10
        ht.urgent_fees.return_value = AccountSnapshot(1000, 0, 1000, 0, (), 0.00045, 0.00045, (wallet,))
        plan = SimpleNamespace(block_reason="", quantity=25, risk_budget=9.99, taker_fee_rate=0.00045)
        ht.plan_trades.return_value = ([plan], 0)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trial.json"

            def check_before_send(*args, **kwargs):
                self.assertEqual(json.loads(path.read_text())["attempt"]["coin"], "PUMP")
                self.assertEqual(ht.plan_trades.call_args.kwargs["reviewed_limits"]["PUMP"][1], 10)
                return [], []

            ht.execute_plans.side_effect = check_before_send
            with patch.object(trial, "desk_exposures", return_value=[]), patch.object(trial.time, "time", return_value=now / 1000):
                trial.attempt(daemon, ht, path, state, paper, now)
            ht.execute_plans.assert_called_once()

    def test_monitor_closes_only_verified_trial_size_and_flags_drawdown(self):
        daemon, ht = Mock(), Mock()
        daemon.wallet.return_value = "07f2"
        exchange = Mock()
        daemon.exchanges = {"07f2": exchange}
        daemon.state.markets.return_value = {"DOGE": SimpleNamespace(mark_price=2.1, size_decimals=0)}
        ht._current_position_quantity.return_value = -25
        ht.kill_switch_engaged.return_value = False
        ht._book_mid.return_value = 2.1
        ht._round_price.return_value = 2.1105
        ht._status_rows.return_value = [{"filled": {"totalSz": "25"}}]
        state = {"wallet": "07f2", "budget": 250, "attempt": {
            "coin": "DOGE", "started": 1000, "hold_ms": 1000, "quantity": 25, "taker_fee_rate": 0.00045,
        }}
        ht.post_info.side_effect = [[{"coin": "DOGE", "side": "A", "oid": 1, "tid": 1, "time": 1001,
                                     "startPosition": "0", "sz": "25", "px": "1", "closedPnl": "0", "fee": "0.01"}], []]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trial.json"
            trial.monitor(daemon, ht, path, state, 2200)
            self.assertTrue(json.loads(path.read_text())["attempt"]["noticed"])
        exchange.order.assert_called_once_with("DOGE", True, 25, 2.1105, {"limit": {"tif": "Ioc"}}, reduce_only=True)
        daemon.alerts.set.assert_called_once()
