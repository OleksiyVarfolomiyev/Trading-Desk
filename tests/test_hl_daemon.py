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
        self.exchange.order.return_value = filled(30, 7.98)
        self.exchange.bulk_orders.return_value = statuses({"resting": {"oid": 2}}, {"resting": {"oid": 3}})
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

    def test_an_urgent_entry_crosses_once_then_sends_stop_and_target_together_and_starts_a_guard(self):
        session = FakeSession("LIVE TRADING", "SUBMIT ORDERS")
        self.assertEqual(self.daemon.trade(self.request(live=True), session), 0)
        (coin, is_buy, size, limit, order_type), kwargs = self.exchange.order.call_args
        self.assertEqual((coin, is_buy, order_type, kwargs), ("AVAX", False, {"limit": {"tif": "Ioc"}},
                                                              {"reduce_only": False}))
        self.assertAlmostEqual(limit, 7.96)  # the $8.00 mid less 0.5%
        [requests_sent], _ = self.exchange.bulk_orders.call_args
        self.assertEqual([("trigger" in item["order_type"], item["reduce_only"], item["sz"]) for item in requests_sent],
                         [(True, True, 30.0), (False, True, 30.0)])
        self.assertIn((WALLET, "AVAX"), self.daemon.guards.entries)
        self.assertEqual(self.daemon.guards.entries[(WALLET, "AVAX")].trade.entry_price, 7.98)

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
        self.exchange.order.assert_called_once()

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
        with self.assertRaisesRegex(RuntimeError, "no fill within 0.50%"):
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

    def test_batched_exits_send_stop_and_target_in_one_request(self):
        exchange = MagicMock()
        exchange.bulk_orders.return_value = statuses({"resting": {"oid": 2}}, {"resting": {"oid": 3}})
        self.assertEqual(ht._place_exit_orders(exchange, self.plan, 30, 8.0, 2, batched=True), (2, 3))
        exchange.order.assert_not_called()

    def test_a_stop_that_cannot_rest_cancels_the_lone_target(self):
        exchange = MagicMock()
        exchange.bulk_orders.return_value = statuses({"error": "bad trigger"}, {"resting": {"oid": 3}})
        with self.assertRaisesRegex(RuntimeError, "stop-market"):
            ht._place_exit_orders(exchange, self.plan, 30, 8.0, 2, batched=True)
        exchange.cancel.assert_called_once_with("AVAX", 3)

    def test_a_target_that_would_cross_is_placed_again_outside_the_spread(self):
        exchange = MagicMock()
        exchange.bulk_orders.return_value = statuses({"resting": {"oid": 2}}, {"error": "Post only would match"})
        exchange.order.return_value = statuses({"resting": {"oid": 4}})
        with patch.object(ht, "post_only_price", return_value=7.5):
            self.assertEqual(ht._place_exit_orders(exchange, self.plan, 30, 8.0, 2, batched=True), (2, 4))

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

    def test_orders_under_ten_dollars_are_left_out(self):
        self.assertEqual(hl_daemon.ladder_quotes(10.0, 10.0, 10.0, 9.99, 10.01, 1.0, 1, self.config), [])

    def test_trims_can_be_switched_off_leaving_the_adds(self):
        quotes = hl_daemon.ladder_quotes(-100.0, -100.0, 10.0, 9.99, 10.01, 1.0, 1, self.config, True, False)
        self.assertTrue(quotes)
        self.assertTrue(all(q["kind"] == "add" and not q["is_buy"] for q in quotes))


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
    config = {**hl_daemon.LADDER_DEFAULTS}

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
