"""The HL trading daemon, the executor's fast paths it uses, and the IB quote fallback."""

import os as _os
import tempfile as _tempfile

# Trades in these tests must never write to the desk's real journal.
_os.environ["DESK_JOURNAL_FILE"] = _os.path.join(_tempfile.mkdtemp(prefix="desk-journal-"), "trade_journal.json")
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "execution")]

import hl_daemon  # noqa: E402
import hl_trade as ht  # noqa: E402
import ib_trade  # noqa: E402
from desk import risk  # noqa: E402
from desk.hl import daemon_link  # noqa: E402
from desk.hl.trade_analytics import ExpectedPnl  # noqa: E402
from desk.venues import ib as ib_venue  # noqa: E402

hl_daemon.load_trading()

WALLET = "0x" + "a" * 36 + "07f2"
OTHER = "0x" + "b" * 36 + "c248"
META = {"universe": [{"name": "AVAX", "szDecimals": 2, "maxLeverage": 10},
                     {"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}
CTXS = [{"markPx": "8.0", "oraclePx": "8.0"}, {"markPx": "70000", "oraclePx": "70000"}]


def account_state(positions=(), value="1000", used="100"):
    return {"marginSummary": {"accountValue": value, "totalMarginUsed": used},
            "assetPositions": [{"position": {"coin": coin, "szi": str(size), "entryPx": str(entry),
                                             "marginUsed": "50", "leverage": {"type": "cross", "value": 4}}}
                               for coin, size, entry in positions]}


def candles(count=60, level=8.0):
    times = pd.date_range("2026-08-01", periods=count, freq="D", tz="UTC")
    closes = np.full(count, level)
    return pd.DataFrame({"time": times, "open": closes, "high": closes + 0.3, "low": closes - 0.3, "close": closes})


def funding(count=600, rate=0.00001):
    return pd.DataFrame({"time": pd.date_range("2026-09-01", periods=count, freq="h", tz="UTC"),
                         "fundingRate": np.full(count, rate)})


def live_state(positions=(("AVAX", -10, 8.0),)):
    state = hl_daemon.HLState([WALLET])
    state.dexes = [""]
    state.meta[""], state.ctxs[""], state.ctx_time[""] = META, CTXS, time.time()
    state.unified[WALLET], state.fees[WALLET] = False, (0.0001, 0.0004)
    state.on_wallet(WALLET, "allDexsClearinghouseState", {"clearinghouseStates": [["", account_state(positions)]]})
    return state


def active_trade(quantity=10.0, budget=10.0, stop_oid=11, target_oid=12):
    plan = ht.PlannedTrade(
        coin="AVAX", dex="", side="SHORT", quantity=quantity, entry_reference=8.0, stop_price=9.0, target_price=7.0,
        stop_distance=1.0, funding_reserve_per_unit=0.0, risk_budget=budget,
        expected_pnl=ExpectedPnl(0.0, 0.0, 0.0, 0.0, 0, 0), maker_fee_rate=0.0, taker_fee_rate=0.0, leverage=4,
        price_stop_distance=1.0)
    return ht.ActiveTrade(plan=plan, quantity=quantity, entry_price=8.0, baseline_quantity=0.0,
                          stop_oid=stop_oid, target_oid=target_oid, started_at=pd.Timestamp.now(tz="UTC"))


def filled(size, price, oid=1):
    return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": str(size), "avgPx": str(price),
                                                                            "oid": oid}}]}}}


def statuses(*items):
    return {"status": "ok", "response": {"data": {"statuses": list(items)}}}


class LiveStateTests(unittest.TestCase):
    def test_websocket_state_builds_the_executor_snapshot_without_requests(self):
        state = live_state()
        with patch.object(ht, "fetch_user_state", side_effect=AssertionError("no REST read expected")), \
                patch.object(ht, "parse_fee_rates", side_effect=AssertionError("no REST read expected")):
            snapshot = state.snapshot([WALLET])
        self.assertEqual(snapshot.equity, 1000.0)
        self.assertEqual(snapshot.margin_used, 100.0)
        self.assertEqual([(p.coin, p.quantity) for p in snapshot.positions], [("AVAX", -10.0)])
        self.assertEqual((snapshot.maker_fee_rate, snapshot.taker_fee_rate), (0.0001, 0.0004))

    def test_a_state_missing_a_dex_is_not_fresh_and_is_read_again(self):
        state = live_state()
        state.dexes = ["", "xyz"]
        state.on_wallet(WALLET, "allDexsClearinghouseState", {"clearinghouseStates": [["", account_state()]]})
        state.state_time[WALLET] = 0.0
        state.meta["xyz"], state.ctxs["xyz"] = {"universe": []}, []
        with patch.object(ht, "fetch_user_state", return_value=account_state()) as read:
            state.snapshot([WALLET])
        self.assertEqual(read.call_count, 2)  # both DEXes, read again

    def test_a_coin_price_from_the_websocket_overrides_the_slower_feed_while_fresh(self):
        state = live_state()
        state.on_market("activeAssetCtx", {"coin": "AVAX", "ctx": {"markPx": "9.0", "oraclePx": "9.1"}})
        self.assertEqual(state.markets()["AVAX"].mark_price, 9.0)
        state.coin_ctx["AVAX"] = (state.coin_ctx["AVAX"][0], time.time() - 60)
        self.assertEqual(state.markets()["AVAX"].mark_price, 8.0)

    def test_trading_a_coin_without_a_fresh_price_reads_its_dex_first(self):
        state = live_state()
        response = [META, [{"markPx": "8.4", "oraclePx": "8.4"}, CTXS[1]]]
        with patch.object(ht, "fetch_market_response", return_value=response) as read:
            markets = state.markets([("AVAX", "")])
        read.assert_called_once_with("")
        self.assertEqual(markets["AVAX"].mark_price, 8.4)

    def test_a_new_listing_marks_the_metadata_stale(self):
        state = live_state()
        state.on_market("allDexsAssetCtxs", {"ctxs": [["", CTXS + [{"markPx": "1", "oraclePx": "1"}]]]})
        self.assertTrue(state.meta_stale)

    def test_unified_collateral_comes_from_the_websocket_usdc_balance(self):
        state = live_state()
        state.unified[WALLET] = True
        state.on_wallet(WALLET, "spotState", {"spotState": {"balances": [{"coin": "USDC", "total": "1234.5"}]}})
        with patch.object(ht, "fetch_collateral_equity", side_effect=AssertionError("no REST read expected")):
            self.assertEqual(state.collateral(WALLET, 400.0), 1234.5)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.state = live_state()
        self.exchange = MagicMock()
        # The take-profit (order 12) rests at 7.0 until the guard reads it; no test reaches HL.
        status = patch.object(ht, "_order_status", return_value={"order": {"status": "open", "order": {"limitPx": "7.0"}}})
        self.order_status = status.start()
        self.addCleanup(status.stop)
        logging = patch.object(hl_daemon, "log")
        logging.start()
        self.addCleanup(logging.stop)
        self.guards = hl_daemon.Guards(self.state, path=None, check=MagicMock(return_value=""))
        self.guards.add(WALLET, active_trade(), self.exchange)
        self.entry = self.guards.entries[(WALLET, "AVAX")]

    def price(self, mark):
        self.state.on_market("activeAssetCtx", {"coin": "AVAX", "ctx": {"markPx": str(mark), "oraclePx": str(mark)}})

    def test_a_price_past_the_loss_limit_closes_the_tranche_at_once(self):
        self.price(8.5)  # short 10 from 8.0: -$5, inside the $10 limit
        self.guards.fast_check(self.entry, time.time())
        self.exchange.market_close.assert_not_called()
        self.price(9.05)  # -$10.50
        self.guards.fast_check(self.entry, time.time())
        self.exchange.market_close.assert_called_once_with("AVAX", 10.0)
        self.assertTrue(self.entry.trade.exiting)

    def test_a_filled_stop_is_checked_as_soon_as_hl_reports_it(self):
        self.entry.next_check = time.time() + 3600
        self.guards.check.return_value = "AVAX: stop filled."
        self.guards.on_event("orders", WALLET, [{"order": {"oid": 11}, "status": "filled"}])
        self.assertEqual(self.entry.next_check, 0.0)
        with patch.object(ht, "fetch_funding_history", return_value=funding()):
            self.guards.step(time.time())
        self.guards.check.assert_called_once()
        self.assertNotIn((WALLET, "AVAX"), self.guards.entries)

    def test_a_failing_check_keeps_the_guard(self):
        self.guards.check.side_effect = RuntimeError("HL down")
        with patch.object(ht, "fetch_funding_history", return_value=funding()), patch.object(hl_daemon, "log"):
            self.guards.step(time.time())
        self.assertIn((WALLET, "AVAX"), self.guards.entries)
        self.assertIsNotNone(self.entry.failing_since)

    def test_guards_are_saved_and_resumed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guards.json"
            saved = hl_daemon.Guards(self.state, path=path)
            trade = active_trade()
            trade.plan = ht.PlannedTrade(**{**trade.plan.__dict__, "leverage": np.int64(4)})
            saved.add(WALLET, trade, self.exchange)
            [(wallet, resumed)] = hl_daemon.Guards(self.state, path=path).load()
        self.assertEqual(wallet, WALLET)
        self.assertEqual(resumed.plan.leverage, 4)
        self.assertEqual((resumed.stop_oid, resumed.target_oid, resumed.quantity), (11, 12, 10.0))
        self.assertEqual(resumed.started_at, trade.started_at)

    # -- take-profits held in memory (the short's target is 7.0, its stop distance 1.0)
    def test_a_take_profit_far_from_the_price_comes_off_the_book_and_into_memory(self):
        self.price(8.0)
        self.guards.manage_target(self.entry, time.time())
        self.exchange.cancel.assert_called_once_with("AVAX", 12)
        self.assertEqual((self.entry.trade.target_oid, self.entry.trade.hidden_target), (0, 7.0))
        self.exchange.cancel.reset_mock()
        self.guards.manage_target(self.entry, time.time())    # nothing more to do while the price stays away
        self.exchange.cancel.assert_not_called()
        self.exchange.order.assert_not_called()

    def test_a_take_profit_goes_on_the_book_as_the_price_nears_it(self):
        self.entry.trade.target_oid, self.entry.trade.hidden_target = 0, 7.0
        self.price(7.15)                                       # 0.15 away: not yet (within 0.10)
        self.guards.manage_target(self.entry, time.time())
        self.exchange.order.assert_not_called()
        self.exchange.order.return_value = statuses({"resting": {"oid": 99}})
        self.price(7.08)
        self.guards.manage_target(self.entry, time.time())
        self.exchange.order.assert_called_once_with("AVAX", True, 10.0, 7.0, {"limit": {"tif": "Alo"}}, reduce_only=True)
        self.assertEqual(self.entry.trade.target_oid, 99)
        self.price(7.15)                                       # back out, but inside 0.20: it stays
        self.guards.manage_target(self.entry, time.time())
        self.exchange.cancel.assert_not_called()

    def test_a_price_already_through_the_target_rests_it_just_outside_the_spread(self):
        self.entry.trade.target_oid, self.entry.trade.hidden_target = 0, 7.0
        self.exchange.order.return_value = statuses({"error": "Post only order would have immediately matched"})
        self.price(6.9)
        with patch.object(ht, "_resend_passive_target", return_value=77) as passive:
            self.guards.manage_target(self.entry, time.time())
        passive.assert_called_once()
        self.assertEqual(self.entry.trade.target_oid, 77)

    def test_a_take_profit_that_filled_before_the_cancel_is_left_for_the_check(self):
        self.entry.trade.hidden_target = 7.0
        self.order_status.return_value = {"order": {"status": "filled", "order": {"limitPx": "7.0"}}}
        self.entry.next_check = time.time() + 3600
        self.price(8.0)
        self.guards.manage_target(self.entry, time.time())
        self.assertEqual((self.entry.trade.target_oid, self.entry.next_check), (12, 0.0))

    def test_the_take_profit_in_memory_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "guards.json"
            trade = active_trade(target_oid=0)
            trade.hidden_target = 7.0
            hl_daemon.Guards(self.state, path=path).add(WALLET, trade, self.exchange)
            [(_, resumed)] = hl_daemon.Guards(self.state, path=path).load()
        self.assertEqual((resumed.target_oid, resumed.hidden_target), (0, 7.0))


class HeldTargetTests(unittest.TestCase):
    """Take-profits on positions without a guard: adopted from HL, held in memory, placed as the price nears."""

    def setUp(self):
        self.state = live_state()                                  # short 10 AVAX
        self.exchange = MagicMock()
        daemon = SimpleNamespace(state=self.state, exchanges={WALLET: self.exchange}, guards=SimpleNamespace(entries={}),
                                 watch=lambda coin: None, stopped=threading.Event())
        self.held = hl_daemon.HeldTargets(daemon, path=None)
        self.held._atr = lambda coin, dex: 0.5                     # reach 1.5: placed within 0.15, pulled past 0.30
        self.orders = [{"coin": "AVAX", "oid": 5, "side": "B", "limitPx": "7.0", "sz": "10", "reduceOnly": True},
                       {"coin": "AVAX", "oid": 6, "side": "B", "limitPx": "9.5", "sz": "10", "reduceOnly": True,
                        "isTrigger": True},                         # the stop: never touched
                       {"coin": "AVAX", "oid": 7, "side": "A", "limitPx": "8.2", "sz": "1", "reduceOnly": False,
                        "cloid": "0x1add" + "0" * 28}]              # a ladder quote: the ladder's own
        for name, value in (("fetch_open_orders", lambda wallet, dexes: self.orders),
                            ("_order_status", lambda wallet, oid: {"order": {"status": "canceled"}})):
            patcher = patch.object(ht, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        logging = patch.object(hl_daemon, "log")
        logging.start()
        self.addCleanup(logging.stop)

    def price(self, mark):
        self.state.on_market("activeAssetCtx", {"coin": "AVAX", "ctx": {"markPx": str(mark), "oraclePx": str(mark)}})

    def test_only_the_take_profit_is_adopted_and_pulled_while_the_price_is_far(self):
        self.price(8.0)
        self.held.step(time.time())
        self.assertEqual([(item["oid"], item["price"]) for item in self.held.items], [(None, 7.0)])
        self.exchange.cancel.assert_called_once_with("AVAX", 5)

    def test_it_goes_back_on_the_book_near_the_price_and_is_dropped_once_filled(self):
        self.price(8.0)
        self.held.step(time.time())
        self.exchange.order.return_value = statuses({"resting": {"oid": 9}})
        self.price(7.1)
        self.held.step(time.time())
        self.exchange.order.assert_called_once_with("AVAX", True, 10.0, 7.0, {"limit": {"tif": "Alo"}}, reduce_only=True)
        self.assertEqual(self.held.items[0]["oid"], 9)
        self.orders = []                                            # order 9 is gone from HL: it filled
        self.held.next_scan = 0.0
        self.held.step(time.time())
        self.assertEqual(self.held.items, [])

    def test_guarded_coins_are_left_to_their_guard(self):
        self.held.daemon.guards.entries = {(WALLET, "AVAX"): object()}
        self.price(8.0)
        self.held.step(time.time())
        self.assertEqual(self.held.items, [])
        self.exchange.cancel.assert_not_called()

    def test_a_closed_position_drops_its_take_profit(self):
        self.price(8.0)
        self.held.step(time.time())
        self.state.on_wallet(WALLET, "allDexsClearinghouseState", {"clearinghouseStates": [["", account_state(())]]})
        self.orders = []
        self.held.step(time.time())
        self.assertEqual(self.held.items, [])


class TargetActionTests(unittest.TestCase):
    def test_place_near_pull_far_for_both_sides(self):
        self.assertEqual(hl_daemon.target_action(7.05, 7.0, 1.0, -1.0, on_book=False), "place")   # short nearing
        self.assertEqual(hl_daemon.target_action(7.5, 7.0, 1.0, -1.0, on_book=False), "")
        self.assertEqual(hl_daemon.target_action(7.5, 7.0, 1.0, -1.0, on_book=True), "pull")
        self.assertEqual(hl_daemon.target_action(6.5, 7.0, 1.0, -1.0, on_book=False), "place")    # through it
        self.assertEqual(hl_daemon.target_action(10.95, 11.0, 1.0, 1.0, on_book=False), "place")  # long nearing
        self.assertEqual(hl_daemon.target_action(10.5, 11.0, 1.0, 1.0, on_book=True), "pull")
        self.assertEqual(hl_daemon.target_action(10.5, 0.0, 1.0, 1.0, on_book=False), "")         # no target


class FakeSession:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    def ask(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""


class DaemonTradeTests(unittest.TestCase):
    def setUp(self):
        self.daemon = hl_daemon.Daemon([WALLET], guards_path=None)
        self.daemon.state = live_state(positions=())
        self.daemon.guards = hl_daemon.Guards(self.daemon.state, path=None, check=MagicMock(return_value=""))
        self.daemon.state.other = (risk.DeskBook(), time.time())
        self.exchange = MagicMock()
        self.exchange.update_leverage.return_value = {"status": "ok"}
        # the entry, then the take-profit ladder's rungs (desk/tp_ladder.json: up to three)
        self.exchange.order.side_effect = [filled(30, 7.98)] + [statuses({"resting": {"oid": 3 + i}}) for i in range(5)]
        self.daemon.exchanges[WALLET] = self.exchange
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.signals = Path(folder.name) / "short.csv"
        self.signals.write_text("coin,side,dex\nAVAX,SHORT,\n")
        for target, value in ((ht, "fetch_daily_candles"), (ht, "fetch_funding_history")):
            patcher = patch.object(target, value, return_value=candles() if "candles" in value else funding())
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("_current_position_quantity", 0.0), ("_book_mid", 8.0), ("kill_switch_engaged", False),
                            ("fetch_market_response", [META, CTXS]), ("resting_stops", {})):
            patcher = patch.object(ht, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # No network in tests: the stress check's view of the other venues and the wallets.
        patcher = patch("desk.stress.desk_exposures", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        patcher = patch("sys.stdout", self.output)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, **extra):
        return {"wallet": WALLET[-4:], "signals": str(self.signals), "urgent": True, **extra}

    def test_a_dry_run_plans_without_prompts_or_orders(self):
        session = FakeSession()
        self.assertEqual(self.daemon.trade(self.request(), session), 0)
        self.assertEqual(session.asked, [])
        self.exchange.order.assert_not_called()
        self.assertIn("AVAX SHORT: quantity=", self.output.getvalue())

    def test_a_live_order_needs_both_typed_phrases(self):
        session = FakeSession("LIVE TRADING", "SUBMIT")
        self.assertEqual(self.daemon.trade(self.request(live=True), session), 0)
        self.assertEqual(len(session.asked), 2)
        self.exchange.order.assert_not_called()
        self.assertIn("Confirmation did not match", self.output.getvalue())

    def test_an_urgent_crypto_entry_places_the_entry_and_a_held_take_profit_no_stop_and_no_guard(self):
        session = FakeSession("LIVE TRADING", "SUBMIT ORDERS")
        self.assertEqual(self.daemon.trade(self.request(live=True), session), 0)
        entry, *targets = self.exchange.order.call_args_list   # the desk's rule: no stop or loss guard; crypto takes profit
        self.assertGreaterEqual(len(targets), 1)
        for target in targets:   # a ladder of take-profit rungs, each a reduce-only limit below the short's fill
            (_, target_is_buy, _, target_price, target_type), target_kwargs = target
            self.assertEqual((target_is_buy, target_kwargs), (True, {"reduce_only": True}))   # closes the short
            self.assertNotIn("trigger", target_type)                                           # a limit, never a stop
            self.assertLess(target_price, 7.98)                                                # never at a loss
        (coin, is_buy, size, limit, order_type), kwargs = entry
        self.assertEqual((coin, is_buy, order_type, kwargs), ("AVAX", False, {"limit": {"tif": "Ioc"}},
                                                              {"reduce_only": False}))
        self.assertAlmostEqual(limit, 7.96)
        self.assertNotIn((WALLET, "AVAX"), self.daemon.guards.entries)

    def test_a_dashboard_entry_is_planned_without_a_label_but_never_sent_without_one(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(hl_daemon.daemon_link, "RUN_DIR", Path(folder)):
            with patch.object(self.daemon, "trade", return_value=0) as trade:
                base = {"cmd": "resize", "change": "long", "coin": "xmr", "wallet": WALLET[-4:]}
                code = self.daemon.dispatch(dict(base, live=True), FakeSession())   # sending needs the label
                self.assertEqual(code, 2)
                trade.assert_not_called()
                self.assertIn("Pick a label", self.output.getvalue())
                self.assertEqual(self.daemon.dispatch(dict(base, live=False), FakeSession()), 0)   # a plan does not: the label never sets the size
                self.assertFalse(trade.call_args.args[0]["live"])
                self.assertEqual(trade.call_args.args[0]["notional"], None)
                trade.reset_mock()

    def test_a_dashboard_entry_is_risk_sized_not_conviction(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(hl_daemon.daemon_link, "RUN_DIR", Path(folder)):
            with patch.object(self.daemon, "trade", return_value=0) as trade:
                self.assertEqual(self.daemon.dispatch({"cmd": "resize", "change": "long", "coin": "xmr", "wallet": WALLET[-4:],
                                                       "label": "directional"}, FakeSession()), 0)
                request = trade.call_args.args[0]
                self.assertNotIn("conviction", request)
                self.assertEqual((request["strategy"], request["label"], request["notional"]), ("directional", "directional", None))
                self.daemon.dispatch({"cmd": "resize", "change": "short", "coin": "ena", "wallet": WALLET[-4:],
                                      "label": "conviction", "notional": "500"}, FakeSession())
                self.assertEqual((trade.call_args.args[0]["strategy"], trade.call_args.args[0]["notional"]), ("conviction", 500.0))
                self.assertFalse(trade.call_args.args[0]["use_flex"])   # the cap flex is never preset
                self.daemon.dispatch({"cmd": "resize", "change": "long", "coin": "xmr", "wallet": WALLET[-4:],
                                      "label": "directional", "use_flex": True}, FakeSession())
                self.assertTrue(trade.call_args.args[0]["use_flex"])    # only when chosen on the confirm step

    def test_a_planned_entry_shows_its_share_of_the_wallet_and_the_risk_cap_without_expected_pnl(self):
        self.daemon.trade(self.request(label="directional"), FakeSession())
        line = next(text for text in self.output.getvalue().splitlines() if text.startswith("AVAX SHORT"))
        self.assertIn("of the wallet", line)
        self.assertIn("of the risk cap (risk budget and volatility)", line)
        self.assertIn("no stop or loss guard, take-profit", line)   # the ladder (desk/tp_ladder.py) or the single target
        self.assertNotIn("expected 5d PnL", self.output.getvalue())

    def test_the_kill_switch_blocks_live_orders(self):
        session = FakeSession("LIVE TRADING", "SUBMIT ORDERS")
        with patch.object(ht, "kill_switch_engaged", return_value=True):
            self.assertEqual(self.daemon.trade(self.request(live=True), session), 2)
        self.assertEqual(session.asked, [])
        self.exchange.order.assert_not_called()

    def test_an_unreadable_desk_account_blocks_live_trading_unless_ignored(self):
        self.daemon.state.other = (risk.DeskBook(unreachable=["IB 8440: TWS down"]), time.time())
        with self.assertRaises(RuntimeError):
            self.daemon.trade(self.request(live=True), FakeSession("LIVE TRADING", "SUBMIT ORDERS"))
        session = FakeSession("LIVE TRADING", "SUBMIT ORDERS")
        self.assertEqual(self.daemon.trade(self.request(live=True, ignore_unreachable=True), session), 0)
        self.assertGreaterEqual(self.exchange.order.call_count, 2)   # the entry and its take-profit rungs: no stop

    def test_a_one_off_risk_cap_is_restored_afterwards(self):
        cap = ht.MAX_TOTAL_RISK_PCT
        self.daemon.trade(self.request(max_risk_pct=25), FakeSession())
        self.assertIn("aggregate risk cap for this run is 25%", self.output.getvalue())
        self.assertEqual(ht.MAX_TOTAL_RISK_PCT, cap)

    def test_wallets_resolve_from_their_last_characters(self):
        self.assertEqual(self.daemon.wallet("07F2"), WALLET)
        with self.assertRaises(ValueError):
            self.daemon.wallet("c248")


class ProtocolTests(unittest.TestCase):
    def test_the_client_prints_output_answers_prompts_and_returns_the_exit_code(self):
        class Stub:
            def dispatch(self, request, session):
                print(f"plan for {request['cmd']}")
                answer = session.ask("Type LIVE TRADING: ")
                print(f"got {answer}")
                session.data({"answer": answer})
                return 0

        folder = tempfile.mkdtemp()
        path = Path(folder) / "d.sock"
        captured = io.StringIO()
        router = hl_daemon._Router(captured)
        server = hl_daemon._Server(str(path), hl_daemon._Handler)
        server.daemon = Stub()
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        try:
            with patch.object(daemon_link, "SOCKET_PATH", path), patch.object(hl_daemon, "OUTPUT", router), \
                    patch("sys.stdout", router), patch("sys.stdin", io.StringIO("LIVE TRADING\n")):
                code = hl_daemon.call({"cmd": "trade"})
                data = daemon_link.query({"cmd": "trade"})
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 0)
        text = captured.getvalue()
        self.assertIn("plan for trade", text)
        self.assertIn("Type LIVE TRADING: ", text)
        self.assertIn("got LIVE TRADING", text)
        self.assertEqual(data, {"answer": ""})  # a data query answers prompts with nothing

    def test_no_daemon_means_no_answer(self):
        with patch.object(daemon_link, "SOCKET_PATH", Path(tempfile.gettempdir()) / "missing-daemon.sock"):
            self.assertIsNone(daemon_link.hl_book([WALLET]))

    def test_the_client_default_slippage_matches_the_executor(self):
        self.assertAlmostEqual(hl_daemon.URGENT_SLIPPAGE_PCT, ht.URGENT_SLIPPAGE * 100)


class RiskLinkTests(unittest.TestCase):
    def test_hl_risk_comes_from_the_daemon_when_it_answers(self):
        live = {"equity": 2100.0, "risk": 400.0, "margin_used": 900.0, "positions": 5, "age": 3.0}
        with patch.object(daemon_link, "hl_book", return_value=live), \
                patch.object(risk, "_hl_executor", side_effect=AssertionError("no HL read expected")):
            entry = risk.hl_book_entry([WALLET], "HL 07f2")
        self.assertEqual((entry.label, entry.equity_usd, entry.risk_usd, entry.positions), ("HL 07f2", 2100.0, 400.0, 5))


class ExecutorFastPathTests(unittest.TestCase):
    def setUp(self):
        self.market = ht.Market("AVAX", "", 8.0, 8.0, 2, 10)
        self.plan = active_trade(quantity=30).plan
        patcher = patch.object(ht, "kill_switch_engaged", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_gather_keeps_order_and_raises_the_first_failure(self):
        self.assertEqual(ht._gather([lambda: 1, lambda: 2, lambda: 3]), [1, 2, 3])
        with self.assertRaisesRegex(ValueError, "first"):
            ht._gather([lambda: 1, lambda: (_ for _ in ()).throw(ValueError("first")),
                        lambda: (_ for _ in ()).throw(KeyError("second"))])

    def test_an_urgent_entry_returns_what_filled(self):
        exchange = MagicMock()
        exchange.order.return_value = filled(12.5, 7.99)
        self.assertEqual(ht._place_taker_entry(exchange, WALLET, self.plan, self.market, reference=8.0, baseline=0.0),
                         (12.5, 7.99))

    def test_an_urgent_entry_with_nothing_inside_the_cap_leaves_nothing_resting(self):
        exchange = MagicMock()
        exchange.order.return_value = statuses({"error": "Order could not immediately match"})
        with self.assertRaisesRegex(RuntimeError, "no fill within 0.5%"):
            ht._place_taker_entry(exchange, WALLET, self.plan, self.market, reference=8.0, baseline=0.0)

    def test_a_failed_send_is_settled_from_the_position(self):
        exchange = MagicMock()
        exchange.order.side_effect = TimeoutError("no answer")
        with patch.object(ht, "_current_position_details", return_value=(-30.0, 7.97)):
            self.assertEqual(ht._place_taker_entry(exchange, WALLET, self.plan, self.market, reference=8.0,
                                                   baseline=0.0), (30.0, 7.97))
        with patch.object(ht, "_current_position_details", return_value=(0.0, 0.0)), \
                self.assertRaisesRegex(RuntimeError, "shows no fill"):
            ht._place_taker_entry(exchange, WALLET, self.plan, self.market, reference=8.0, baseline=0.0)

    def test_entry_target_is_post_only_and_reduce_only(self):
        exchange = MagicMock()
        exchange.order.return_value = statuses({"resting": {"oid": 3}})
        self.assertEqual(ht._place_entry_target(exchange, self.plan, 30, 8.0, 2), 3)
        self.assertEqual(exchange.order.call_args.args[4], {"limit": {"tif": "Alo"}})
        self.assertTrue(exchange.order.call_args.kwargs["reduce_only"])
        exchange.order.assert_called_once()

    def test_a_target_that_would_cross_is_placed_again_outside_the_spread(self):
        exchange = MagicMock()
        exchange.order.side_effect = [statuses({"error": "Post only would match"}), statuses({"resting": {"oid": 4}})]
        with patch.object(ht, "post_only_price", return_value=7.5):
            self.assertEqual(ht._place_entry_target(exchange, self.plan, 30, 8.0, 2), 4)

    def test_a_fast_chase_reprices_every_two_seconds(self):
        exchange = MagicMock()
        exchange.order.return_value = statuses({"resting": {"oid": 5}})
        with patch.object(ht, "post_only_price", return_value=8.0), \
                patch.object(ht, "_current_position_quantity", return_value=0.0), \
                patch.object(ht, "_rest_entry", return_value=("filled", 30.0)) as rest:
            self.assertEqual(ht._place_entry(exchange, WALLET, self.plan, self.market, fast=True), (30.0, 8.0))
        self.assertEqual(rest.call_args.args[-1], ht.FAST_CHASE_SECONDS)

    def test_a_fast_chase_stops_when_the_price_runs_away(self):
        exchange = MagicMock()
        # A short planned at $8.00 with a $1.00 stop distance: chasing below $7.60 stops.
        with patch.object(ht, "post_only_price", return_value=7.55), \
                patch.object(ht, "_current_position_quantity", return_value=0.0), \
                self.assertRaisesRegex(RuntimeError, "No entry fill"):
            ht._place_entry(exchange, WALLET, self.plan, self.market, fast=True)
        exchange.order.assert_not_called()

    def test_a_snapshot_from_supplied_data_makes_no_requests(self):
        markets = {"AVAX": self.market}
        with patch.object(ht, "fetch_user_state", side_effect=AssertionError("no read")), \
                patch.object(ht, "parse_fee_rates", side_effect=AssertionError("no read")):
            snapshot = ht.collect_snapshot([WALLET], [""], markets, states={(WALLET, ""): account_state()},
                                           collateral=lambda wallet, perp: perp, fee_rates=(0.0, 0.0))
        self.assertEqual(snapshot.equity, 1000.0)


class IBQuoteFallbackTests(unittest.TestCase):
    def setUp(self):
        self.contract = SimpleNamespace(secType="STK", currency="USD", symbol="NET")
        patcher = patch.object(ib_trade, "quote_source", "auto")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_real_time_ib_quote_is_used(self):
        quote = ib_venue.Quote(99.9, 100.1, 100.0, 98.0, True)
        with patch.object(ib_venue, "quote", return_value=quote), \
                patch.object(ib_trade, "yahoo_quote", side_effect=AssertionError("not needed")):
            self.assertIs(ib_trade.get_quote(None, self.contract), quote)

    def test_no_ib_quote_falls_back_to_a_current_yahoo_price(self):
        yahoo = ib_venue.Quote(99.7, 100.3, 100.0, 98.0, True, source="yahoo")
        with patch.object(ib_venue, "quote", side_effect=ValueError("IB sent no quote")) as ib_quote, \
                patch.object(ib_trade, "yahoo_quote", return_value=yahoo):
            self.assertIs(ib_trade.get_quote(None, self.contract), yahoo)
        self.assertEqual(ib_quote.call_args.kwargs, {"timeout": ib_trade.IB_QUOTE_WAIT_SECONDS})

    def test_a_delayed_ib_quote_is_kept_when_yahoo_is_not_current_either(self):
        delayed = ib_venue.Quote(99.9, 100.1, 100.0, 98.0, False)
        stale = ib_venue.Quote(99.7, 100.3, 100.0, 98.0, False, source="yahoo")
        with patch.object(ib_venue, "quote", return_value=delayed), patch.object(ib_trade, "yahoo_quote", return_value=stale):
            self.assertIs(ib_trade.get_quote(None, self.contract), delayed)

    def test_other_instruments_use_ib_only(self):
        future = SimpleNamespace(secType="FUT", currency="USD", symbol="ES")
        quote = ib_venue.Quote(5000, 5000.25, 5000, 4990, False)
        with patch.object(ib_venue, "quote", return_value=quote) as ib_quote, \
                patch.object(ib_trade, "yahoo_quote", side_effect=AssertionError("not for futures")):
            self.assertIs(ib_trade.get_quote(None, future), quote)
        self.assertEqual(ib_quote.call_args.kwargs, {})


if __name__ == "__main__":
    unittest.main()


class MarginPlanTests(unittest.TestCase):
    def position(self, coin, quantity, entry, mark, margin, decimals=1):
        return SimpleNamespace(coin=coin, dex="", quantity=quantity, entry_price=entry, mark_price=mark,
                               margin_used=margin, market=SimpleNamespace(size_decimals=decimals))

    def config(self, **changes):
        return {**hl_daemon.MARGIN_DEFAULTS, **changes}

    def test_a_busy_account_far_from_liquidation_is_left_alone(self):
        # 95% of equity in initial margin is past the reserve, but the account is only 38% of the way to liquidation.
        plan = hl_daemon.margin_plan([self.position("ICP", -100, 2.5, 2.6, 950)], 1000.0, 950.0, "0xabc07f2",
                                     self.config(), maintenance=380.0)
        self.assertAlmostEqual(plan["health"], 0.38)
        self.assertIsNone(plan["action"])

    def test_near_a_cascade_the_most_in_the_money_position_is_cut_first(self):
        positions = [self.position("ICP", -100, 2.5, 2.4, 300),     # short, price fell: +10
                     self.position("BERA", -1000, 0.10, 0.12, 300),  # short, price rose: -20
                     self.position("ETH", 1.0, 2000, 2100, 400)]     # long, +100: the most in the money
        plan = hl_daemon.margin_plan(positions, 1000.0, 1000.0, "0xabc07f2", self.config(), maintenance=700.0)
        action = plan["action"]
        self.assertEqual(action["coin"], "ETH")
        # ETH carries 40% of the 700 maintenance (280); getting to 45% needs 250 of it: 89%, capped at half.
        self.assertEqual(action["fraction"], 0.5)
        self.assertEqual(action["quantity"], 0.5)

    def test_protected_positions_go_last(self):
        positions = [self.position("VVV", -122.46, 19.56, 15.0, 600, 2), self.position("ICP", -100, 2.5, 2.6, 300)]
        plan = hl_daemon.margin_plan(positions, 1000.0, 900.0, "0xabc07f2", self.config(), maintenance=650.0)
        self.assertEqual(plan["action"]["coin"], "ICP")  # VVV is in profit but protected (the hedge)
        self.assertFalse(plan["last_resort"])
        only = hl_daemon.margin_plan(positions[:1], 1000.0, 600.0, "0xabc07f2", self.config(), maintenance=650.0)
        self.assertEqual(only["action"]["coin"], "VVV")  # nothing else left: a cascade would close it anyway
        self.assertTrue(only["last_resort"])

    def test_a_small_overshoot_takes_the_chop_fraction(self):
        plan = hl_daemon.margin_plan([self.position("ICP", -100, 2.5, 2.4, 1000)], 1000.0, 1000.0, "0xabc07f2",
                                     self.config(), maintenance=610.0)
        self.assertAlmostEqual(plan["action"]["fraction"], 160 / 610)  # to 45%: 160 of the 610


class RegimeTargetTests(unittest.TestCase):
    position = MarginPlanTests.position

    def test_each_regime_hits_its_own_side_and_spares_the_hedge(self):
        positions = [self.position("ICP", -100, 2.5, 2.6, 300), self.position("BTC", -0.01, 80000, 79000, 200),
                     self.position("VVV", -122, 19.5, 19.0, 800, 2), self.position("xyz:NVDA", 2, 180, 190, 100),
                     self.position("xyz:QNT", -3, 41, 40, 100), self.position("SOL", 5, 150, 160, 200)]
        alts = hl_daemon.regime_targets(positions, "0xabc07f2", "alt_season", ["07f2:VVV"])
        self.assertEqual([p.coin for p in alts], ["ICP"])  # not BTC, not the VVV hedge, not longs or equity perps
        equities = hl_daemon.regime_targets(positions, "0xabc07f2", "equity_selloff", ["07f2:VVV"])
        self.assertEqual([p.coin for p in equities], ["xyz:NVDA"])  # equity-perp longs only


class LadderQuoteTests(unittest.TestCase):
    config = {**hl_daemon.LADDER_DEFAULTS}

    def quotes(self, position, base=100.0, allow_adds=True, mid=10.0, atr=1.0):
        return hl_daemon.ladder_quotes(position, base, mid, mid - 0.01, mid + 0.01, atr, 1, self.config, allow_adds)

    def test_a_long_leans_bids_closer_and_bigger(self):
        quotes = self.quotes(100.0)
        bids = [q for q in quotes if q["is_buy"]]
        asks = [q for q in quotes if not q["is_buy"]]
        self.assertEqual((len(bids), len(asks)), (5, 5))
        self.assertAlmostEqual(bids[0]["price"], 9.95)   # half a spacing (0.1 x ATR) under the mid
        self.assertAlmostEqual(asks[0]["price"], 10.1)   # a full spacing over it
        self.assertEqual(bids[0]["size"], 5.0)           # 5% of the size opened
        self.assertEqual(asks[0]["size"], 3.0)           # 3%
        self.assertTrue(all(q["reduce_only"] for q in asks) and not any(q["reduce_only"] for q in bids))

    def test_a_short_mirrors_it(self):
        quotes = self.quotes(-100.0, base=-100.0)
        asks = [q for q in quotes if not q["is_buy"]]
        self.assertEqual(asks[0]["kind"], "add")
        self.assertAlmostEqual(asks[0]["price"], 10.05)

    def test_the_band_stops_adds_and_trims(self):
        full = self.quotes(148.0)                        # 2 left before 150% of the base
        self.assertEqual(sum(q["size"] for q in full if q["kind"] == "add"), 2.0)
        thin = self.quotes(52.0)                         # 2 left before 50%
        self.assertEqual(sum(q["size"] for q in thin if q["kind"] == "trim"), 2.0)

    def test_adds_pause_when_margin_is_stretched_and_quotes_never_cross(self):
        self.assertFalse(any(q["kind"] == "add" for q in self.quotes(100.0, allow_adds=False)))
        tight = hl_daemon.ladder_quotes(100.0, 100.0, 10.0, 9.99, 10.01, 0.0001, 1, self.config)
        self.assertTrue(all(q["price"] < 10.01 for q in tight if q["is_buy"]))
        self.assertTrue(all(q["price"] > 9.99 for q in tight if not q["is_buy"]))

    def test_levels_under_ten_dollars_are_raised_to_the_minimum(self):
        # 2026-11-14 the ETH, MELANIA and S&P ladders sat idle: 2-5% of a small position was under HL's $10 minimum.
        quotes = hl_daemon.ladder_quotes(10.0, 10.0, 10.0, 9.99, 10.01, 1.0, 1, self.config)
        self.assertTrue(quotes)
        self.assertTrue(all(q["size"] * q["price"] >= hl_daemon.MIN_ORDER_USD for q in quotes))

    def test_no_quote_when_the_band_leaves_less_than_the_minimum(self):
        quotes = hl_daemon.ladder_quotes(14.5, 10.0, 10.0, 9.99, 10.01, 1.0, 1, self.config)   # $5 of room to add
        self.assertFalse(any(q["kind"] == "add" for q in quotes))

    def test_trims_can_be_switched_off_leaving_the_adds(self):
        quotes = hl_daemon.ladder_quotes(-100.0, -100.0, 10.0, 9.99, 10.01, 1.0, 1, self.config, True, False)
        self.assertTrue(quotes)
        self.assertTrue(all(q["kind"] == "add" and not q["is_buy"] for q in quotes))


class LadderVerdictTests(unittest.TestCase):
    closes = [100, 102, 99, 101, 100, 102]   # a range

    def test_a_trend_regime_defers_the_verdict(self):
        self.closes = [100, 96, 92, 89, 85, 80]   # five red days
        self.assertFalse(self.run_verdict(self.fills(buy=101, sell=100))[0])

    def run_verdict(self, fills):
        from types import SimpleNamespace
        from unittest.mock import patch
        from desk import market_making

        hl_daemon.load_trading()
        alerts = []
        daemon = SimpleNamespace(alerts=SimpleNamespace(set=lambda *a, **k: alerts.append(a[1]), clear=lambda *a: None))
        ladders = hl_daemon.Ladders(daemon, path=None)
        ladder = {"wallet": "0xabc", "coin": "BTC", "dex": "", "base": -1.0, "started": "2026-11-14 10:00"}
        ladders.ladders[("0xabc", "BTC")] = ladder
        daily = pd.DataFrame({"close": self.closes})
        with patch.object(market_making, "ladder_fill_rows", return_value=fills), patch.object(ladders, "_cancel_own"), \
                patch.object(hl_daemon.ht, "fetch_daily_candles", return_value=daily):
            stopped = ladders._verdict(ladder, 10_000.0, {"verdict_fills": 4})
        return stopped, ladders.ladders, alerts

    def fills(self, buy, sell, count=4):
        return [{"coin": "BTC", "side": "B" if i % 2 == 0 else "A", "px": str(buy if i % 2 == 0 else sell), "sz": "1",
                 "fee": "0.01", "time": i} for i in range(count)]

    def test_a_ladder_whose_round_trips_lose_stops(self):
        stopped, running, alerts = self.run_verdict(self.fills(buy=101, sell=100))
        self.assertTrue(stopped)
        self.assertEqual(running, {})
        self.assertIn("round trips lost", alerts[0])

    def test_a_winning_ladder_or_one_short_of_the_fill_count_keeps_quoting(self):
        self.assertFalse(self.run_verdict(self.fills(buy=100, sell=101))[0])
        self.assertFalse(self.run_verdict(self.fills(buy=101, sell=100, count=3))[0])


class TrendPauseTests(unittest.TestCase):
    config = {**hl_daemon.LADDER_DEFAULTS}

    def test_efficiency_ratio(self):
        self.assertEqual(hl_daemon.efficiency_ratio([10, 9, 8, 7]), (1.0, -1.0))          # a straight fall
        self.assertEqual(hl_daemon.efficiency_ratio([10, 11, 10, 11, 10]), (0.0, 0.0))     # churn
        self.assertAlmostEqual(hl_daemon.efficiency_ratio([10, 12, 11, 13])[0], 3 / 5)
        self.assertEqual(hl_daemon.efficiency_ratio([10]), (0.0, 0.0))

    def test_trims_pause_only_when_the_trend_runs_the_positions_way(self):
        falling, rising = (0.6, -1.0), (0.6, 1.0)
        self.assertTrue(hl_daemon.trims_paused(*falling, -100.0, self.config))    # a short in a fall: keep it
        self.assertFalse(hl_daemon.trims_paused(*rising, -100.0, self.config))    # against the short: trims stay
        self.assertTrue(hl_daemon.trims_paused(*rising, 100.0, self.config))      # a long in a rise
        self.assertFalse(hl_daemon.trims_paused(0.2, -1.0, -100.0, self.config))  # choppy: the ladder trades both ways
        self.assertFalse(hl_daemon.trims_paused(*falling, -100.0, {**self.config, "trend_pause_er": 0}))  # switched off


class LadderArmingTests(unittest.TestCase):
    # These test the arming logic itself, at fixed distances (place within 1.25 spacings, pull beyond 2).
    config = {**hl_daemon.LADDER_DEFAULTS, "place_within": 1.25, "cancel_beyond": 2.0}

    def grid(self):
        # Sells above and buys below a mid of 100, one step (1.0) apart.
        return [{"price": 100 + step, "is_buy": False, "oid": None} for step in range(1, 6)] + \
               [{"price": 100 - step, "is_buy": True, "oid": None} for step in range(1, 6)]

    def test_only_the_nearest_quote_per_side_goes_on_the_book(self):
        to_place, to_cancel = hl_daemon.arm_plan(self.grid(), 100.0, 1.0, self.config, now=0.0)
        self.assertEqual(sorted(quote["price"] for quote in to_place), [99, 101])     # one a side, within 1.25 steps
        self.assertEqual(to_cancel, [])

    def test_nothing_is_shown_until_the_price_comes_near(self):
        to_place, _ = hl_daemon.arm_plan(self.grid(), 100.0, 0.7, self.config, now=0.0)   # quotes 1.4+ steps away
        self.assertEqual(to_place, [])

    def test_a_quote_is_pulled_once_the_price_leaves_it_behind_and_the_next_one_shows(self):
        grid = self.grid()
        for quote in grid:
            if quote["price"] in (99, 101):
                quote["oid"] = int(quote["price"])
        to_place, to_cancel = hl_daemon.arm_plan(grid, 101.6, 1.0, self.config, now=0.0)
        self.assertEqual(sorted(quote["price"] for quote in to_cancel), [99, 101])  # 99 is 2.6 steps away; 102 is nearer than 101
        self.assertEqual(sorted(quote["price"] for quote in to_place), [102])       # no buy is near enough to show

    def test_a_nearer_quote_replaces_the_one_on_the_book(self):
        grid = self.grid()
        grid[1]["oid"] = 7                                    # the sell at 102 rests
        to_place, to_cancel = hl_daemon.arm_plan(grid, 100.2, 1.0, self.config, now=0.0)
        self.assertEqual([quote["price"] for quote in to_place if not quote["is_buy"]], [101])
        self.assertIn(grid[1], to_cancel)

    def test_a_lowered_limit_pulls_the_extras(self):
        grid = self.grid()
        grid[0]["oid"], grid[1]["oid"] = 1, 2                 # two sells rest (an older, higher limit)
        _, to_cancel = hl_daemon.arm_plan(grid, 100.0, 1.0, self.config, now=0.0)
        self.assertEqual([quote["price"] for quote in to_cancel], [102])

    def test_a_refused_quote_waits_before_trying_again(self):
        grid = self.grid()
        grid[0]["retry_after"] = 50.0
        to_place, _ = hl_daemon.arm_plan(grid, 100.0, 1.0, self.config, now=10.0)
        self.assertNotIn(grid[0], to_place)
        to_place, _ = hl_daemon.arm_plan(grid, 100.0, 1.0, self.config, now=60.0)
        self.assertIn(grid[0], to_place)


class HedgeRiskTests(unittest.TestCase):
    def test_a_hedge_counts_nothing_toward_the_risk_cap(self):
        from desk.risk import is_hedged

        self.assertTrue(is_hedged("0x" + "a" * 36 + "07f2", "VVV"))
        self.assertFalse(is_hedged("0x" + "a" * 36 + "c248", "VVV"))   # another wallet's VVV is a normal position
        self.assertFalse(is_hedged("0x" + "a" * 36 + "07f2", "ICP"))


class LadderCoinConfigTests(unittest.TestCase):
    def test_a_coin_entry_overrides_only_that_coins_ladder(self):
        import hl_daemon as daemon

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ladder.json"
            path.write_text(json.dumps({"spacing_atr": 0.05, "coins": {"ETH": {"spacing_atr": 0.10}, "LIT": {"spacing_atr": 0.12}}}))
            with patch.object(daemon, "LADDER_CONFIG", path):
                self.assertEqual(daemon.ladder_config("ETH")["spacing_atr"], 0.10)
                self.assertEqual(daemon.ladder_config("xyz:eth")["spacing_atr"], 0.10)   # the coin name alone, any case
                self.assertEqual(daemon.ladder_config("MELANIA")["spacing_atr"], 0.05)
                self.assertEqual(daemon.ladder_config()["spacing_atr"], 0.05)
                self.assertNotIn("coins", daemon.ladder_config("ETH"))

    def test_the_nearest_quote_stays_in_memory_until_the_price_is_half_a_spacing_away(self):
        config = {**hl_daemon.LADDER_DEFAULTS}
        grid = [{"price": 101.0, "is_buy": False, "oid": None}, {"price": 99.0, "is_buy": True, "oid": None}]   # 1 spacing out
        to_place, _ = hl_daemon.arm_plan(grid, 100.0, 1.0, config, now=0.0)
        self.assertEqual(to_place, [])                                                       # nothing rests at the centre
        to_place, _ = hl_daemon.arm_plan(grid, 100.6, 1.0, config, now=0.0)                  # the price rose to 0.4 from the sell
        self.assertEqual([q["price"] for q in to_place], [101.0])


class ApiKeyAlertTests(unittest.TestCase):
    """The dashboard warns before an HL API wallet expires, and if the daemon holds the main wallet's own key."""

    SIGNER = "0x" + "c" * 36 + "ab59"

    def check(self, signer: str, agents: list[dict]) -> dict:
        alerts = hl_daemon.Alerts(path=None)
        daemon = SimpleNamespace(paper=False, exchanges={WALLET: SimpleNamespace(wallet=SimpleNamespace(address=signer))})
        with patch.object(hl_daemon, "post_info", return_value=agents):
            alerts.watch_keys(daemon)
        return alerts.items

    def test_an_api_wallet_expiring_within_30_days_raises_an_alert(self):
        soon = (time.time() + 10 * 86400) * 1000
        items = self.check(self.SIGNER, [{"address": self.SIGNER, "name": "trading-api", "validUntil": soon}])
        self.assertEqual(items[f"apikey:{WALLET}"]["title"], "API wallet expiring")
        self.assertIn("--store-key", items[f"apikey:{WALLET}"]["text"])

    def test_a_valid_api_wallet_raises_nothing(self):
        later = (time.time() + 120 * 86400) * 1000
        self.assertEqual(self.check(self.SIGNER, [{"address": self.SIGNER, "validUntil": later}]), {})

    def test_the_main_wallets_own_key_raises_an_alert(self):
        items = self.check(WALLET, [])
        self.assertEqual(items[f"apikey:{WALLET}"]["title"], "main wallet key on the server")

    def test_an_unlisted_api_wallet_raises_an_alert(self):
        items = self.check(self.SIGNER, [])
        self.assertEqual(items[f"apikey:{WALLET}"]["title"], "API wallet not approved")


class FundingAlertTests(unittest.TestCase):
    """The daemon flags a position whose funding has run against it for six hourly payments in a row."""

    def check(self, size: float, rates: list[float], value: float = 1500.0, alerts=None):
        alerts = alerts or hl_daemon.Alerts(path=None)
        alerts.funding_checked_at = 0.0
        position = {"coin": "MELANIA", "szi": str(size), "positionValue": str(value)}
        state = SimpleNamespace(lock=threading.RLock(), states={WALLET: {"": {"assetPositions": [{"position": position}] if size else []}}})
        history = pd.DataFrame({"fundingRate": rates})
        with patch.object(ht, "fetch_funding_history", return_value=history):
            alerts.watch_funding(SimpleNamespace(state=state))
        return alerts

    def test_a_short_paying_for_six_hours_raises_an_alert(self):
        alerts = self.check(-1000, [-0.0001] * 8)  # longs receive: the short pays 87.6% APR
        item = alerts.items[f"funding:{WALLET}:MELANIA"]
        self.assertEqual(item["title"], "funding")
        self.assertEqual(item["text"], f"...{WALLET[-4:]} MELANIA short -88% avg APR over 6 hrs")

    def test_one_earning_hour_in_the_six_clears_it(self):
        alerts = self.check(-1000, [-0.0001] * 5 + [0.00001] + [-0.0001] * 2)
        self.assertEqual(alerts.items, {})

    def test_a_position_earning_funding_raises_nothing(self):
        self.assertEqual(self.check(1000, [-0.0001] * 8).items, {})

    def test_a_closed_position_drops_its_alert(self):
        alerts = self.check(-1000, [-0.0001] * 8)
        self.check(0, [-0.0001] * 8, alerts=alerts)
        self.assertEqual(alerts.items, {})


class AltRallyAlertTests(unittest.TestCase):
    """The daemon raises the alt rally watch from HL's 24-hour moves and remembers when it fired."""

    def test_a_five_percent_basket_move_raises_the_watch_with_the_book_hit(self):
        import tempfile

        alerts = hl_daemon.Alerts(path=None)
        meta = {"universe": [{"name": "SOL"}, {"name": "DOGE"}]}
        contexts = [{"markPx": "106", "prevDayPx": "100", "dayNtlVlm": "1e8"}, {"markPx": "104", "prevDayPx": "100", "dayNtlVlm": "1e8"}]
        position = {"coin": "SOL", "szi": "-10"}
        state = SimpleNamespace(lock=threading.RLock(), states={WALLET: {"": {
            "assetPositions": [{"position": position}], "marginSummary": {"accountValue": "1000"}}}})
        from desk import market_context

        hack = {"name": "BigVenue", "amount": 300_000_000, "chain": ["Ethereum"], "classification": "CeFi", "date": time.time() - 7200}
        news = [{"time": time.time() - 3600, "source": "The Block", "title": "Alts jump on ETF approval", "link": ""}]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(hl_daemon, "ALT_RALLY_FILE", Path(folder) / "watches.json"), \
                patch.object(hl_daemon, "STOCK_DIP_FILE", Path(folder) / "dips.json"), \
                patch.object(hl_daemon, "post_info", return_value=(meta, contexts)), \
                patch.object(market_context, "fear_greed", return_value=(20, "Extreme Fear")), \
                patch.object(market_context, "recent_hacks", return_value=[hack]), \
                patch.object(market_context, "headlines", return_value=news), \
                patch.object(__import__("desk.event_watch").event_watch, "EVENTS_FILE", Path(folder) / "events.jsonl"), \
                patch.object(hl_daemon.Alerts, "watch_events", lambda *args: None):
            alerts.watch_alt_rally(SimpleNamespace(state=state))
            saved = (Path(folder) / "watches.json").read_text()
            logged = (Path(folder) / "events.jsonl").read_text().splitlines()
        item = alerts.items["alt_rally"]
        self.assertEqual(item["title"], "Alt rally")
        self.assertIn("alts 5% in 24h (2 of 2 up); the book -6% on it", item["text"])   # -10 SOL x $6 on $1,000
        self.assertIn("Context: mood extreme fear 20; shorts pay on 0 of 2 alts; hack: BigVenue $300M.", item["text"])
        self.assertIn("The Block · Alts jump on ETF approval", item["context"])      # the headlines, on hover
        self.assertEqual(len(__import__("json").loads(saved)), 1)
        self.assertEqual(len(logged), 1)                                              # the event, for later study
        hack_item = next(value for key, value in alerts.items.items() if key.startswith("hack:"))
        self.assertEqual(hack_item["title"], "Major hack")
        self.assertTrue(hack_item["text"].startswith("BigVenue $300M (Ethereum; CeFi), 2 h ago"))


class EventWatchTests(unittest.TestCase):
    """The hourly event check: a VVV burn on the chain raises "Burn" and logs it for its debrief."""

    def test_a_burn_day_raises_the_alert_and_logs_the_event(self):
        import tempfile

        from desk import event_watch

        alerts = hl_daemon.Alerts(path=None)
        meta = {"universe": [{"name": "BTC"}, {"name": "VVV"}]}
        contexts = [{"markPx": "100000", "dayNtlVlm": "9e9"}, {"markPx": "21", "dayNtlVlm": "5e7"}]
        today = time.strftime("%Y-%m-%d", time.gmtime())
        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        with tempfile.TemporaryDirectory() as folder:
            supply, events = Path(folder) / "supply.json", Path(folder) / "events.jsonl"
            supply.write_text(json.dumps({"VVV burned": {yesterday: 33_000_000.0}}))
            with patch.object(event_watch, "SUPPLY_FILE", supply), patch.object(event_watch, "EVENTS_FILE", events), \
                    patch.object(event_watch, "vvv_burned", return_value=33_021_657.0), \
                    patch.object(event_watch, "hype_circulating", return_value=float("nan")):
                alerts.watch_events(meta, contexts, {"universe": []}, [])
                logged = event_watch.load_events()
        burn = alerts.items[f"burn:VVV:{today}"]
        self.assertEqual(burn["title"], "Burn")
        self.assertTrue(burn["text"].startswith("VVV $455K bought and burned today (21,657 VVV"))
        self.assertEqual([(event["kind"], event["coin"]) for event in logged], [("burn", "VVV")])


class MakerReductionTests(unittest.TestCase):
    """Position reductions rest as post-only makers first and close at market only what is left."""

    def run_reduction(self, positions: list[float], order_status: dict, state: str = "open", timeout: float = 0.05):
        exchange = MagicMock()
        exchange.order.return_value = {"status": "ok", "response": {"data": {"statuses": [order_status]}}}
        exchange.market_close.return_value = {"status": "ok"}
        reads = iter(positions)
        last = [positions[0]]

        def position(*_):
            last[0] = next(reads, last[0])
            return last[0]

        with patch.object(ht, "_current_position_quantity", side_effect=position), \
                patch.object(ht, "post_only_price", return_value=100.0), \
                patch.object(ht, "_read_fill", return_value=(state, 0.0)), \
                patch.object(ht.time, "sleep"):
            result = ht._place_reduction(exchange, WALLET, "TRX", "", "SHORT", 10.0, SimpleNamespace(size_decimals=2),
                                         timeout=timeout, interval=0.0)
        return exchange, result

    def test_a_maker_fill_closes_without_a_market_order(self):
        exchange, (maker, taker) = self.run_reduction([-10.0, 0.0], {"filled": {"totalSz": "10", "avgPx": "100"}})
        args, kwargs = exchange.order.call_args
        self.assertEqual(args[:4], ("TRX", True, 10.0, 100.0))           # buys back the short
        self.assertEqual(args[4], {"limit": {"tif": "Alo"}})
        self.assertTrue(kwargs["reduce_only"])
        exchange.market_close.assert_not_called()
        self.assertEqual((maker, taker), (10.0, 0.0))

    def test_whatever_the_maker_order_leaves_closes_at_market(self):
        exchange, (maker, taker) = self.run_reduction([-10.0, -4.0], {"resting": {"oid": 7}}, timeout=0.0)
        exchange.market_close.assert_called_once_with("TRX", 4.0)
        self.assertEqual((maker, taker), (6.0, 4.0))

    def test_a_partial_reduction_stops_at_its_target(self):
        exchange = MagicMock()
        exchange.order.return_value = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
        exchange.market_close.return_value = {"status": "ok"}
        with patch.object(ht, "_current_position_quantity", side_effect=[-10.0, -7.0, -7.0]), \
                patch.object(ht, "post_only_price", return_value=100.0), \
                patch.object(ht, "_read_fill", return_value=("canceled", 0.0)), \
                patch.object(ht.time, "sleep"):
            maker, taker = ht._place_reduction(exchange, WALLET, "TRX", "", "SHORT", 4.0, SimpleNamespace(size_decimals=2),
                                               timeout=0.0, interval=0.0)
        exchange.market_close.assert_called_once_with("TRX", 1.0)         # 10 -> 6, of which 3 filled as maker
        self.assertEqual((maker, taker), (3.0, 1.0))


class LadderStopTests(unittest.TestCase):
    def test_closing_the_position_stops_the_ladder_without_an_alert(self):
        daemon = SimpleNamespace(alerts=hl_daemon.Alerts(path=None),
                                 state=SimpleNamespace(position_quantity=lambda wallet, coin, dex: 0.0))
        ladders = hl_daemon.Ladders(daemon, path=None)
        ladders._cancel_own = lambda wallet, coin, dex: None
        ladder = {"wallet": "0xabc0000000000000000000000000000000c248", "coin": "LIT", "dex": "", "base": -249.0}
        ladders.ladders[(ladder["wallet"], "LIT")] = ladder
        ladders.step(ladder, time.time())
        self.assertEqual(ladders.ladders, {})
        self.assertEqual(daemon.alerts.items, {})   # a deliberate close is not something that needs attention


class PositionLadderTakeProfitOnlyTests(unittest.TestCase):
    def test_a_position_ladder_quotes_only_its_take_profit_side(self):
        from unittest.mock import MagicMock

        wallet = "0xabc0000000000000000000000000000000c248"
        exchange = MagicMock()
        daemon = SimpleNamespace(alerts=hl_daemon.Alerts(path=None), exchanges={wallet: exchange},
                                 margin_guard=SimpleNamespace(latest_health={wallet: 0.0}),
                                 state=SimpleNamespace(markets=lambda pairs: {"LIT": SimpleNamespace(size_decimals=1)},
                                                       position=lambda w, c, d: {"entryPx": "9.0"}))
        ladders = hl_daemon.Ladders(daemon, path=None)
        ladders._book = lambda coin, dex: (9.99, 10.01)
        ladder = {"wallet": wallet, "coin": "LIT", "dex": "", "base": 100.0, "grid": [], "oids": [], "fills": 0}
        config = dict(hl_daemon.LADDER_DEFAULTS)
        ladders._rebuild(ladder, 100.0, time.time(), config, 1.0, False)   # a long in profit (entry 9, mid 10)
        self.assertTrue(ladder["grid"])
        self.assertTrue(all(not quote["is_buy"] and quote["reduce_only"] for quote in ladder["grid"]))   # sells only: no adds
        self.assertTrue(all(quote["price"] > 9.0 for quote in ladder["grid"]))                           # and never at a loss


class SlowPushTests(unittest.TestCase):
    def test_the_regime_reaches_the_relay_when_ib_comes_from_the_relay(self):
        from desk import web_sync

        state = SimpleNamespace(slow_sent_at=0.0)
        book = SimpleNamespace(ib_reads=[("entry", "snapshot")], ib_from_relay=True)   # the server
        with patch.object(web_sync, "push_regime") as regime, patch.object(web_sync, "push_wallets") as wallets, \
                patch.object(web_sync, "push_ib") as ib:
            hl_daemon.HLState.send_ib(SimpleNamespace(send_slow=lambda: hl_daemon.HLState.send_slow(state)), book)
            hl_daemon.HLState.send_ib(SimpleNamespace(send_slow=lambda: hl_daemon.HLState.send_slow(state)), book)
        regime.assert_called_once()          # hourly, not on every book read
        wallets.assert_called_once()
        ib.assert_not_called()               # IB is not sent back to where it came from


class LadderTrendRequestTests(unittest.TestCase):
    def test_a_hip3_ladder_asks_for_candles_by_its_full_market_name(self):
        ladders = hl_daemon.Ladders(SimpleNamespace(), path=None)
        asked = []
        ladder = {"coin": "xyz:SP500", "dex": "xyz"}
        with patch.object(hl_daemon.ht, "post_info", side_effect=lambda request: asked.append(request) or
                          [{"t": i, "c": str(100 + i)} for i in range(15)]):
            ladders._trend(ladder, now=1_000_000.0, config=hl_daemon.ladder_config())
        self.assertEqual(asked[0]["req"]["coin"], "xyz:SP500")   # the bare "SP500" with dex xyz is an HTTP 500
        self.assertEqual(asked[0]["dex"], "xyz")


class StrategyUpnlTests(unittest.TestCase):
    def daemon(self):
        position = SimpleNamespace(wallet="0xabc0000000000000000000000000000000c248", coin="ETH", quantity=-1.0,
                                   entry_price=2000.0, mark_price=1900.0)
        return SimpleNamespace(state=SimpleNamespace(wallets=["w"], snapshot=lambda wallets: SimpleNamespace(positions=[position])))

    def test_live_uPnL_by_strategy_is_published_with_the_alerts(self):
        from desk import journal

        alerts = hl_daemon.Alerts(path=None)
        with patch.object(journal, "ib_positions", return_value={"U1238440": [{"symbol": "MU", "quantity": 5, "unrealized": -20.0}]}), \
                patch.object(journal, "load", return_value=[]):
            alerts.refresh_strategy_upnl(self.daemon())
        state = alerts.state()
        self.assertAlmostEqual(state["strategy_upnl"]["untagged"], 100.0 - 20.0)   # HL ETH short +100, IB MU -20
        self.assertIsNotNone(state["strategy_upnl_at"])
        self.assertTrue(alerts.dirty)        # so it reaches the relay at once, not at the 15-minute heartbeat

    def test_without_ib_the_figure_is_withheld_not_partial(self):
        from desk import journal

        alerts = hl_daemon.Alerts(path=None)
        with patch.object(journal, "ib_positions", return_value={}):
            alerts.refresh_strategy_upnl(self.daemon())
        self.assertIsNone(alerts.state()["strategy_upnl"])


class AutoTraderTests(unittest.TestCase):
    def test_the_standing_order_answers_both_confirmations(self):
        session = hl_daemon.AutoSession()
        self.assertEqual(session.ask("Type LIVE TRADING to submit these mainnet orders: "), "LIVE TRADING")
        self.assertEqual(session.ask("Type SUBMIT ORDERS to send the refreshed plan: "), "SUBMIT ORDERS")
        self.assertEqual(session.ask("Type SUBMIT POSITION ACTIONS to execute the refreshed plan: "), "SUBMIT POSITION ACTIONS")

    def test_each_rule_acts_once_per_episode(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as folder, patch.object(hl_daemon, "AUTO_STATE", Path(folder) / "state.json"):
            auto = hl_daemon.AutoTrader(SimpleNamespace())
            self.assertTrue(auto._claim("flip", 12))
            self.assertFalse(auto._claim("flip", 12))       # the same episode: no second trade
            self.assertTrue(auto._claim("bank", 12))
            again = hl_daemon.AutoTrader(SimpleNamespace())  # a restart remembers
            self.assertFalse(again._claim("flip", 12))


class NeutralLadderTests(unittest.TestCase):
    def setUp(self):
        hl_daemon.load_trading()

    def quotes(self, position, allow_adds=True):
        # ETH-like: mid 2000, spacing 10, $11 levels, at most 5 levels either way.
        return hl_daemon.neutral_quotes(position, 2000.0, 1999.9, 2000.1, 10.0, 4, 5, 11.0, 5, 0.5, allow_adds)

    def test_flat_quotes_both_sides_symmetrically(self):
        quotes = self.quotes(0.0)
        bids = sorted((q["price"] for q in quotes if q["is_buy"]), reverse=True)
        asks = sorted(q["price"] for q in quotes if not q["is_buy"])
        self.assertEqual((len(bids), len(asks)), (5, 5))
        self.assertAlmostEqual(bids[0], 1990.0)
        self.assertAlmostEqual(asks[0], 2010.0)
        self.assertTrue(all(not q["reduce_only"] and q["size"] * q["price"] >= 10 for q in quotes))

    def test_inventory_skews_quotes_and_caps_the_side(self):
        long_three = 3 * 11.0 / 2000.0
        quotes = self.quotes(long_three)
        bids = sorted((q["price"] for q in quotes if q["is_buy"]), reverse=True)
        asks = sorted(q["price"] for q in quotes if not q["is_buy"])
        self.assertLess(bids[0], 1990.0)          # leaning back to flat: buys further away...
        self.assertLess(asks[0], 2010.0)          # ...sells nearer
        self.assertEqual(len(bids), 2)            # two levels of room left on the long side
        full = self.quotes(5 * 11.0 / 2000.0)
        self.assertFalse(any(q["is_buy"] for q in full))

    def test_margin_pause_keeps_only_the_reducing_side(self):
        quotes = self.quotes(-2 * 11.0 / 2000.0, allow_adds=False)
        self.assertTrue(quotes and all(q["is_buy"] for q in quotes))
        self.assertEqual(self.quotes(0.0, allow_adds=False), [])


class TrimRuleTests(unittest.TestCase):
    def _trim(self, coin, up):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        hl_daemon.load_trading()
        with tempfile.TemporaryDirectory() as folder, patch.object(hl_daemon, "AUTO_STATE", Path(folder) / "s.json"), \
                patch.object(hl_daemon, "AUTO_CONFIG", Path(folder) / "none.json"), \
                patch.object(hl_daemon.ht, "KILL_SWITCH_FILE", Path(folder) / "KILL_SWITCH"):
            auto = hl_daemon.AutoTrader(SimpleNamespace())
            spawned = []
            with patch.object(auto, "_positions", return_value=[("0xa", "BTC", 0.01, 1.0), ("0xa", "WLD", -100.0, 1.0)]), \
                    patch.object(auto, "_spawn", side_effect=lambda name, work, coins=None: spawned.append(name)):
                auto.trim_favoured(coin, up, "test")
            return spawned

    def test_crypto_up_days_leave_the_longs(self):
        # Binance 2020-2026: BTC kept rising after a 2-sigma up day; trimming the longs lost in both halves.
        self.assertEqual(self._trim("BTC", True), [])

    def test_down_days_still_trim_the_shorts_and_stocks_still_trim(self):
        self.assertEqual(self._trim("BTC", False), ["trimmed shorts"])
        self.assertEqual(self._trim("xyz:SP500", True), ["trimmed longs"])

    def test_flip_is_off_by_default(self):
        self.assertFalse(hl_daemon.AUTO_DEFAULTS["momentum_flip"])


class ResizeTests(unittest.TestCase):
    def test_close_reduce_and_add(self):
        position = {"coin": "xyz:AMD", "szi": "0.4", "marginUsed": "60"}
        self.assertIn("close,xyz:AMD,LONG,xyz,", hl_daemon.resize_actions(position, "close", 1))
        self.assertIn("adjust_margin,xyz:AMD,LONG,xyz,-20.00", hl_daemon.resize_actions(position, "reduce", 1 / 3))
        self.assertIn("adjust_margin,xyz:AMD,LONG,xyz,20.00", hl_daemon.resize_actions(position, "add", 1 / 3))
        self.assertIn("close,", hl_daemon.resize_actions(position, "reduce", 1.0))   # reducing all of it is a close
        short = {"coin": "SYRUP", "szi": "-9805", "marginUsed": "300"}
        self.assertIn("adjust_margin,SYRUP,SHORT,,-100.00", hl_daemon.resize_actions(short, "reduce", 1 / 3))

    def test_bad_requests_are_refused(self):
        with self.assertRaises(ValueError):
            hl_daemon.resize_actions({"coin": "BTC", "szi": "1", "marginUsed": "10"}, "flip", 0.5)
        with self.assertRaises(ValueError):
            hl_daemon.resize_actions({"coin": "BTC", "szi": "1", "marginUsed": "10"}, "reduce", 1.5)
        with self.assertRaises(ValueError):
            hl_daemon.resize_actions({"coin": "BTC", "szi": "1", "marginUsed": "0"}, "reduce", 0.5)


class ActedTests(unittest.TestCase):
    def test_acted_is_true_only_inside_the_episode(self):
        import tempfile
        import time
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        hl_daemon.load_trading()
        with tempfile.TemporaryDirectory() as folder, patch.object(hl_daemon, "AUTO_STATE", Path(folder) / "s.json"), \
                patch.object(hl_daemon, "AUTO_CONFIG", Path(folder) / "none.json"):
            auto = hl_daemon.AutoTrader(SimpleNamespace())
            self.assertFalse(auto.acted("trim:xyz:SP500:down"))
            auto._claim("trim:xyz:SP500:down", 12)
            self.assertTrue(auto.acted("trim:xyz:SP500:down"))           # its own alert now says it was done
            auto.done["trim:xyz:SP500:down"] = time.time() - 13 * 3600
            self.assertFalse(auto.acted("trim:xyz:SP500:down"))          # a new episode raises the advisory again


class BuildUpTests(unittest.TestCase):
    def _step(self, delta, held=-100.0, alerts=(), block=""):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        hl_daemon.load_trading()
        plan = SimpleNamespace(quantity_delta=delta, mark_price=1.0, block_reason=block)
        with tempfile.TemporaryDirectory() as folder, patch.object(hl_daemon, "AUTO_STATE", Path(folder) / "s.json"), \
                patch.object(hl_daemon.daemon_link, "RUN_DIR", Path(folder)), \
                patch.object(hl_daemon.ht, "KILL_SWITCH_FILE", Path(folder) / "KILL_SWITCH"):
            daemon = MagicMock()
            daemon.state.position_quantity.return_value = held
            daemon.alerts.items = {name: {} for name in alerts}
            daemon._plan.return_value = ("actions", [plan], {}, None)
            daemon.trade.return_value = 0
            builder = hl_daemon.BuildUps(daemon, None)
            builder.step({"wallet": "0xa", "coin": "TRUMP", "side": "SHORT"})
            return daemon.trade.called

    def test_adds_when_the_plan_has_room(self):
        self.assertTrue(self._step(80.0))

    def test_never_cuts_and_ignores_dust_adds(self):
        self.assertFalse(self._step(-40.0))
        self.assertFalse(self._step(5.0))

    def test_waits_while_blocked_or_a_short_squeeze_alert_is_up(self):
        self.assertFalse(self._step(80.0, block="cap"))
        self.assertFalse(self._step(80.0, alerts=("short_strength",)))

    def test_only_grows_the_position_it_was_set_for(self):
        self.assertTrue(self._step(80.0, held=0.0))      # not open yet: opens at the room there is
        self.assertFalse(self._step(80.0, held=100.0))   # flipped long: not the short build-up


class FlattenTests(unittest.TestCase):
    def test_adverse_momentum_raises_a_test_thesis_notice_and_closes_nothing(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        hl_daemon.load_trading()
        with tempfile.TemporaryDirectory() as folder, patch.object(hl_daemon, "AUTO_STATE", Path(folder) / "s.json"), \
                patch.object(hl_daemon.ht, "KILL_SWITCH_FILE", Path(folder) / "KILL_SWITCH"):
            marks = {"WLD": 1.0, "ENA": 0.1}
            daemon = SimpleNamespace(alerts=SimpleNamespace(set=lambda *a, **k: None),
                                     state=SimpleNamespace(mark=lambda coin: marks[coin]))
            auto = hl_daemon.AutoTrader(daemon)
            notes = []
            with patch.object(auto, "_positions", return_value=[("0xa", "WLD", -100.0, 10.0), ("0xa", "ENA", -500.0, -10.0)]), \
                    patch.object(auto, "_reduce") as reduce, \
                    patch("desk.journal.load", return_value=[{"wallet": "0xa", "coin": "ENA", "strategy": "fade", "closed": None}]), \
                    patch.object(auto, "_spawn", side_effect=lambda name, work, coins=None: notes.append((name, work()))):
                auto.flatten("BTC up 3%")
            reduce.assert_not_called()
            self.assertFalse((Path(folder) / "KILL_SWITCH").exists())
            name, note = notes[0]
            self.assertEqual(name, "test thesis?")
            self.assertIn("Nothing was closed", note)
            self.assertLess(note.index("ENA"), note.index("WLD"))   # worst against its entry first
            self.assertIn("ENA short -25% ALL (fade)", note)


class InfoUsageTests(unittest.TestCase):
    def test_usage_line_weighs_requests_per_minute(self):
        from desk.hl import trade_analytics as ta
        line = ta.info_usage_line(60, {"candleSnapshot": [3, 1], "clearinghouseState": [10, 0]})
        self.assertIn("~80 of 1200 weight/min", line)       # 3 x 20 + 10 x 2
        self.assertIn("candleSnapshot 3.0/min (1 x 429)", line)

    def test_post_info_counts_requests(self):
        from unittest import mock
        from desk.hl import trade_analytics as ta
        ta.info_usage()
        response = mock.Mock(status_code=200, ok=True)
        response.json.return_value = {}
        with mock.patch.object(ta, "_session") as session:
            session.return_value.post.return_value = response
            ta.post_info({"type": "allMids"})
        self.assertEqual(ta.info_usage()[1], {"allMids": [1, 0]})
