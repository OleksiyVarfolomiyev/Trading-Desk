"""HL trading daemon: live account and market data, ready signing clients, guards that react at once.

The executor (``hl_trade.py``) reads everything from scratch on each run: about 40 requests and
several seconds before the first order, and its guards poll every 15 seconds, one process per
trade. This daemon starts once and keeps:

* every wallet's positions and margin on every DEX, pushed by HL's websocket about every
  5 seconds (``allDexsClearinghouseState``), plus USDC collateral (``spotState``);
* every market's metadata and prices (``allDexsAssetCtxs``), with a price per second for the
  coins held, guarded or being traded (``activeAssetCtx``);
* order and fill events (``orderUpdates``, ``userFills``);
* the other desk accounts' equity and open risk (IB), re-read every minute;
* one signing client per wallet whose API key is in the Keychain, built at startup.

Orders go through the same planning, risk caps, confirmations and order code as the executor;
only the waiting is gone. Guards run inside the daemon: a price below the loss limit closes the
trade within about a second, and a filled stop or take-profit is acted on as HL reports it.

Commands (run with the trading environment, ``.venv/bin/python``):

    hl_daemon.py serve [--paper]       start the daemon (Ctrl-C stops it; guards resume on restart)
    hl_daemon.py trade --wallet 07f2 --signals short.csv [--urgent] [--live]
    hl_daemon.py trade --wallet 07f2 --actions resize.csv [--live]
    hl_daemon.py status                wallets, data age, guards
    hl_daemon.py reattach --wallet 07f2 [--coins TRUMP,VVV] [--live]
    hl_daemon.py stop

``trade`` takes the executor's flags and asks for the same typed confirmations (LIVE TRADING,
then SUBMIT ORDERS or SUBMIT POSITION ACTIONS). ``--paper`` runs on live data with a stand-in
exchange that sends nothing, for trying the daemon out.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import itertools
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from desk.hl import daemon_link  # noqa: E402

# The trading code (pandas, the executor) loads only in the server: the client commands then
# start in a fraction of a second.
pd: Any = None
ht: Any = None
ExpectedPnl: Any = None
is_unified_account: Any = None
post_info: Any = None
URGENT_SLIPPAGE_PCT = 0.5  # hl_trade.URGENT_SLIPPAGE, in percent


def load_trading() -> None:
    """Import pandas and the executor (once)."""
    global pd, ht, ExpectedPnl, is_unified_account, post_info
    if ht is not None:
        return
    import pandas
    import hl_trade
    from desk.hl import trade_analytics

    pd, ht = pandas, hl_trade
    ExpectedPnl = trade_analytics.ExpectedPnl
    is_unified_account, post_info = trade_analytics.is_unified_account, trade_analytics.post_info

WS_URL = "wss://api.hyperliquid.xyz/ws"
GUARDS_FILE = daemon_link.RUN_DIR / "hl_guards.json"
LOG_FILE = daemon_link.RUN_DIR / "hl_daemon.log"
# HL pushes account state about every 5 s and all prices about every 12 s; a coin's own price
# about every second. Older than these, the daemon reads HL directly instead.
STATE_STALE_SECONDS = 15
MARK_STALE_SECONDS = 5
# A websocket silent this long is reconnected; HL drops idle connections after 60 s.
WS_SILENCE_SECONDS = 45
WS_PING_SECONDS = 20
OTHER_VENUES_EVERY_SECONDS = 120
SLOW_REFRESH_EVERY_SECONDS = 30 * 60
GUARD_CHECK_SECONDS = 60
EXIT_RETRY_SECONDS = 5
FUNDING_REFRESH_SECONDS = 10 * 60
PROMPT_TIMEOUT_SECONDS = 180
ACTIONS = {"order", "bulk_orders", "cancel", "bulk_cancel", "market_open", "market_close", "update_leverage",
           "modify_order", "bulk_modify_orders_new", "cancel_by_cloid", "bulk_cancel_by_cloid"}


# ----------------------------------------------------------------------------------------------
# Output: each request's prints go to its client; everything else to the daemon's log.

def log(message: str) -> None:
    """A timestamped line in the daemon's terminal and log file."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC {message}", file=_LOG)


class _Tee(io.TextIOBase):
    """Write to the terminal and append to the log file."""

    def __init__(self, stream, path: Path | None):
        self.stream, self.path = stream, path

    def write(self, text: str) -> int:
        text = daemon_link.mask(text)
        self.stream.write(text)
        self.stream.flush()
        if self.path is not None:
            try:
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(text)
            except OSError:
                pass
        return len(text)

    def flush(self) -> None:
        self.stream.flush()


class _Router(io.TextIOBase):
    """``sys.stdout`` replacement: a request thread's prints go to its client, others to the log."""

    def __init__(self, fallback):
        self.fallback = fallback
        self.local = threading.local()

    @contextlib.contextmanager
    def bind(self, writer: Callable[[str], None]):
        previous = getattr(self.local, "writer", None)
        self.local.writer = writer
        try:
            yield
        finally:
            self.local.writer = previous

    def write(self, text: str) -> int:
        writer = getattr(self.local, "writer", None)
        if writer is not None:
            try:
                writer(text)
                return len(text)
            except OSError:  # the client went away: keep the record in the log
                pass
        return self.fallback.write(text)

    def flush(self) -> None:
        self.fallback.flush()


_LOG: Any = sys.stdout
OUTPUT: _Router | None = None


# ----------------------------------------------------------------------------------------------
# Websocket connections

class Stream:
    """One HL websocket: subscribes on every (re)connect, pings, and reconnects with backoff."""

    def __init__(self, name: str, subscriptions: list[dict], on_message: Callable[[str, Any], None],
                 url: str = WS_URL):
        self.name, self.url, self.on_message = name, url, on_message
        self.subscriptions = list(subscriptions)
        self.last_message = 0.0
        self.connected = False
        self._ws = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._backoff = 1.0

    def start(self) -> None:
        threading.Thread(target=self._run, name=f"ws-{self.name}", daemon=True).start()
        threading.Thread(target=self._keepalive, name=f"ws-{self.name}-ping", daemon=True).start()

    def add(self, subscription: dict) -> None:
        """Subscribe now (and on every reconnect)."""
        with self._lock:
            if subscription in self.subscriptions:
                return
            self.subscriptions.append(subscription)
            ws = self._ws
        if ws is not None and self.connected:
            try:
                ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))
            except Exception:  # resent on reconnect
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                self._ws.close()

    def age(self) -> float:
        return time.time() - self.last_message if self.last_message else float("inf")

    def _run(self) -> None:
        import websocket

        while not self._stop.is_set():
            ws = websocket.WebSocketApp(self.url, on_open=self._opened, on_message=self._received)
            with self._lock:
                self._ws = ws
            try:
                ws.run_forever()
            except Exception as exc:
                log(f"websocket {self.name} failed: {exc}")
            self.connected = False
            if self._stop.is_set():
                break
            log(f"websocket {self.name} disconnected; reconnecting in {self._backoff:.0f}s")
            self._stop.wait(self._backoff)
            self._backoff = min(30.0, self._backoff * 2)

    def _opened(self, ws) -> None:
        self.connected = True
        self._backoff = 1.0
        self.last_message = time.time()
        with self._lock:
            subscriptions = list(self.subscriptions)
        for subscription in subscriptions:
            ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))

    def _received(self, _ws, message: str) -> None:
        self.last_message = time.time()
        try:
            data = json.loads(message)
        except ValueError:
            return
        channel = data.get("channel") if isinstance(data, dict) else None
        if channel in (None, "pong", "subscriptionResponse"):
            return
        if channel == "error":
            log(f"websocket {self.name}: HL reported {data.get('data')}")
            return
        try:
            self.on_message(channel, data.get("data"))
        except Exception:
            log(f"websocket {self.name}: could not handle a {channel} message:\n{traceback.format_exc()}")

    def _keepalive(self) -> None:
        while not self._stop.wait(WS_PING_SECONDS):
            ws = self._ws
            if ws is None or not self.connected:
                continue
            if self.age() > WS_SILENCE_SECONDS:
                log(f"websocket {self.name} silent for {self.age():.0f}s; reconnecting")
                with contextlib.suppress(Exception):
                    ws.close()
                continue
            with contextlib.suppress(Exception):
                ws.send(json.dumps({"method": "ping"}))


# ----------------------------------------------------------------------------------------------
# Live HL data

def _valid_state(state: Any) -> bool:
    return (isinstance(state, dict) and isinstance(state.get("assetPositions"), list)
            and isinstance(state.get("marginSummary"), dict))


def _usdc_total(balances: Any) -> float | None:
    if not isinstance(balances, list):
        return None
    usdc = next((item for item in balances if isinstance(item, dict) and item.get("coin") == "USDC"), None)
    try:
        total = float(usdc["total"]) if usdc is not None else 0.0
    except (KeyError, TypeError, ValueError):
        return None
    return total if total >= 0 and total == total else None


class HLState:
    """Wallet and market data kept current by the websockets, read from HL when stale."""

    def __init__(self, wallets: list[str]):
        self.wallets = list(dict.fromkeys(wallet.lower() for wallet in wallets))
        self.lock = threading.RLock()
        self.dexes: list[str] = []
        self.meta: dict[str, dict] = {}
        self.ctxs: dict[str, list] = {}
        self.ctx_time: dict[str, float] = {}
        self.coin_ctx: dict[str, tuple[dict, float]] = {}
        self.states: dict[str, dict[str, dict]] = {}
        self.state_time: dict[str, float] = {}
        self.spot_usdc: dict[str, tuple[float, float]] = {}
        self.unified: dict[str, bool] = {}
        self.fees: dict[str, tuple[float, float]] = {}
        self.other: tuple[Any, float] | None = None
        self.meta_stale = False
        self.listeners: list[Callable[[str, str, Any], None]] = []
        self.watch: Callable[[str], None] = lambda coin: None

    # -- loading from HL (startup, and whenever the websocket data is stale)
    def load_markets(self) -> None:
        dexes = ht.get_dexes([])
        responses = ht._gather([lambda dex=dex: ht.fetch_market_response(dex) for dex in dexes])
        now = time.time()
        with self.lock:
            self.dexes = dexes
            for dex, response in zip(dexes, responses):
                ht.markets_from_response(dex, response)  # validates the shape
                self.meta[dex], self.ctxs[dex], self.ctx_time[dex] = response[0], response[1], now
            self.meta_stale = False

    def refresh_dexes(self, dexes: set[str]) -> None:
        """Re-read prices (and metadata) for some DEXes now."""
        dexes = sorted(dexes)
        responses = ht._gather([lambda dex=dex: ht.fetch_market_response(dex) for dex in dexes])
        now = time.time()
        with self.lock:
            for dex, response in zip(dexes, responses):
                ht.markets_from_response(dex, response)
                self.meta[dex], self.ctxs[dex], self.ctx_time[dex] = response[0], response[1], now

    def load_wallets(self, wallets: list[str], slow: bool = True) -> None:
        """Read wallets' states on every DEX (and, with ``slow``, account mode and fee rates)."""
        def read(wallet: str) -> tuple:
            states = ht._gather([lambda dex=dex: ht.fetch_user_state(wallet, dex) for dex in self.dexes])
            unified = is_unified_account(wallet) if slow or wallet not in self.unified else self.unified[wallet]
            usdc = None
            if unified:
                usdc = _usdc_total((post_info({"type": "spotClearinghouseState", "user": wallet}) or {}).get("balances"))
                if usdc is None:
                    raise ValueError(f"Wallet ...{wallet[-4:]} returned an invalid USDC balance.")
            fees = ht.parse_fee_rates(wallet) if slow or wallet not in self.fees else self.fees[wallet]
            return wallet, dict(zip(self.dexes, states)), unified, usdc, fees

        results = ht._gather([lambda wallet=wallet: read(wallet) for wallet in wallets])
        now = time.time()
        with self.lock:
            for wallet, states, unified, usdc, fees in results:
                self.states[wallet], self.state_time[wallet] = states, now
                self.unified[wallet], self.fees[wallet] = unified, fees
                if usdc is not None:
                    self.spot_usdc[wallet] = (usdc, now)

    def refresh_other(self) -> None:
        from desk.risk import other_venue_book

        book = other_venue_book("hl", already_counted=tuple(self.wallets))
        with self.lock:
            self.other = (book, time.time())

    # -- websocket messages
    def on_wallet(self, wallet: str, channel: str, data: Any) -> None:
        now = time.time()
        if channel == "allDexsClearinghouseState":
            items = data.get("clearinghouseStates") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return
            parsed = {str(item[0]): item[1] for item in items
                      if isinstance(item, list) and len(item) == 2 and _valid_state(item[1])}
            with self.lock:
                self.states[wallet] = {**self.states.get(wallet, {}), **parsed}
                if all(dex in parsed for dex in self.dexes):
                    self.state_time[wallet] = now
            self._notify("state", wallet, None)
        elif channel == "spotState":
            usdc = _usdc_total(((data or {}).get("spotState") or {}).get("balances"))
            if usdc is not None:
                with self.lock:
                    self.spot_usdc[wallet] = (usdc, now)
        elif channel == "orderUpdates":
            self._notify("orders", wallet, data)
        elif channel == "userFills" and isinstance(data, dict) and not data.get("isSnapshot"):
            self._notify("fills", wallet, data)

    def on_market(self, channel: str, data: Any) -> None:
        now = time.time()
        if channel == "allDexsAssetCtxs":
            for item in (data or {}).get("ctxs", []):
                if not (isinstance(item, list) and len(item) == 2 and isinstance(item[1], list)):
                    continue
                dex, ctxs = str(item[0]), item[1]
                with self.lock:
                    meta = self.meta.get(dex)
                    if meta is None or len(meta.get("universe", [])) != len(ctxs):
                        self.meta_stale = True  # a new listing: metadata is re-read
                        continue
                    self.ctxs[dex], self.ctx_time[dex] = ctxs, now
        elif channel == "activeAssetCtx" and isinstance(data, dict) and isinstance(data.get("ctx"), dict):
            coin = str(data.get("coin"))
            with self.lock:
                self.coin_ctx[coin] = (data["ctx"], now)
            self._notify("mark", coin, data["ctx"])

    def _notify(self, kind: str, key: str, payload: Any) -> None:
        for listener in list(self.listeners):
            try:
                listener(kind, key, payload)
            except Exception:
                log(f"listener failed on {kind}:\n{traceback.format_exc()}")

    # -- reads used for planning
    def mark(self, coin: str, max_age: float = MARK_STALE_SECONDS) -> float | None:
        with self.lock:
            item = self.coin_ctx.get(coin)
        if item is None or time.time() - item[1] > max_age:
            return None
        try:
            mark = float(item[0].get("markPx") or 0)
        except (TypeError, ValueError):
            return None
        return mark if mark > 0 else None

    def markets(self, trading: list[tuple[str, str]] = ()) -> dict[str, ht.Market]:
        """Every market; the ones being traded carry a price from the last few seconds."""
        if self.meta_stale:
            self.load_markets()
        stale = set()
        for coin, dex in trading:
            self.watch(coin)
            if self.mark(coin) is None:
                stale.add(dex)
        if stale:
            self.refresh_dexes(stale)
        now = time.time()
        with self.lock:
            markets: dict[str, ht.Market] = {}
            for dex in self.dexes:
                markets.update(ht.markets_from_response(dex, [self.meta[dex], self.ctxs[dex]]))
            fresh = {coin: ctx for coin, (ctx, stamp) in self.coin_ctx.items() if now - stamp <= MARK_STALE_SECONDS}
        for coin, ctx in fresh.items():
            if coin not in markets:
                continue
            try:
                mark = float(ctx.get("markPx") or 0)
                oracle = float(ctx.get("oraclePx") or mark or 0)
            except (TypeError, ValueError):
                continue
            if mark > 0 and oracle > 0:
                markets[coin] = dataclasses.replace(markets[coin], mark_price=mark, oracle_price=oracle)
        return markets

    def collateral(self, wallet: str, perp_equity: float) -> float:
        """Margin collateral: USDC on unified accounts (from the websocket when fresh), else perp equity."""
        if wallet not in self.unified:
            self.unified[wallet] = is_unified_account(wallet)
        if not self.unified[wallet]:
            return perp_equity
        with self.lock:
            usdc, stamp = self.spot_usdc.get(wallet, (None, 0.0))
        if usdc is None or time.time() - stamp > STATE_STALE_SECONDS:
            usdc = ht.fetch_collateral_equity(wallet, perp_equity)
            with self.lock:
                self.spot_usdc[wallet] = (usdc, time.time())
        return usdc

    def ensure_fresh(self, wallets: list[str], max_age: float = STATE_STALE_SECONDS) -> None:
        now = time.time()
        stale = [wallet for wallet in wallets if now - self.state_time.get(wallet, 0.0) > max_age]
        if stale:
            self.load_wallets(stale, slow=False)

    def snapshot(self, wallets: list[str], markets: dict[str, ht.Market] | None = None) -> ht.AccountSnapshot:
        """The executor's account snapshot, built from live data instead of ~25 requests."""
        wallets = list(dict.fromkeys(wallet.lower() for wallet in wallets))
        self.ensure_fresh(wallets)
        markets = markets if markets is not None else self.markets()
        with self.lock:
            states = {(wallet, dex): self.states[wallet][dex] for wallet in wallets for dex in self.dexes}
            fees = self.fees.get(wallets[0])
        try:
            return ht.collect_snapshot(wallets, self.dexes, markets, states=states,
                                       collateral=self.collateral, fee_rates=fees)
        except ValueError as exc:
            if "Cannot price existing position" not in str(exc):
                raise
            self.load_markets()  # a position in a market listed since startup
            return ht.collect_snapshot(wallets, self.dexes, self.markets(), states=states,
                                       collateral=self.collateral, fee_rates=fees)

    def position_quantity(self, wallet: str, coin: str, dex: str) -> float | None:
        """Signed size from the live state, or None when that state is stale."""
        with self.lock:
            if time.time() - self.state_time.get(wallet, 0.0) > STATE_STALE_SECONDS:
                return None
            state = self.states.get(wallet, {}).get(dex)
        if state is None:
            return None
        for item in state.get("assetPositions", []):
            position = item.get("position", {})
            if position.get("coin") == coin:
                return float(position.get("szi") or 0)
        return 0.0

    def held_coins(self) -> set[str]:
        with self.lock:
            return {str(item.get("position", {}).get("coin")) for states in self.states.values()
                    for state in states.values() for item in state.get("assetPositions", [])
                    if float(item.get("position", {}).get("szi") or 0)}


# ----------------------------------------------------------------------------------------------
# Signing clients

class SerialExchange:
    """A wallet's signing client, one action at a time (HL nonces are millisecond timestamps)."""

    def __init__(self, exchange: Any):
        self._exchange = exchange
        self._lock = threading.RLock()

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._exchange, name)
        if name not in ACTIONS or not callable(attribute):
            return attribute

        def serialized(*args, **kwargs):
            with self._lock:
                return attribute(*args, **kwargs)

        return serialized


class PaperExchange:
    """Answers like HL's exchange endpoint and sends nothing (``serve --paper``)."""

    def __init__(self, wallet: str):
        self.wallet = wallet
        self.ids = itertools.count(1)
        self.sent: list[dict] = []

    def _ok(self, kind: str, statuses: list) -> dict:
        return {"status": "ok", "response": {"type": kind, "data": {"statuses": statuses}}}

    def update_leverage(self, leverage, coin, is_cross=True):
        self.sent.append({"leverage": leverage, "coin": coin, "cross": is_cross})
        print(f"[paper] leverage {leverage}x for {coin}")
        return {"status": "ok"}

    def order(self, coin, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None, builder=None):
        return self.bulk_orders([{"coin": coin, "is_buy": is_buy, "sz": sz, "limit_px": limit_px,
                                  "order_type": order_type, "reduce_only": reduce_only}])

    def bulk_orders(self, requests, builder=None, grouping="na"):
        statuses = []
        for request in requests:
            self.sent.append(request)
            oid = next(self.ids)
            kind = request["order_type"]
            side = "buy" if request["is_buy"] else "sell"
            if kind.get("limit", {}).get("tif") == "Ioc":
                coin, dex = ht.normalize_market(request["coin"])
                mid = ht._book_mid(coin, dex)
                crosses = mid <= request["limit_px"] if request["is_buy"] else mid >= request["limit_px"]
                print(f"[paper] IOC {side} {request['sz']:g} {request['coin']} limit {request['limit_px']:g}: "
                      + (f"filled at the ${mid:,.6g} mid" if crosses else "no fill"))
                statuses.append({"filled": {"totalSz": str(request["sz"]), "avgPx": str(mid), "oid": oid}}
                                if crosses else {"error": "Order could not immediately match against any resting orders."})
            else:
                label = "stop" if "trigger" in kind else "limit"
                price = kind["trigger"]["triggerPx"] if "trigger" in kind else request["limit_px"]
                print(f"[paper] {label} {side} {request['sz']:g} {request['coin']} at {price:g}"
                      f"{' reduce-only' if request.get('reduce_only') else ''} rests as order {oid}")
                statuses.append({"resting": {"oid": oid}})
        return self._ok("order", statuses)

    def cancel(self, coin, oid):
        print(f"[paper] cancel {coin} order {oid}")
        return self._ok("cancel", ["success"])

    def market_close(self, coin, sz=None, px=None, slippage=0.05, cloid=None, builder=None):
        print(f"[paper] market close {sz} {coin}")
        return self._ok("order", [{"filled": {"totalSz": str(sz), "avgPx": "0", "oid": next(self.ids)}}])


# ----------------------------------------------------------------------------------------------
# Guards

def _plain(value: Any) -> Any:
    """JSON for numpy scalars (their Python value) and anything else (its text)."""
    return value.item() if hasattr(value, "item") else str(value)


def trade_to_json(wallet: str, trade: ht.ActiveTrade) -> dict:
    return {"wallet": wallet, "plan": dataclasses.asdict(trade.plan), "quantity": trade.quantity,
            "entry_price": trade.entry_price, "baseline_quantity": trade.baseline_quantity,
            "stop_oid": trade.stop_oid, "target_oid": trade.target_oid,
            "started_at": trade.started_at.isoformat(), "prior_funding_pnl": trade.prior_funding_pnl,
            "exiting": trade.exiting}


def trade_from_json(record: dict) -> tuple[str, ht.ActiveTrade]:
    plan = dict(record["plan"])
    plan["expected_pnl"] = ExpectedPnl(**plan["expected_pnl"])
    trade = ht.ActiveTrade(
        plan=ht.PlannedTrade(**plan), quantity=float(record["quantity"]), entry_price=float(record["entry_price"]),
        baseline_quantity=float(record["baseline_quantity"]), stop_oid=int(record["stop_oid"]),
        target_oid=int(record["target_oid"]), started_at=pd.Timestamp(record["started_at"]),
        prior_funding_pnl=float(record.get("prior_funding_pnl", 0.0)), exiting=bool(record.get("exiting", False)),
    )
    return record["wallet"], trade


@dataclasses.dataclass
class GuardEntry:
    wallet: str
    trade: ht.ActiveTrade
    exchange: Any
    next_check: float = 0.0
    funding_sum: float = 0.0
    funding_time: float = 0.0
    failing_since: float | None = None
    last_warning: float = 0.0

    @property
    def coin(self) -> str:
        return self.trade.plan.coin

    def budget(self) -> float:
        plan = self.trade.plan
        return plan.risk_budget * self.trade.quantity / plan.quantity if plan.quantity > 0 else 0.0


class Guards:
    """Net-loss guards for open trades, checked on every price update and every minute."""

    def __init__(self, state: HLState, path: Path | None = GUARDS_FILE, check: Callable | None = None):
        self.state = state
        self.path = path
        self.entries: dict[tuple[str, str], GuardEntry] = {}
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.check = check or ht._check_trade
        state.listeners.append(self.on_event)

    def add(self, wallet: str, trade: ht.ActiveTrade, exchange: Any) -> None:
        with self.lock:
            self.entries[(wallet, trade.plan.coin)] = GuardEntry(wallet, trade, exchange)
            self.save()
        self.state.watch(trade.plan.coin)
        self.wake.set()

    def remove(self, key: tuple[str, str], message: str) -> None:
        with self.lock:
            self.entries.pop(key, None)
            self.save()
        log(message)

    def save(self) -> None:
        """Write the running guards so a restarted daemon resumes them."""
        if self.path is None:
            return
        with self.lock:
            records = [trade_to_json(entry.wallet, entry.trade) for entry in self.entries.values()]
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(records, indent=1, default=_plain))
            temporary.replace(self.path)

    def load(self) -> list[tuple[str, ht.ActiveTrade]]:
        if self.path is None or not self.path.exists():
            return []
        try:
            return [trade_from_json(record) for record in json.loads(self.path.read_text())]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log(f"could not read saved guards ({exc}); none resumed")
            return []

    def on_event(self, kind: str, key: str, payload: Any) -> None:
        if kind == "mark":
            if any(entry.coin == key for entry in self.entries.values()):
                self.wake.set()
        elif kind == "orders" and isinstance(payload, list):
            oids = {int(item.get("order", {}).get("oid", -1)) for item in payload if isinstance(item, dict)}
            for entry in list(self.entries.values()):
                if entry.wallet == key and oids & {entry.trade.stop_oid, entry.trade.target_oid}:
                    entry.next_check = 0.0  # a stop or take-profit changed: check at once
                    self.wake.set()
        elif kind == "fills":
            coins = {fill.get("coin") for fill in (payload or {}).get("fills", [])}
            for entry in list(self.entries.values()):
                if entry.wallet == key and entry.coin in coins:
                    entry.next_check = 0.0
                    self.wake.set()

    def fast_check(self, entry: GuardEntry, now: float) -> None:
        """From live data only: close the tranche once its net PnL reaches the loss limit."""
        trade, plan = entry.trade, entry.trade.plan
        if trade.exiting:
            return
        mark = self.state.mark(plan.coin)
        quantity = self.state.position_quantity(entry.wallet, plan.coin, plan.dex)
        if mark is None or quantity is None:
            return
        tranche = ht.side_to_direction(plan.side) * (quantity - trade.baseline_quantity)
        if tranche <= max(1e-8, trade.quantity * 0.01):
            entry.next_check = 0.0  # gone or reversed: the full check settles it
            return
        close_quantity = min(trade.quantity, abs(tranche))
        net_pnl = ht._guard_net_pnl(trade, close_quantity, mark, entry.funding_sum,
                                    plan.maker_fee_rate, plan.taker_fee_rate)
        budget = entry.budget()
        if budget > 0 and net_pnl <= -budget:
            trade.exiting = True
            self.save()
            log(f"{plan.coin}: funding-aware net-loss guard is exiting the tranche "
                f"(estimated net PnL ${net_pnl:,.2f} at ${mark:,.6g}).")
            try:
                entry.exchange.market_close(plan.coin, close_quantity)
            except Exception as exc:  # retried by the full check until flat
                log(f"{plan.coin}: guard close failed ({exc}); retrying.")
            entry.next_check = now + EXIT_RETRY_SECONDS

    def refresh_funding(self, entry: GuardEntry, now: float) -> None:
        if now - entry.funding_time < FUNDING_REFRESH_SECONDS:
            return
        plan = entry.trade.plan
        funding = ht.fetch_funding_history(plan.coin.split(":")[-1], plan.dex,
                                           start_time=entry.trade.started_at, allow_empty=True)
        rates = funding.loc[funding["time"] >= entry.trade.started_at, "fundingRate"]
        entry.funding_sum, entry.funding_time = float(rates.sum()), now

    def run(self) -> None:
        while not self.stopped.is_set():
            self.wake.wait(1.0)
            self.wake.clear()
            self.step(time.time())

    def step(self, now: float) -> None:
        for key, entry in list(self.entries.items()):
            try:
                self.refresh_funding(entry, now)
                self.fast_check(entry, now)
                if now >= entry.next_check:
                    plan = entry.trade.plan
                    message = self.check(entry.exchange, entry.wallet, entry.trade,
                                         plan.maker_fee_rate, plan.taker_fee_rate)
                    entry.next_check = now + (EXIT_RETRY_SECONDS if entry.trade.exiting else GUARD_CHECK_SECONDS)
                    if entry.failing_since is not None:
                        log(f"{entry.coin}: guard checks recovered after {now - entry.failing_since:.0f}s.")
                        entry.failing_since = None
                    if message:
                        self.remove(key, message)
            except Exception as exc:  # the guard never stops; the exchange stop keeps protecting
                entry.failing_since = entry.failing_since or now
                entry.next_check = now + 15
                if now - entry.last_warning >= ht.GUARD_WARNING_INTERVAL_SECONDS:
                    entry.last_warning = now
                    log(f"{entry.coin}: guard check failed ({type(exc).__name__}: {exc}); retrying. "
                        "The exchange stop stays active.")


# ----------------------------------------------------------------------------------------------
# The daemon

def _memoize_market_history() -> None:
    """Keep daily candles (per UTC day) and funding (per hour) in memory instead of re-reading files."""
    candles, funding = ht.fetch_daily_candles, ht.fetch_funding_history
    memo: dict[tuple, pd.DataFrame] = {}

    def cached_candles(coin, dex="", *args, **kwargs):
        if args or kwargs:
            return candles(coin, dex, *args, **kwargs)
        key = ("candles", coin, dex, pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"))
        if key not in memo:
            memo[key] = candles(coin, dex)
        return memo[key].copy()

    def cached_funding(coin, dex="", *args, **kwargs):
        if args or kwargs:
            return funding(coin, dex, *args, **kwargs)
        key = ("funding", coin, dex, pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H"))
        if key not in memo:
            memo[key] = funding(coin, dex)
        return memo[key].copy()

    ht.fetch_daily_candles, ht.fetch_funding_history = cached_candles, cached_funding


def _prefetch_history(coin: str) -> None:
    """Load a market's daily candles and funding the way the planner asks for them."""
    name, dex = ht.normalize_market(coin)
    ht.fetch_daily_candles(name.split(":")[-1], dex)
    ht.fetch_funding_history(name.split(":")[-1], dex)


# ----------------------------------------------------------------------------------------------
# Margin guard: reduce positions when a wallet's margin is truly stressed

MARGIN_CONFIG = daemon_link.RUN_DIR / "margin_guard.json"
MARGIN_DEFAULTS = {
    "enabled": False,           # False: only log what it would do
    "warn_used": 0.80,          # margin used / equity: the 20% reserve is breached (a warning)
    "act_used": 0.90,           # danger: reduce positions from here
    "target_used": 0.85,        # reduce until about here
    "chop_fraction": 0.25,      # share of the chosen position closed per step (at most half)
    "cooldown_seconds": 90,     # between steps in a wallet, so margin data catches up
    "max_actions_per_day": 8,
    "protect": ["07f2:VVV"],    # "wallet last four:coin" never reduced (this VVV short hedges the staked VVV)
}


def margin_config() -> dict:
    """The guard's settings: defaults, overridden by the file (read each time, so edits apply at once)."""
    config = dict(MARGIN_DEFAULTS)
    try:
        config.update(json.loads(MARGIN_CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    return config


def margin_plan(positions, equity: float, margin_used: float, wallet: str, config: dict) -> dict:
    """What to do about a wallet's margin: ``used`` (margin used / equity) and, when it is in danger, the step.

    The step reduces the most in-the-money position (largest unrealized profit in USD) that is not
    protected, by ``chop_fraction`` or what reaches ``target_used`` if that is more, at most half of it.
    """
    used = margin_used / equity if equity > 0 else float("inf")
    plan: dict = {"used": used, "action": None, "blocked": ""}
    if used < config["act_used"]:
        return plan
    protected = {str(item).lower() for item in config.get("protect", [])}
    candidates = [position for position in positions
                  if position.quantity and f"{wallet[-4:]}:{position.coin}".lower() not in protected]
    if not candidates:
        plan["blocked"] = "every open position is protected"
        return plan
    best = max(candidates, key=lambda position: position.quantity * (position.mark_price - position.entry_price))
    needed = (margin_used - config["target_used"] * equity) / best.margin_used if best.margin_used > 0 else 1.0
    fraction = min(0.5, max(config["chop_fraction"], needed))
    step = 10 ** -best.market.size_decimals
    quantity = int(abs(best.quantity) * fraction / step + 1e-9) * step
    quantity = round(quantity if quantity > 0 else abs(best.quantity), best.market.size_decimals)
    plan["action"] = {"coin": best.coin, "dex": best.dex, "quantity": quantity, "fraction": fraction,
                      "unrealized": best.quantity * (best.mark_price - best.entry_price),
                      "side": "SHORT" if best.quantity < 0 else "LONG"}
    return plan


class MarginGuard:
    """Watches every trading wallet's margin; in danger, closes slices of the most profitable position."""

    def __init__(self, daemon: "Daemon"):
        self.daemon = daemon
        self.last_action: dict[str, float] = {}
        self.last_warning: dict[str, float] = {}
        self.actions_today: dict[str, int] = {}
        self.day = time.strftime("%Y-%m-%d")
        self.latest: dict[str, float] = {}

    def run(self) -> None:
        while not self.daemon.stopped.wait(10):
            try:
                self.step(time.time())
            except Exception as exc:  # the guard never stops watching
                log(f"margin guard failed ({type(exc).__name__}: {exc}); retrying")

    def step(self, now: float) -> None:
        config = margin_config()
        if time.strftime("%Y-%m-%d") != self.day:
            self.day, self.actions_today = time.strftime("%Y-%m-%d"), {}
        for wallet in self.daemon.state.wallets:
            if now - self.daemon.state.state_time.get(wallet, 0.0) > 2 * STATE_STALE_SECONDS:
                continue  # no fresh data: never act on a stale picture
            snapshot = self.daemon.state.snapshot([wallet])
            plan = margin_plan(snapshot.positions, snapshot.equity, snapshot.margin_used, wallet, config)
            self.latest[wallet] = plan["used"]
            tag = f"...{wallet[-4:]}"
            if plan["used"] >= config["warn_used"] and now - self.last_warning.get(wallet, 0.0) >= 600:
                self.last_warning[wallet] = now
                log(f"margin: {tag} uses {plan['used']:.0%} of its equity (reserve is 20%, so up to 80%).")
            if plan["action"] is None:
                if plan["blocked"] and now - self.last_warning.get(wallet + "!", 0.0) >= 600:
                    self.last_warning[wallet + "!"] = now
                    log(f"margin: {tag} is at {plan['used']:.0%} but {plan['blocked']}; nothing to reduce.")
                continue
            action = plan["action"]
            what = (f"reduce {action['coin']} {action['side']} by {action['quantity']:g} "
                    f"({action['fraction']:.0%}, unrealized ${action['unrealized']:+,.2f}; margin {plan['used']:.0%} "
                    f"of equity, act at {config['act_used']:.0%})")
            if now - self.last_action.get(wallet, 0.0) < config["cooldown_seconds"]:
                continue
            if not config["enabled"]:
                if now - self.last_warning.get(wallet + "?", 0.0) >= 300:
                    self.last_warning[wallet + "?"] = now
                    log(f"margin guard (dry run, not enabled) would {tag}: {what}")
                continue
            if self.actions_today.get(wallet, 0) >= config["max_actions_per_day"]:
                log(f"margin guard: {tag} reached its {config['max_actions_per_day']} steps today; not reducing: {what}")
                continue
            exchange = self.daemon.exchanges.get(wallet)
            if exchange is None:
                log(f"margin guard: {tag} is read-only in the daemon, cannot {what}")
                continue
            self.last_action[wallet] = now
            self.actions_today[wallet] = self.actions_today.get(wallet, 0) + 1
            log(f"margin guard {tag}: {what}")
            try:
                response = exchange.market_close(action["coin"], action["quantity"])
                log(f"margin guard {tag}: {action['coin']} {response.get('status', response)}")
            except Exception as exc:
                log(f"margin guard {tag}: the reduction failed ({type(exc).__name__}: {exc}); will retry")


class Session:
    """One client connection: text out, typed answers in."""

    def __init__(self, connection: socket.socket, reader, writer):
        self.connection, self.reader, self.writer = connection, reader, writer

    def send(self, message: dict) -> None:
        self.writer.write((json.dumps(message, default=str) + "\n").encode())
        self.writer.flush()

    def out(self, text: str) -> None:
        self.send({"t": "out", "text": daemon_link.mask(text)})

    def data(self, payload: Any) -> None:
        self.send({"t": "data", "data": payload})

    def ask(self, prompt: str) -> str:
        self.send({"t": "ask", "text": prompt})
        self.connection.settimeout(PROMPT_TIMEOUT_SECONDS)
        try:
            line = self.reader.readline()
        except (OSError, socket.timeout):
            return ""
        finally:
            self.connection.settimeout(None)
        try:
            return str(json.loads(line).get("text", "")).strip() if line else ""
        except ValueError:
            return ""


class Daemon:
    def __init__(self, wallets: list[str], paper: bool = False, guards_path: Path | None = GUARDS_FILE):
        self.state = HLState(wallets)
        self.paper = paper
        self.exchanges: dict[str, Any] = {}
        self.guards = Guards(self.state, guards_path)
        self.trade_lock = threading.Lock()
        self.margin_guard = MarginGuard(self)
        self.streams: list[Stream] = []
        self.market_stream: Stream | None = None
        self.stopped = threading.Event()
        self.server: socketserver.BaseServer | None = None
        self.started = time.time()
        self.state.watch = self.watch

    # -- startup
    def watch(self, coin: str) -> None:
        if self.market_stream is not None:
            self.market_stream.add({"type": "activeAssetCtx", "coin": coin})

    def load(self) -> None:
        began = time.time()
        self.state.load_markets()
        self.state.load_wallets(self.state.wallets)
        try:
            self.state.refresh_other()
        except Exception as exc:
            log(f"other desk accounts could not be read ({exc}); retrying every minute")
        held = sorted(self.state.held_coins())
        ht._gather([lambda coin=coin: _prefetch_history(coin) for coin in held])
        log(f"loaded {len(self.state.dexes)} DEXes, {len(self.state.wallets)} wallets and {len(held)} held markets "
            f"in {time.time() - began:.1f}s")

    def load_keys(self) -> None:
        """One signing client per wallet whose API key is in the Keychain (built in parallel)."""
        def build(wallet: str) -> tuple[str, Any]:
            if self.paper:
                return wallet, PaperExchange(wallet)
            try:
                return wallet, SerialExchange(ht.build_exchange(wallet, ht._load_key_from_keychain(wallet),
                                                                self.state.dexes))
            except Exception as exc:
                log(f"wallet ...{wallet[-4:]} is read-only in the daemon: {exc}")
                return wallet, None

        for wallet, exchange in ht._gather([lambda wallet=wallet: build(wallet) for wallet in self.state.wallets]):
            if exchange is not None:
                self.exchanges[wallet] = exchange

    def start_streams(self) -> None:
        self.market_stream = Stream("markets", [{"type": "allDexsAssetCtxs"}], self.state.on_market)
        for coin in sorted(self.state.held_coins()):
            self.market_stream.subscriptions.append({"type": "activeAssetCtx", "coin": coin})
        self.streams = [self.market_stream]
        for wallet in self.state.wallets:
            self.streams.append(Stream(
                f"...{wallet[-4:]}",
                [{"type": "allDexsClearinghouseState", "user": wallet}, {"type": "spotState", "user": wallet},
                 {"type": "orderUpdates", "user": wallet}, {"type": "userFills", "user": wallet}],
                lambda channel, data, wallet=wallet: self.state.on_wallet(wallet, channel, data),
            ))
        for stream in self.streams:
            stream.start()

    def resume_guards(self) -> None:
        for wallet, trade in self.guards.load():
            exchange = self.exchanges.get(wallet)
            if exchange is None or self.paper:
                log(f"{trade.plan.coin}: saved guard not resumed (no signing client for ...{wallet[-4:]})")
                continue
            self.guards.add(wallet, trade, exchange)
            log(f"{trade.plan.coin}: guard resumed for ...{wallet[-4:]} ({trade.plan.side} {trade.quantity:g}).")

    def refresher(self) -> None:
        last_other = last_slow = time.time()
        while not self.stopped.wait(5):
            now = time.time()
            try:
                if self.state.meta_stale:
                    self.state.load_markets()
                stale = [wallet for wallet in self.state.wallets
                         if now - self.state.state_time.get(wallet, 0.0) > 2 * STATE_STALE_SECONDS]
                if stale:
                    self.state.load_wallets(stale, slow=False)
                if now - last_other >= OTHER_VENUES_EVERY_SECONDS:
                    last_other = now
                    self.state.refresh_other()
                if now - last_slow >= SLOW_REFRESH_EVERY_SECONDS:
                    last_slow = now
                    self.state.load_markets()
                    self.state.load_wallets(self.state.wallets)
            except Exception as exc:
                log(f"background refresh failed ({type(exc).__name__}: {exc}); retrying")

    # -- requests
    def wallet(self, text: str) -> str:
        text = text.strip().lower()
        matches = [wallet for wallet in self.state.wallets if wallet == text or (len(text) >= 4 and wallet.endswith(text))]
        if len(matches) != 1:
            raise ValueError(f"wallet {text!r} is not one of the daemon's wallets "
                             f"({', '.join('...' + wallet[-4:] for wallet in self.state.wallets)})")
        return matches[0]

    def dispatch(self, request: dict, session: Session) -> int:
        command = request.get("cmd")
        if command == "ping":
            session.data({"ok": True})
            return 0
        if command == "status":
            session.data(self.status())
            return 0
        if command == "book":
            book = self.book(request.get("wallets") or [])
            if book is None:
                return 1
            session.data(book)
            return 0
        if command in ("trade", "reattach"):
            if not self.trade_lock.acquire(timeout=PROMPT_TIMEOUT_SECONDS):
                print("Another trade is waiting for confirmation in the daemon; try again shortly.")
                return 2
            try:
                return self.trade(request, session) if command == "trade" else self.reattach(request, session)
            finally:
                self.trade_lock.release()
        if command == "stop":
            return self.stop_request(session)
        print(f"Unknown command {command!r}.")
        return 2

    def book(self, wallets: list[str]) -> dict | None:
        wallets = [wallet.lower() for wallet in wallets]
        if not wallets or any(wallet not in self.state.wallets for wallet in wallets):
            return None
        markets = self.state.markets()
        snapshot = self.state.snapshot(wallets, markets)
        _, risk = ht.plan_trades([], markets, snapshot, include_external=False)
        age = max(time.time() - self.state.state_time.get(wallet, 0.0) for wallet in wallets)
        return {"equity": snapshot.equity, "risk": risk, "margin_used": snapshot.margin_used,
                "positions": len(snapshot.positions), "age": age}

    def status(self) -> dict:
        now = time.time()
        other = self.state.other
        guards = []
        for entry in list(self.guards.entries.values()):
            plan, trade = entry.trade.plan, entry.trade
            mark = self.state.mark(plan.coin, max_age=60)
            guards.append({"wallet": entry.wallet[-4:], "coin": plan.coin, "side": plan.side, "quantity": trade.quantity,
                           "entry": trade.entry_price, "mark": mark, "loss_limit": entry.budget(),
                           "exiting": trade.exiting})
        return {
            "paper": self.paper, "uptime_s": now - self.started,
            "wallets": [{"wallet": wallet[-4:], "trading": wallet in self.exchanges,
                         "state_age_s": now - self.state.state_time.get(wallet, 0.0)} for wallet in self.state.wallets],
            "streams": [{"name": stream.name, "connected": stream.connected, "age_s": stream.age()}
                        for stream in self.streams],
            "other_venues_age_s": now - other[1] if other else None,
            "kill_switch": ht.kill_switch_engaged(),
            "guards": guards,
            "margin_guard": {"enabled": margin_config()["enabled"],
                             "used": {wallet[-4:]: used for wallet, used in self.margin_guard.latest.items()}},
        }

    def _other_book(self, ignore_unreachable: bool, live: bool):
        other = self.state.other
        if other is None or time.time() - other[1] > 3 * OTHER_VENUES_EVERY_SECONDS:
            self.state.refresh_other()
            other = self.state.other
        book, stamp = other
        from desk.risk import print_book

        print_book(book, f"Other desk accounts (read {time.time() - stamp:.0f}s ago)")
        if book.unreachable and live and not ignore_unreachable:
            raise RuntimeError("a configured desk account could not be read, so its risk is unknown. Fix the "
                               "connection, or rerun with --ignore-unreachable to trade without it.")
        if book.unreachable:
            print("WARNING: unreachable accounts are left out of desk equity and risk.")
        return book

    def _plan(self, request: dict, wallet: str, risk_wallets: list[str]):
        """Plan from live data: (kind, plans, markets, snapshot)."""
        live = bool(request.get("live"))
        if request.get("signals"):
            signals = ht.read_signals(Path(request["signals"]))
            trading = [ht.normalize_market(item["coin"], item.get("dex", "")) for item in signals]
            markets = self.state.markets(trading)
            snapshot = self.state.snapshot(risk_wallets, markets)
            book = self._other_book(bool(request.get("ignore_unreachable")), live)
            snapshot = dataclasses.replace(snapshot, external_equity=book.equity_usd, external_risk=book.risk_usd)
            snapshot = ht.urgent_fees(snapshot, bool(request.get("urgent")))
            plans, risk = ht.plan_trades(signals, markets, snapshot)
            ht.print_plan(snapshot, plans, risk)
            return "signals", plans, markets, snapshot
        actions = ht.read_position_actions(Path(request["actions"]))
        markets = self.state.markets([(action.coin, action.dex) for action in actions])
        snapshot = self.state.snapshot(risk_wallets, markets)
        book = self._other_book(bool(request.get("ignore_unreachable")), live)
        snapshot = dataclasses.replace(snapshot, external_equity=book.equity_usd, external_risk=book.risk_usd)
        orders = ht.fetch_open_orders(wallet, sorted({action.dex for action in actions}))
        plans = ht.plan_position_actions(actions, markets, snapshot, orders)
        ht.print_reconcile_plans(plans)
        return "actions", plans, markets, snapshot

    def trade(self, request: dict, session: Session) -> int:
        began = time.time()
        if bool(request.get("signals")) == bool(request.get("actions")):
            print("Give either --signals or --actions.")
            return 2
        wallet = self.wallet(request.get("wallet") or "")
        risk_wallets = list(dict.fromkeys(
            [wallet] + [self.wallet(item) for item in (request.get("risk_wallets") or self.state.wallets)]))
        live, urgent, fast = bool(request.get("live")), bool(request.get("urgent")), bool(request.get("fast"))
        slippage = float(request.get("slippage") or ht.URGENT_SLIPPAGE * 100) / 100
        if (urgent or fast) and not request.get("signals"):
            print("--urgent and --fast apply to --signals entries.")
            return 2
        if urgent and fast:
            print("Choose --urgent or --fast, not both.")
            return 2
        if self.paper and request.get("signals") and not urgent and live:
            print("Paper mode simulates urgent entries only; add --urgent.")
            return 2
        cap = request.get("max_risk_pct")
        saved_cap = ht.MAX_TOTAL_RISK_PCT
        try:
            if cap is not None:
                if not 0 < float(cap) <= ht.MAX_RISK_PCT_OVERRIDE_LIMIT:
                    print(f"--max-risk-pct must be above 0 and at most {ht.MAX_RISK_PCT_OVERRIDE_LIMIT:g}.")
                    return 2
                ht.MAX_TOTAL_RISK_PCT = float(cap) / 100
                print(f"NOTE: aggregate risk cap for this run is {float(cap):g}%, not the default "
                      f"{ht.DEFAULT_MAX_TOTAL_RISK_PCT * 100:g}%.")
            if self.paper:
                print("[paper] Live data, simulated exchange: nothing is sent to HL.")
            if urgent:
                print(f"Urgent entry: immediate-or-cancel at most {slippage * 100:g}% from the mid (taker fee).")
            kind, plans, markets, snapshot = self._plan(request, wallet, risk_wallets)
            print(f"(planned in {time.time() - began:.2f}s from live data)")
            if not live:
                print("Dry run only. No order was sent.")
                return 0
            if ht.kill_switch_engaged():
                print("Kill switch is engaged; no order was sent. Use --clear-kill-switch to trade again.")
                return 2
            if wallet not in self.exchanges:
                print(f"Wallet ...{wallet[-4:]} has no signing key in this daemon; no order was sent.")
                return 2
            submitable = (any(plan.quantity > 0 for plan in plans) if kind == "signals"
                          else ht.has_submitable_reconciliation(plans))
            if not submitable:
                print("No live order can be sent: every proposed quantity is zero or blocked.")
                return 2
            first = ("Type LIVE TRADING to submit these mainnet orders: " if kind == "signals"
                     else "Type LIVE TRADING to review the position actions: ")
            if session.ask(first) != "LIVE TRADING":
                print("Confirmation did not match; no order was sent.")
                return 0
            refreshed = time.time()
            kind, plans, markets, snapshot = self._plan(request, wallet, risk_wallets)
            print(f"Refreshed preflight immediately before live submission ({time.time() - refreshed:.2f}s).")
            if kind == "signals" and not any(plan.quantity > 0 for plan in plans):
                print("The refreshed risk/margin check blocks every order; no order was sent.")
                return 2
            if kind == "actions" and not ht.has_submitable_reconciliation(plans):
                print("The refreshed check blocks every action; no order was sent.")
                return 2
            phrase = "SUBMIT ORDERS" if kind == "signals" else "SUBMIT POSITION ACTIONS"
            prompt = ("Type SUBMIT ORDERS to send the refreshed plan: " if kind == "signals"
                      else "Type SUBMIT POSITION ACTIONS to execute the refreshed plan: ")
            if session.ask(prompt) != phrase:
                print("Confirmation did not match; no order was sent.")
                return 0
            confirmed = time.time()
            exchange = self.exchanges[wallet]
            if kind == "signals":
                active, failures = ht.execute_plans(exchange, wallet, plans, markets, urgent=urgent,
                                                    slippage=slippage, batched_exits=True, fast=fast)
                print(f"Orders sent and protected {time.time() - confirmed:.2f}s after confirmation.")
                for trade in active:
                    if self.paper:
                        print(f"[paper] {trade.plan.coin}: a guard would start here.")
                    else:
                        self.guards.add(wallet, trade, exchange)
                        print(f"{trade.plan.coin}: the net-loss guard runs in the daemon "
                              f"(loss limit ${trade.plan.risk_budget:,.2f}).")
                if failures:
                    print("Some live orders did not complete successfully: " + "; ".join(failures))
                    return 2
                return 0
            try:
                ht.run_position_reconciliation(wallet, None, self.state.dexes, plans, markets, exchange=exchange)
            finally:
                print(f"Position actions done {time.time() - confirmed:.2f}s after confirmation.")
                self._refresh_guards(wallet, plans)
            return 0
        finally:
            ht.MAX_TOTAL_RISK_PCT = saved_cap

    def _refresh_guards(self, wallet: str, plans: list) -> None:
        """After a resize or close, rebuild a guarded coin's guard from its new stop and take-profit."""
        coins = {plan.action.coin for plan in plans if not plan.block_reason}
        keys = [key for key in self.guards.entries if key[0] == wallet and key[1] in coins]
        if not keys or self.paper:
            return
        for key in keys:
            with self.guards.lock:
                self.guards.entries.pop(key, None)
        markets = self.state.markets()
        rebuilt = ht.plan_guard_reattachment(wallet, self.state.dexes, markets, {key[1].upper() for key in keys})
        for item in rebuilt:
            if item.trade is not None:
                self.guards.add(wallet, item.trade, self.exchanges[wallet])
                print(f"{item.coin}: guard rebuilt for the new size ({item.note}).")
        self.guards.save()

    def reattach(self, request: dict, session: Session) -> int:
        wallet = self.wallet(request.get("wallet") or "")
        coins = {coin.strip().upper() for coin in (request.get("coins") or []) if coin.strip()} or None
        markets = self.state.markets()
        plans = ht.plan_guard_reattachment(wallet, self.state.dexes, markets, coins)
        ht.print_guard_reattachment(plans)
        if not request.get("live"):
            print("Dry run only. No guard was started.")
            return 0
        if wallet not in self.exchanges or self.paper:
            print(f"Wallet ...{wallet[-4:]} has no signing client in this daemon; no guard was started.")
            return 2
        if not any(item.trade for item in plans):
            print("No position can be guarded.")
            return 2
        if session.ask("Type LIVE TRADING to review the guards: ") != "LIVE TRADING":
            print("Confirmation did not match; no guard was started.")
            return 0
        plans = ht.plan_guard_reattachment(wallet, self.state.dexes, self.state.markets(), coins)
        print("Refreshed check immediately before starting:")
        ht.print_guard_reattachment(plans)
        if session.ask("Type START GUARDS to start them (they can close positions): ") != "START GUARDS":
            print("Confirmation did not match; no guard was started.")
            return 0
        for item in plans:
            if item.trade is not None:
                self.guards.add(wallet, item.trade, self.exchanges[wallet])
                print(f"{item.coin}: guard running in the daemon. Stop any executor process still guarding it.")
        return 0

    def stop_request(self, session: Session) -> int:
        if self.guards.entries:
            coins = ", ".join(sorted(entry.coin for entry in self.guards.entries.values()))
            print(f"Guards running for {coins}. Their exchange stops stay on HL; the guards resume when the "
                  "daemon starts again.")
            if session.ask("Type STOP DAEMON to stop it: ") != "STOP DAEMON":
                print("Confirmation did not match; the daemon keeps running.")
                return 0
        print("Stopping the daemon.")
        threading.Thread(target=self.shutdown, daemon=True).start()
        return 0

    def shutdown(self) -> None:
        self.stopped.set()
        self.guards.stopped.set()
        for stream in self.streams:
            stream.stop()
        if self.server is not None:
            self.server.shutdown()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        daemon: Daemon = self.server.daemon  # type: ignore[attr-defined]
        session = Session(self.request, self.rfile, self.wfile)
        code = 2
        try:
            line = self.rfile.readline()
            if not line:
                return
            request = json.loads(line)
            assert OUTPUT is not None
            with OUTPUT.bind(session.out):
                try:
                    code = daemon.dispatch(request, session)
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    print(f"Trade execution stopped: {exc}")
                    code = 2
                except Exception as exc:
                    print(f"Trade execution stopped: {type(exc).__name__}: {exc}")
                    log(traceback.format_exc())
                    code = 2
            session.send({"t": "end", "code": code})
        except (OSError, ValueError):
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(args: argparse.Namespace) -> int:
    global _LOG, OUTPUT
    run_dir = daemon_link.RUN_DIR
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    if daemon_link.query({"cmd": "ping"}, timeout=1.0) is not None:
        print("A trading daemon is already running.", file=sys.stderr)
        return 2
    load_trading()
    daemon_link.SOCKET_PATH.unlink(missing_ok=True)
    _LOG = _Tee(sys.__stdout__, LOG_FILE)
    OUTPUT = _Router(_LOG)
    sys.stdout = OUTPUT
    _memoize_market_history()

    from desk.accounts import accounts_for, load_accounts

    wallets = ([item.strip() for item in args.wallets.split(",") if item.strip()] if args.wallets
               else [account.id for account in accounts_for("hl", load_accounts())])
    for wallet in wallets:
        ht._validate_address(wallet, "wallet address")
    daemon = Daemon(wallets, paper=args.paper, guards_path=None if args.paper else GUARDS_FILE)
    log(f"starting{' in PAPER mode' if args.paper else ''} for "
        + ", ".join("..." + wallet[-4:] for wallet in daemon.state.wallets))
    daemon.load()
    daemon.load_keys()
    daemon.start_streams()
    if not args.no_resume:
        daemon.resume_guards()
    threading.Thread(target=daemon.guards.run, name="guards", daemon=True).start()
    threading.Thread(target=daemon.refresher, name="refresher", daemon=True).start()
    threading.Thread(target=daemon.margin_guard.run, name="margin-guard", daemon=True).start()

    server = _Server(str(daemon_link.SOCKET_PATH), _Handler)
    os.chmod(daemon_link.SOCKET_PATH, 0o600)
    server.daemon = daemon  # type: ignore[attr-defined]
    daemon.server = server

    def stop(*_):
        threading.Thread(target=daemon.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    trading = [wallet[-4:] for wallet in daemon.exchanges]
    log(f"ready in {time.time() - daemon.started:.1f}s; trading wallets: "
        + (", ".join("..." + item for item in trading) or "none (read-only)")
        + f"; socket {daemon_link.SOCKET_PATH}")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        daemon_link.SOCKET_PATH.unlink(missing_ok=True)
        if daemon.guards.entries:
            log("stopped; guards saved for " + ", ".join(sorted(entry.coin for entry in daemon.guards.entries.values()))
                + " (resumed on the next start; their exchange stops stay on HL)")
        else:
            log("stopped")
    return 0


# ----------------------------------------------------------------------------------------------
# Client side

def call(request: dict) -> int:
    """Send a request, print what the daemon says, and answer its prompts from stdin."""
    sock = daemon_link.connect(timeout=None)
    if sock is None:
        print("No trading daemon is running. Start one with: .venv/bin/python execution/hl_daemon.py serve",
              file=sys.stderr)
        return 3
    with sock, sock.makefile("r", encoding="utf-8") as reader:
        daemon_link.send(sock, request)
        for line in reader:
            message = json.loads(line)
            kind = message.get("t")
            if kind == "out":
                sys.stdout.write(message["text"])
                sys.stdout.flush()
            elif kind == "ask":
                sys.stdout.write(message["text"])
                sys.stdout.flush()
                answer = sys.stdin.readline()
                if not sys.stdin.isatty():
                    sys.stdout.write("\n")
                daemon_link.send(sock, {"t": "answer", "text": answer.strip()})
            elif kind == "data":
                print_data(request.get("cmd"), message.get("data"))
            elif kind == "end":
                return int(message.get("code", 2))
    print("The daemon closed the connection without finishing.", file=sys.stderr)
    return 2


def print_data(command: str | None, data: Any) -> None:
    if command != "status":
        print(json.dumps(data, indent=1, default=str))
        return
    print(f"Trading daemon{' (PAPER)' if data['paper'] else ''}, up {data['uptime_s'] / 60:.0f} min; "
          f"kill switch {'ENGAGED' if data['kill_switch'] else 'off'}")
    for wallet in data["wallets"]:
        print(f"  wallet ...{wallet['wallet']}: {'trading' if wallet['trading'] else 'read-only'}, "
              f"positions {wallet['state_age_s']:.0f}s old")
    for stream in data["streams"]:
        print(f"  websocket {stream['name']}: {'connected' if stream['connected'] else 'DOWN'}, "
              f"last message {stream['age_s']:.0f}s ago")
    if data["other_venues_age_s"] is not None:
        print(f"  other desk accounts read {data['other_venues_age_s']:.0f}s ago")
    if not data["guards"]:
        print("  no guards running")
    for guard in data["guards"]:
        mark = f"${guard['mark']:,.6g}" if guard["mark"] else "n/a"
        print(f"  guard ...{guard['wallet']} {guard['coin']} {guard['side']} {guard['quantity']:g} "
              f"@ ${guard['entry']:,.6g}, mark {mark}, loss limit ${guard['loss_limit']:,.2f}"
              + (" — EXITING" if guard["exiting"] else ""))
    margin = data.get("margin_guard")
    if margin:
        used = ", ".join(f"...{wallet} {share:.0%}" for wallet, share in margin["used"].items()) or "not read yet"
        print(f"  margin guard {'ON' if margin['enabled'] else 'dry run (logs only)'}: margin used / equity {used}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("serve", help="start the daemon")
    start.add_argument("--wallets", help="comma-separated HL wallets (default: every HL account in accounts.json)")
    start.add_argument("--paper", action="store_true", help="live data, simulated exchange: nothing is sent")
    start.add_argument("--no-resume", action="store_true", help="do not resume guards saved by the last run")
    trade = commands.add_parser("trade", help="plan and (with --live) send orders through the daemon")
    trade.add_argument("--wallet", required=True, help="execution wallet: address or its last 4+ characters")
    trade.add_argument("--risk-wallets", help="comma-separated wallets counted in risk (default: all daemon wallets)")
    trade.add_argument("--signals", type=Path, help="CSV with coin,side,dex columns")
    trade.add_argument("--actions", type=Path, help="CSV with action,coin,side,dex,margin_usd columns")
    trade.add_argument("--urgent", action="store_true",
                       help="enter at once: immediate-or-cancel at most --slippage from the mid (taker fee)")
    trade.add_argument("--fast", action="store_true",
                       help="post-only limit re-priced every 2s until fully filled (stops if the price runs away)")
    trade.add_argument("--slippage", type=float, default=URGENT_SLIPPAGE_PCT,
                       help=f"with --urgent: percent beyond the mid the entry may pay (default {URGENT_SLIPPAGE_PCT:g})")
    trade.add_argument("--max-risk-pct", type=float, help="aggregate risk cap for this run only, in percent")
    trade.add_argument("--ignore-unreachable", action="store_true",
                       help="trade even if another desk account (such as IB) cannot be read")
    trade.add_argument("--live", action="store_true", help="send orders after the typed confirmations")
    commands.add_parser("status", help="wallets, data age and guards")
    reattach = commands.add_parser("reattach", help="move guards for open positions into the daemon")
    reattach.add_argument("--wallet", required=True)
    reattach.add_argument("--coins", help="comma-separated markets (default: every position with a stop)")
    reattach.add_argument("--live", action="store_true")
    commands.add_parser("stop", help="stop the daemon (guards resume on the next start)")
    args = parser.parse_args(argv)

    if args.command == "serve":
        return serve(args)
    if args.command == "trade":
        if not 0 < args.slippage <= 5:
            parser.error("--slippage must be above 0 and at most 5 (percent).")
        if args.urgent and args.fast:
            parser.error("choose --urgent or --fast, not both.")
        request = {"cmd": "trade", "wallet": args.wallet, "live": args.live, "urgent": args.urgent, "fast": args.fast,
                   "slippage": args.slippage, "max_risk_pct": args.max_risk_pct,
                   "ignore_unreachable": args.ignore_unreachable,
                   "risk_wallets": [item.strip() for item in args.risk_wallets.split(",")] if args.risk_wallets else None,
                   "signals": str(args.signals.resolve()) if args.signals else None,
                   "actions": str(args.actions.resolve()) if args.actions else None}
        return call(request)
    if args.command == "reattach":
        return call({"cmd": "reattach", "wallet": args.wallet, "live": args.live,
                     "coins": [item for item in (args.coins or "").split(",") if item.strip()]})
    return call({"cmd": args.command})


if __name__ == "__main__":
    from desk.privacy import mask_output

    mask_output()  # only the last four characters of any wallet or account number are shown
    raise SystemExit(main())
