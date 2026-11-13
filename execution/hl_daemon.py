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
from types import SimpleNamespace
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
        self.send_ib(book)

    def send_ib(self, book) -> None:
        """Send IB, as just read for the risk checks, to the web relay every ten minutes (desk/web_sync.py)."""
        if not book.ib_reads or getattr(book, "ib_from_relay", False) \
                or time.time() - getattr(self, "ib_sent_at", 0.0) < IB_WEB_EVERY_SECONDS:  # the server: IB came from the relay
            return
        self.ib_sent_at = time.time()
        try:
            from desk import web_sync
            from desk.day_change import position_changes

            for entry, snapshot in book.ib_reads:
                try:
                    moves = position_changes([snapshot])
                except Exception:
                    moves = None
                web_sync.push_ib(snapshot, entry, moves)
            if time.time() - getattr(self, "slow_sent_at", 0.0) >= 3600:  # slow-changing readings, hourly
                self.slow_sent_at = time.time()
                web_sync.push_wallets()
                web_sync.push_regime()
        except Exception as exc:  # the web page keeps the last IB it received
            log(f"IB not sent to the web relay ({type(exc).__name__}: {exc})")

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
            "exiting": trade.exiting, "hidden_target": trade.hidden_target}


def trade_from_json(record: dict) -> tuple[str, ht.ActiveTrade]:
    plan = dict(record["plan"])
    plan["expected_pnl"] = ExpectedPnl(**plan["expected_pnl"])
    trade = ht.ActiveTrade(
        plan=ht.PlannedTrade(**plan), quantity=float(record["quantity"]), entry_price=float(record["entry_price"]),
        baseline_quantity=float(record["baseline_quantity"]), stop_oid=int(record["stop_oid"]),
        target_oid=int(record["target_oid"]), started_at=pd.Timestamp(record["started_at"]),
        prior_funding_pnl=float(record.get("prior_funding_pnl", 0.0)), exiting=bool(record.get("exiting", False)),
        hidden_target=float(record.get("hidden_target", 0.0)),
    )
    return record["wallet"], trade


# Take-profits are held in the daemon's memory and go on HL's book only once the price comes within
# TARGET_PLACE_WITHIN of them (as a share of the trade's stop distance); they are pulled again past
# TARGET_CANCEL_BEYOND, so a price wobbling at the edge does not place and cancel over and over.
# Stops stay on HL at all times: they are trigger orders (not on the public book) and they are what
# protects a position while the daemon is down.
TARGET_PLACE_WITHIN = 0.10
TARGET_CANCEL_BEYOND = 0.20


def target_action(mark: float, target: float, stop_distance: float, direction: float, on_book: bool) -> str:
    """``"place"``, ``"pull"`` or ``""`` for a take-profit at ``target`` with the price at ``mark``.

    ``direction`` is the position's (+1 long: the target is above; -1 short: below). A price already
    through the target counts as near, so the take-profit goes on at once.
    """
    if target <= 0 or stop_distance <= 0:
        return ""
    gap = direction * (target - mark)  # how far the price still has to go to reach the target
    if on_book and gap > TARGET_CANCEL_BEYOND * stop_distance:
        return "pull"
    if not on_book and gap <= TARGET_PLACE_WITHIN * stop_distance:
        return "place"
    return ""


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
    target_retry: float = 0.0   # a take-profit HL would not rest is tried again after this

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

    def manage_target(self, entry: GuardEntry, now: float) -> None:
        """Hold the take-profit in memory; put it on HL's book only while the price is near it.

        A take-profit the executor just placed is adopted (its price read from HL) and pulled while the
        price is far from it. The stop is never touched: it stays on HL whatever the daemon does.
        """
        trade, plan = entry.trade, entry.trade.plan
        if trade.exiting:
            return
        if trade.target_oid and not trade.hidden_target:  # resting since the entry: adopt its price
            order = ht._order_status(entry.wallet, trade.target_oid).get("order", {})
            if str(order.get("status", "")).lower() != "open":
                return  # filled or gone: the full check settles it
            trade.hidden_target = float(order["order"]["limitPx"])
            self.save()
        mark = self.state.mark(plan.coin)
        if mark is None or not trade.hidden_target:
            return
        direction = ht.side_to_direction(plan.side)
        distance = float(plan.price_stop_distance or plan.stop_distance or 0.0)
        action = target_action(mark, trade.hidden_target, distance, direction, bool(trade.target_oid))
        if action == "pull":
            oid = trade.target_oid
            entry.exchange.cancel(plan.coin, oid)
            order = ht._order_status(entry.wallet, oid).get("order", {})
            if str(order.get("status", "")).lower() == "filled":
                entry.next_check = 0.0  # it filled first: the full check closes the guard
                return
            trade.target_oid = 0
            self.save()
            log(f"{plan.coin}: take-profit at ${trade.hidden_target:,.6g} back in memory (price ${mark:,.6g}); "
                "it goes on the book again as the price nears it.")
        elif action == "place" and now >= entry.target_retry:
            quantity = self.state.position_quantity(entry.wallet, plan.coin, plan.dex)
            if quantity is None:
                return
            market = self.state.markets([(plan.coin, plan.dex)])[plan.coin]
            step = 10 ** -market.size_decimals
            size = min(trade.quantity, direction * (quantity - trade.baseline_quantity))
            size = round(int(size / step + 1e-9) * step, market.size_decimals)
            if size <= 0:
                return
            response = entry.exchange.order(plan.coin, direction < 0, size, trade.hidden_target,
                                            {"limit": {"tif": "Alo"}}, reduce_only=True)
            try:
                oid = ht._oid_from_status(ht._status_rows(response)[0])
            except ValueError:
                oid = None
            if oid is None:  # the price is already through the target: rest just outside the spread
                oid = ht._resend_passive_target(entry.exchange, plan, size, trade.hidden_target, market.size_decimals)
            if oid:
                trade.target_oid = int(oid)
                self.save()
                log(f"{plan.coin}: take-profit {size:g} at ${trade.hidden_target:,.6g} placed on the book "
                    f"(price ${mark:,.6g} is near it).")
            else:
                entry.target_retry = now + 30

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
                self.manage_target(entry, now)
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
# Take-profits on positions without a guard (conviction trades, the VVV hedge, older positions)

HELD_TARGETS_FILE = daemon_link.RUN_DIR / "held_targets.json"
HELD_SCAN_SECONDS = 60
HELD_DISTANCE_ATR = 3.0   # the reach target_action measures in: 0.3 x ATR to place, 0.6 x ATR to pull


class HeldTargets:
    """Resting reduce-only take-profit limits on unguarded positions, held in memory like the guards' own.

    Every minute the wallets' open orders are read: a reduce-only limit that is not a ladder quote and
    not a guarded coin's is adopted (price and size noted) and pulled while the price is far from it.
    It goes back on the book as the price nears it, sized to what is still open. A take-profit that
    fills or that someone cancels on HL is dropped; so is one whose position closed or flipped. Stops
    (trigger orders) are never touched.
    """

    def __init__(self, daemon: "Daemon", path: Path | None = HELD_TARGETS_FILE):
        self.daemon = daemon
        self.path = path
        self.lock = threading.RLock()
        self.items: list[dict] = []
        self.next_scan = 0.0
        self.atr: dict[tuple[str, str, str], float] = {}

    def save(self) -> None:
        if self.path is None:
            return
        with self.lock:
            self.path.write_text(json.dumps(self.items, indent=1))

    def resume(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            self.items = json.loads(self.path.read_text())
        except ValueError:
            self.items = []
        for item in self.items:
            self.daemon.watch(item["coin"])

    def run(self) -> None:
        while not self.daemon.stopped.wait(2):
            try:
                self.step(time.time())
            except Exception as exc:  # never stops; the stops on HL keep protecting
                log(f"held take-profits: check failed ({type(exc).__name__}: {exc}); retrying")

    def _atr(self, coin: str, dex: str) -> float:
        key = (coin, dex, time.strftime("%Y-%m-%d", time.gmtime()))
        if key not in self.atr:
            self.atr[key] = float(ht.wilder_atr(ht.fetch_daily_candles(coin.split(":")[-1], dex), 14))
        return self.atr[key]

    def scan(self) -> None:
        """Adopt new resting take-profits; drop those filled, cancelled on HL, or whose position is gone."""
        guarded = {(wallet, coin) for wallet, coin in self.daemon.guards.entries}
        for wallet in list(self.daemon.exchanges):
            with self.daemon.state.lock:
                dexes = [dex for dex, state in self.daemon.state.states.get(wallet, {}).items()
                         if state.get("assetPositions")]
            orders = ht.fetch_open_orders(wallet, dexes or [""])
            live = {int(order["oid"]): order for order in orders}
            with self.lock:
                mine = [item for item in self.items if item["wallet"] == wallet]
                known = {item["oid"] for item in mine if item.get("oid")}
                for item in mine:
                    if item.get("oid") and item["oid"] not in live:  # filled, or cancelled by hand on HL
                        state = str(ht._order_status(wallet, item["oid"]).get("order", {}).get("status", "")).lower()
                        self.items.remove(item)
                        log(f"...{wallet[-4:]} {item['coin']}: take-profit at ${item['price']:,.6g} {state or 'gone'}; "
                            "no longer held.")
                    elif item.get("oid"):
                        item["size"] = float(live[item["oid"]]["sz"])  # a partial fill leaves less to take
                for order in orders:
                    coin, oid = order["coin"], int(order["oid"])
                    if (oid in known or order.get("isTrigger") or not order.get("reduceOnly")
                            or str(order.get("cloid") or "").startswith(LADDER_CLOID_PREFIX) or (wallet, coin) in guarded
                            or any(item["wallet"] == wallet and item["coin"] == coin and abs(item["price"] - float(order["limitPx"])) < 1e-12
                                   for item in self.items)):
                        continue
                    dex = coin.split(":")[0] if ":" in coin else ""
                    self.items.append({"wallet": wallet, "coin": coin, "dex": dex, "is_buy": order["side"] == "B",
                                       "price": float(order["limitPx"]), "size": float(order["sz"]), "oid": oid})
                    self.daemon.watch(coin)
                    log(f"...{wallet[-4:]} {coin}: take-profit {order['sz']} at ${float(order['limitPx']):,.6g} "
                        "now held by the daemon (on the book only as the price nears it).")
        self.save()

    def step(self, now: float) -> None:
        if now >= self.next_scan:
            self.next_scan = now + HELD_SCAN_SECONDS
            self.scan()
        for item in list(self.items):
            wallet, coin, dex = item["wallet"], item["coin"], item["dex"]
            exchange = self.daemon.exchanges.get(wallet)
            position = self.daemon.state.position_quantity(wallet, coin, dex)
            mark = self.daemon.state.mark(coin)
            if exchange is None or position is None or mark is None:
                continue
            direction = -1.0 if item["is_buy"] else 1.0  # a buy take-profit closes a short
            if position * direction <= 0:  # closed or flipped: the take-profit has nothing left to take
                if item.get("oid"):
                    exchange.cancel(coin, item["oid"])
                with self.lock:
                    self.items.remove(item)
                self.save()
                log(f"...{wallet[-4:]} {coin}: position closed; its held take-profit is dropped.")
                continue
            reach = HELD_DISTANCE_ATR * self._atr(coin, dex)
            action = target_action(mark, item["price"], reach, direction, bool(item.get("oid")))
            if action == "pull":
                oid = item["oid"]
                exchange.cancel(coin, oid)
                if str(ht._order_status(wallet, oid).get("order", {}).get("status", "")).lower() == "filled":
                    continue  # the next scan drops it
                item["oid"] = None
                self.save()
                log(f"...{wallet[-4:]} {coin}: take-profit at ${item['price']:,.6g} back in memory (price ${mark:,.6g}).")
            elif action == "place" and now >= item.get("retry_after", 0.0):
                market = self.daemon.state.markets([(coin, dex)])[coin]
                step = 10 ** -market.size_decimals
                size = round(int(min(item["size"], abs(position)) / step + 1e-9) * step, market.size_decimals)
                if size <= 0:
                    continue
                response = exchange.order(coin, item["is_buy"], size, item["price"], {"limit": {"tif": "Alo"}},
                                          reduce_only=True)
                try:
                    oid = ht._oid_from_status(ht._status_rows(response)[0])
                except ValueError:
                    oid = None
                if oid is None:  # already through the price: rest just outside the spread
                    plan = SimpleNamespace(coin=coin, dex=dex, side="LONG" if direction > 0 else "SHORT")
                    oid = ht._resend_passive_target(exchange, plan, size, item["price"], market.size_decimals)
                if oid:
                    item["oid"], item["size"] = int(oid), size
                    self.save()
                    log(f"...{wallet[-4:]} {coin}: take-profit {size:g} at ${item['price']:,.6g} placed (price ${mark:,.6g}).")
                else:
                    item["retry_after"] = now + 30


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
# Alerts: what needs the user's attention, for the dashboards (desk/alerts.py)

ALERTS_FILE = daemon_link.RUN_DIR / "daemon_alerts.json"
IB_WEB_EVERY_SECONDS = 600       # the daemon sends IB to the web relay this often
ALERT_PUSH_SECONDS = 60          # at most this often after a change
ALERT_HEARTBEAT_SECONDS = 900    # and at least this often to the web relay
API_KEY_WARN_DAYS = 30           # warn this long before an HL API wallet expires
API_KEY_CHECK_SECONDS = 6 * 3600


class Alerts:
    def __init__(self, path: Path | None = ALERTS_FILE):
        self.path = path
        self.items: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.dirty = True
        self.last_push = 0.0
        self.market_making: list[dict] = []
        self.market_making_at = 0.0
        self.ladders: list[dict] = []

    def refresh_market_making(self, daemon: "Daemon") -> None:
        """The ladders' metrics (desk/market_making.py) for the dashboards, every five minutes."""
        self.ladders = [{"wallet": item["wallet"][-4:], "coin": item["coin"], "quotes": len(item["oids"]),
                         "queued": max(0, len(item.get("grid", [])) - len(item["oids"])),
                         "status": f"quoting, trims paused (trend {item['trend'][0]:.2f})" if item.get("trims_paused")
                         else "quoting"}
                        for item in list(daemon.ladders.ladders.values())]
        if time.time() - self.market_making_at < 300:
            return
        self.market_making_at = time.time()
        from desk import market_making

        frame = market_making.market_making_frame({(item["wallet"], item["coin"]): item["quotes"] for item in self.ladders},
                                                  {(item["wallet"], item["coin"]): item["queued"] for item in self.ladders},
                                                  {(item["wallet"], item["coin"]): item["status"] for item in self.ladders})
        rows = json.loads(frame.to_json(orient="records"))
        if [row.get("Fills") for row in rows] != [row.get("Fills") for row in self.market_making] or len(rows) != len(self.market_making):
            self.dirty = True
        self.market_making = rows

    def set(self, key: str, title: str, text: str, ttl: float = 0.0) -> None:
        with self.lock:
            old = self.items.get(key)
            if old and old["title"] == title and old["text"] == text:
                return
            self.items[key] = {"title": title, "text": daemon_link.mask(text),
                               "since": old["since"] if old else time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                               "expires": time.time() + ttl if ttl else 0}
            self.dirty = True
        if not old:
            log(f"ALERT {title}: {text}")

    def clear(self, key: str) -> None:
        with self.lock:
            if self.items.pop(key, None) is not None:
                self.dirty = True

    def state(self) -> dict:
        now = time.time()
        with self.lock:
            for key in [key for key, item in self.items.items() if item["expires"] and item["expires"] < now]:
                self.items.pop(key)
            return {"heartbeat": now, "as_of": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                    "alerts": list(self.items.values()), "ladders": self.ladders, "market_making": self.market_making}

    def run(self, daemon: "Daemon") -> None:
        while not daemon.stopped.wait(30):
            try:
                self.watch(daemon)
                try:
                    self.refresh_market_making(daemon)
                except Exception as exc:
                    log(f"market-making metrics failed ({type(exc).__name__}: {exc})")
                state = self.state()
                if self.path is not None:
                    self.path.write_text(json.dumps(state, indent=1))
                now = time.time()
                if (self.dirty and now - self.last_push >= ALERT_PUSH_SECONDS) or now - self.last_push >= ALERT_HEARTBEAT_SECONDS:
                    self.last_push = now
                    self.dirty = False
                    try:
                        from desk import web_sync

                        web_sync._post("/ingest/alerts", state)
                    except Exception as exc:  # the web page then shows the daemon as not reporting
                        log(f"alerts not sent to the web relay ({type(exc).__name__}: {exc})")
            except Exception as exc:
                log(f"alerts failed ({type(exc).__name__}: {exc})")

    def watch_keys(self, daemon: "Daemon") -> None:
        """Each wallet's signing key: warn API_KEY_WARN_DAYS before its API wallet expires on HL, and if the
        daemon signs with the main wallet's own key (which, unlike an API wallet, can withdraw). Six-hourly."""
        if daemon.paper or time.time() - getattr(self, "keys_checked_at", 0.0) < API_KEY_CHECK_SECONDS:
            return
        self.keys_checked_at = time.time()
        for wallet, exchange in list(daemon.exchanges.items()):
            signer = str(getattr(getattr(exchange, "wallet", None), "address", "") or "").lower()
            key = f"apikey:{wallet}"
            if not signer:
                continue
            if signer == wallet.lower():
                self.set(key, "main wallet key on the server",
                         f"...{wallet[-4:]} signs with the main wallet's own key, which can withdraw funds. Store the "
                         "API wallet key instead (hl_trade.py --wallet ... --store-key).")
                continue
            agents = post_info({"type": "extraAgents", "user": wallet}) or []
            mine = [agent for agent in agents if str(agent.get("address", "")).lower() == signer]
            if not mine:
                self.set(key, "API wallet not approved",
                         f"...{wallet[-4:]}: HL does not list the API wallet the daemon signs with (...{signer[-4:]}); "
                         "orders will be refused. Approve a new API wallet on HL and store its key.")
                continue
            days = (float(mine[0].get("validUntil") or 0) / 1000 - time.time()) / 86400
            if days <= API_KEY_WARN_DAYS:
                until = time.strftime("%Y-%m-%d", time.gmtime(float(mine[0]["validUntil"]) / 1000))
                self.set(key, "API wallet expiring",
                         f"...{wallet[-4:]}: the API wallet ...{signer[-4:]} expires {until} ({max(days, 0):.0f} days). "
                         "Create a new one on HL (API page) and store its key on the server "
                         f"(hl_trade.py --wallet {wallet[-4:]} --store-key), then restart the daemon.")
            else:
                self.clear(key)

    def watch(self, daemon: "Daemon") -> None:
        """The daemon's own health: stale account data, dead websockets, guards that cannot check."""
        try:
            self.watch_keys(daemon)
        except Exception as exc:  # the key check is extra: the rest of the health check goes on
            log(f"API key check failed ({type(exc).__name__}: {exc})")
        now = time.time()
        for wallet in daemon.state.wallets:
            age = now - daemon.state.state_time.get(wallet, 0.0)
            key = f"stale:{wallet}"
            if age > 120:
                self.set(key, "stale account data", f"...{wallet[-4:]}: no fresh positions for {age:.0f}s; "
                         "the guards and ladders wait for fresh data")
            else:
                self.clear(key)
        down = [stream.name for stream in daemon.streams if not stream.connected]
        if down:
            self.set("streams", "websocket down", "Disconnected: " + ", ".join(down) + "; reconnecting")
        else:
            self.clear("streams")
        read_only = [wallet for wallet in daemon.state.wallets if wallet not in daemon.exchanges] if not daemon.paper else []
        if read_only:
            self.set("read-only", "daemon cannot trade (no signing client)",
                     ", ".join(f"...{wallet[-4:]}" for wallet in read_only) + ": guards, the margin guard and ladders cannot "
                     "act there (the exchange stops stay on HL). Restart the daemon.")
        else:
            self.clear("read-only")
        failing = [entry for entry in list(daemon.guards.entries.values())
                   if entry.failing_since is not None and now - entry.failing_since > 300]
        if failing:
            self.set("guards", "guard checks failing", ", ".join(f"...{entry.wallet[-4:]} {entry.coin}" for entry in failing)
                     + ": the net-loss guard cannot check them (the exchange stops stay active)")
        else:
            self.clear("guards")


# ----------------------------------------------------------------------------------------------
# Margin guard: keep a wallet away from an account-wide liquidation cascade

MARGIN_CONFIG = daemon_link.RUN_DIR / "margin_guard.json"
MARGIN_DEFAULTS = {
    "enabled": False,               # False: only log what it would do
    # On cross margin HL liquidates the account once its collateral falls to the maintenance margin of all
    # its positions together; that is the cascade. A single isolated position liquidating is accepted.
    "act_maintenance": 0.60,        # maintenance margin / collateral: reduce positions from here (1.0 = liquidation)
    "target_maintenance": 0.45,     # reduce until about here
    "chop_fraction": 0.25,          # share of the chosen position closed per step (at most half)
    "cooldown_seconds": 30,         # between steps in a wallet, so the account data catches up
    "warn_used": 0.80,              # initial margin / equity past the 20% reserve: a warning only
    "protect": ["07f2:VVV"],        # "wallet last four:coin" cut only once nothing else is left (the VVV hedge)
}


def margin_config() -> dict:
    """The guard's settings: defaults, overridden by the file (read each time, so edits apply at once)."""
    config = dict(MARGIN_DEFAULTS)
    try:
        config.update(json.loads(MARGIN_CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    return config


def _slice(position, fraction: float) -> float:
    step = 10 ** -position.market.size_decimals
    quantity = int(abs(position.quantity) * fraction / step + 1e-9) * step
    return round(quantity if quantity > 0 else abs(position.quantity), position.market.size_decimals)


def margin_plan(positions, equity: float, margin_used: float, wallet: str, config: dict,
                maintenance: float = 0.0) -> dict:
    """What to do about a wallet: ``used`` (initial margin / equity), ``health`` (cross maintenance margin /
    collateral, where 1.0 starts liquidation) and, when ``health`` reaches ``act_maintenance``, the step.

    The step cuts the most in-the-money position (largest unrealized profit in USD) by ``chop_fraction``, or by
    what brings ``health`` to ``target_maintenance`` if that is more, at most half of it. Protected positions
    are cut only when no other position is left, since a cascade would close them anyway.
    """
    used = margin_used / equity if equity > 0 else float("inf")
    health = maintenance / equity if equity > 0 else float("inf")
    plan: dict = {"used": used, "health": health, "action": None, "blocked": "", "last_resort": False}
    if health < config["act_maintenance"]:
        return plan
    open_positions = [position for position in positions if position.quantity]
    if not open_positions:
        plan["blocked"] = "no open position to reduce"
        return plan
    protected = {str(item).lower() for item in config.get("protect", [])}
    pool = [position for position in open_positions if f"{wallet[-4:]}:{position.coin}".lower() not in protected]
    if not pool:
        pool, plan["last_resort"] = open_positions, True
    best = max(pool, key=lambda position: position.quantity * (position.mark_price - position.entry_price))
    # A position's share of the maintenance margin, from its share of the initial margin.
    share = best.margin_used / margin_used if margin_used > 0 else 1.0
    own = maintenance * share
    needed = (maintenance - config["target_maintenance"] * equity) / own if own > 0 else 1.0
    fraction = min(0.5, max(config["chop_fraction"], needed))
    plan["action"] = {"coin": best.coin, "dex": best.dex, "quantity": _slice(best, fraction), "fraction": fraction,
                      "unrealized": best.quantity * (best.mark_price - best.entry_price),
                      "side": "SHORT" if best.quantity < 0 else "LONG"}
    return plan


class MarginGuard:
    """Watches every trading wallet; near an account-wide liquidation, closes slices of positions."""

    def __init__(self, daemon: "Daemon"):
        self.daemon = daemon
        self.last_action: dict[str, float] = {}
        self.last_warning: dict[str, float] = {}
        self.latest: dict[str, float] = {}
        self.latest_health: dict[str, float] = {}

    def maintenance(self, wallet: str) -> float:
        """The cross maintenance margin of all the wallet's positions, from the live state (all DEXes)."""
        with self.daemon.state.lock:
            states = list(self.daemon.state.states.get(wallet, {}).values())
        total = 0.0
        for state in states:
            try:
                total += float((state or {}).get("crossMaintenanceMarginUsed") or 0)
            except (TypeError, ValueError):
                continue
        return total

    def run(self) -> None:
        while not self.daemon.stopped.wait(5):
            try:
                self.step(time.time())
            except Exception as exc:  # the guard never stops watching
                log(f"margin guard failed ({type(exc).__name__}: {exc}); retrying")

    def step(self, now: float) -> None:
        config = margin_config()
        for wallet in self.daemon.state.wallets:
            if now - self.daemon.state.state_time.get(wallet, 0.0) > 2 * STATE_STALE_SECONDS:
                continue  # no fresh data: never act on a stale picture
            snapshot = self.daemon.state.snapshot([wallet])
            plan = margin_plan(snapshot.positions, snapshot.equity, snapshot.margin_used, wallet, config,
                               self.maintenance(wallet))
            self.latest[wallet], self.latest_health[wallet] = plan["used"], plan["health"]
            tag = f"...{wallet[-4:]}"
            if plan["used"] >= config["warn_used"] and now - self.last_warning.get(wallet, 0.0) >= 600:
                self.last_warning[wallet] = now
                log(f"margin: {tag} uses {plan['used']:.0%} of its equity as margin (reserve is 20%, so up to 80%); "
                    f"{plan['health']:.0%} of the way to liquidation.")
            alerts = self.daemon.alerts
            if plan["action"] is None:
                alerts.clear(f"margin:{wallet}")
                if plan["blocked"] and now - self.last_warning.get(wallet + "!", 0.0) >= 600:
                    self.last_warning[wallet + "!"] = now
                    log(f"margin: {tag} is {plan['health']:.0%} of the way to liquidation but {plan['blocked']}.")
                continue
            action = plan["action"]
            alerts.set(f"margin:{wallet}", "margin guard cutting positions",
                       f"{tag} is {plan['health']:.0%} of the way to liquidation; the guard reduces positions "
                       f"(next: {action['coin']} {action['side']}) until about {config['target_maintenance']:.0%}")
            what = (f"reduce {action['coin']} {action['side']} by {action['quantity']:g} ({action['fraction']:.0%}, "
                    f"unrealized ${action['unrealized']:,.2f}){' as a last resort (protected)' if plan['last_resort'] else ''}; "
                    f"the account is {plan['health']:.0%} of the way to liquidation, act at {config['act_maintenance']:.0%}")
            if now - self.last_action.get(wallet, 0.0) < config["cooldown_seconds"]:
                continue
            if not config["enabled"]:
                if now - self.last_warning.get(wallet + "?", 0.0) >= 300:
                    self.last_warning[wallet + "?"] = now
                    log(f"margin guard (dry run, not enabled) would {tag}: {what}")
                continue
            exchange = self.daemon.exchanges.get(wallet)
            if exchange is None:
                log(f"margin guard: {tag} is read-only in the daemon, cannot {what}")
                continue
            self.last_action[wallet] = now
            log(f"margin guard {tag}: {what}")
            try:
                response = exchange.market_close(action["coin"], action["quantity"])
                log(f"margin guard {tag}: {action['coin']} {response.get('status', response)}")
            except Exception as exc:
                log(f"margin guard {tag}: the reduction failed ({type(exc).__name__}: {exc}); will retry")


# ----------------------------------------------------------------------------------------------
# Regime guard: trim the book's exposed side when a regime change is read with high confidence

REGIME_CONFIG = daemon_link.RUN_DIR / "regime_guard.json"
REGIME_DEFAULTS = {
    "enabled": False,               # False: only log what it would trim
    "trim_fraction": 0.20,          # share of each exposed position closed per trim
    "max_trims_per_episode": 3,     # an episode ends when the regime reading is back to "none"
    "check_seconds": 1800,
}
MAJORS = {"BTC", "ETH"}


def regime_config() -> dict:
    config = dict(REGIME_DEFAULTS)
    try:
        config.update(json.loads(REGIME_CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    return config


def regime_targets(positions, wallet: str, regime: str, protect: list[str]) -> list:
    """The positions a regime change hurts: alt shorts (main-DEX perps other than BTC and ETH) in an alt
    season, equity-perp longs (xyz:) in an equity sell-off; protected positions (the VVV hedge) are left."""
    protected = {str(item).lower() for item in protect}
    chosen = []
    for position in positions:
        if not position.quantity or f"{wallet[-4:]}:{position.coin}".lower() in protected:
            continue
        if regime == "alt_season" and position.quantity < 0 and ":" not in position.coin and position.coin not in MAJORS:
            chosen.append(position)
        elif regime == "equity_selloff" and position.quantity > 0 and position.coin.startswith("xyz:"):
            chosen.append(position)
    return chosen


class RegimeGuard:
    """Reads the market regime (desk/market_regime.py); on "high", trims the exposed positions once a day."""

    def __init__(self, daemon: "Daemon"):
        self.daemon = daemon
        self.trims: dict[str, int] = {}          # regime -> trims in the current episode
        self.last_day: dict[str, str] = {}       # regime -> UTC day of the last trim
        self.latest: dict = {}

    def run(self) -> None:
        while not self.daemon.stopped.wait(60):
            config = regime_config()
            if time.time() - self.latest.get("_checked", 0.0) < config["check_seconds"]:
                continue
            try:
                self.step(config)
            except Exception as exc:  # keep watching
                log(f"regime guard failed ({type(exc).__name__}: {exc}); retrying")
            self.latest["_checked"] = time.time()

    def step(self, config: dict) -> None:
        from desk import market_regime

        state = market_regime.current()
        self.latest.update({key: state[key]["level"] for key in ("alt_season", "equity_selloff")})
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for regime in ("alt_season", "equity_selloff"):
            part = state[regime]
            if part["level"] == "none":
                self.trims[regime] = 0
            if part["level"] != "high":
                continue
            if self.last_day.get(regime) == today or self.trims.get(regime, 0) >= config["max_trims_per_episode"]:
                continue
            protect = margin_config().get("protect", [])
            orders = []
            for wallet in self.daemon.state.wallets:
                snapshot = self.daemon.state.snapshot([wallet])
                for position in regime_targets(snapshot.positions, wallet, regime, protect):
                    orders.append((wallet, position, _slice(position, config["trim_fraction"])))
            label = regime.replace("_", " ")
            if regime == "equity_selloff":
                log("regime: equity sell-off at high confidence; IB stock longs are not traded by the daemon, trim them by hand.")
            if not orders:
                log(f"regime: {label} at high confidence, but no exposed HL position to trim ({part['detail']}).")
                self.last_day[regime] = today
                continue
            plan = ", ".join(f"...{wallet[-4:]} {position.coin} {quantity:g}" for wallet, position, quantity in orders)
            if not config["enabled"]:
                log(f"regime guard (dry run, not enabled) would trim {config['trim_fraction']:.0%} for {label}: {plan} "
                    f"({part['detail']})")
                self.last_day[regime] = today
                continue
            log(f"regime guard: {label} at high confidence ({part['detail']}); trimming {config['trim_fraction']:.0%}: {plan}")
            self.daemon.alerts.set(f"regime:{regime}", f"regime guard trimmed ({label})",
                                   f"Trimmed {config['trim_fraction']:.0%}: {plan}", ttl=24 * 3600)
            for wallet, position, quantity in orders:
                exchange = self.daemon.exchanges.get(wallet)
                if exchange is None:
                    log(f"regime guard: ...{wallet[-4:]} is read-only, cannot trim {position.coin}")
                    continue
                try:
                    response = exchange.market_close(position.coin, quantity)
                    log(f"regime guard ...{wallet[-4:]}: {position.coin} {response.get('status', response)}")
                except Exception as exc:
                    log(f"regime guard ...{wallet[-4:]}: trimming {position.coin} failed ({type(exc).__name__}: {exc})")
            self.last_day[regime] = today
            self.trims[regime] = self.trims.get(regime, 0) + 1


# ----------------------------------------------------------------------------------------------
# Market-making ladders: post-only quotes around a position you opened, leaning with it

LADDERS_FILE = daemon_link.RUN_DIR / "ladders.json"
LADDER_CONFIG = daemon_link.RUN_DIR / "ladder.json"
LADDER_DEFAULTS = {
    "levels": 5,                 # quotes per side
    "spacing_atr": 0.10,         # level spacing as a share of the coin's daily ATR(14)
    "min_spacing_pct": 0.001,    # never closer than 0.1% of the price
    "add_first": 0.5,            # first adding quote this many spacings from the mid (the lean)
    "trim_first": 1.0,           # first trimming quote this many spacings from the mid
    "add_level_pct": 0.05,       # each adding quote: this share of the size you opened
    "trim_level_pct": 0.03,      # each trimming quote
    "band": 0.50,                # the position stays within (1 -/+ band) x the size you opened
    "replace_move": 1.0,         # re-centre once the mid moves this many spacings
    "min_replace_seconds": 20,
    "refresh_seconds": 600,      # re-centre at least this often
    "pause_adds_at_health": 0.50,  # stop adding once the account is this far toward liquidation
    # The full grid of quotes is kept in memory; a quote goes on HL's book only once the price comes within
    # place_within spacings of it, and is pulled again past cancel_beyond, so few orders rest at a time.
    "place_within": 0.5,         # the nearest quote sits 1 spacing from the mid, so none rests until the price is half way to it
    "cancel_beyond": 1.0,
    "max_live_per_side": 1,       # at most this many quotes per side on the book at once: the nearest

    "reject_retry_seconds": 30,   # a post-only quote HL refused (it would have crossed) is tried again after this
    # Trend pause: while the price trends the position's way (efficiency ratio of the last trend_bars
    # candles at or above trend_pause_er, moving in the position's favour), the trims (buying back a short,
    # selling a long) pause, so the ladder does not hand back the trend; the adds keep quoting. 0 turns it off.
    "trend_interval": "1h",
    "trend_bars": 12,
    "trend_pause_er": 0.40,
    "trend_refresh_seconds": 300,
}
LADDER_CLOID_PREFIX = "0x1add"   # marks the ladder's own orders (client order ids)
MIN_ORDER_USD = 10.0


def ladder_config(coin: str | None = None) -> dict:
    """The ladder settings: defaults, then ladder.json, then that file's ``"coins"`` entry for ``coin``.

    A coin's own entry overrides any setting for that coin's ladder only, for example
    ``"coins": {"ETH": {"spacing_atr": 0.10}}`` widens ETH's spacing and leaves the other ladders alone.
    """
    config = dict(LADDER_DEFAULTS)
    try:
        saved = json.loads(LADDER_CONFIG.read_text())
    except (OSError, ValueError):
        saved = {}
    own = saved.pop("coins", None) or {}
    config.update(saved)
    if coin:
        config.update(own.get(coin.split(":")[-1].upper(), {}))
    return config


def efficiency_ratio(closes: list[float]) -> tuple[float, float]:
    """``(efficiency, direction)`` of a run of closes: the net move over the sum of the moves (0 = churn, 1 = a
    straight line), and the net move's sign (+1 up, -1 down, 0 flat or too few closes)."""
    if len(closes) < 2:
        return 0.0, 0.0
    path = sum(abs(later - earlier) for earlier, later in zip(closes, closes[1:]))
    net = closes[-1] - closes[0]
    if not path:
        return 0.0, 0.0
    return abs(net) / path, (1.0 if net > 0 else -1.0 if net < 0 else 0.0)


def trims_paused(efficiency: float, direction: float, base: float, config: dict) -> bool:
    """True while the price trends the position's way strongly enough that trimming would hand back the trend."""
    threshold = float(config.get("trend_pause_er") or 0)
    return bool(threshold) and efficiency >= threshold and direction * base > 0


def ladder_quotes(position: float, base: float, mid: float, best_bid: float, best_ask: float, atr: float,
                  size_decimals: int, config: dict, allow_adds: bool = True, allow_trims: bool = True) -> list[dict]:
    """The ladder's post-only quotes: ``{is_buy, price, size, reduce_only}``.

    ``base`` is the signed size you opened. The adding side (bids under a long, asks over a short)
    starts ``add_first`` spacings from the mid with ``add_level_pct`` of the base per level; the
    trimming side starts ``trim_first`` spacings away with ``trim_level_pct`` (reduce-only). The adds
    stop at (1 + band) x base and the trims at (1 - band) x base, so the position stays in its band.
    ``allow_adds`` / ``allow_trims`` False drop that side (margin pause, trend pause).
    """
    side = 1.0 if base > 0 else -1.0
    spacing = max(config["spacing_atr"] * atr, mid * config["min_spacing_pct"])
    held = side * position                       # on the base side: positive
    room_add = max(0.0, (1 + config["band"]) * abs(base) - held) if allow_adds else 0.0
    room_trim = max(0.0, held - (1 - config["band"]) * abs(base)) if allow_trims else 0.0
    step = 10 ** -size_decimals
    tick = ht._price_tick(mid, size_decimals)
    quotes = []
    for kind, first, share, room in (("add", config["add_first"], config["add_level_pct"], room_add),
                                     ("trim", config["trim_first"], config["trim_level_pct"], room_trim)):
        is_buy = (kind == "add") == (side > 0)
        for level in range(int(config["levels"])):
            size = int(min(share * abs(base), room) / step + 1e-9) * step
            if size <= 0:
                break
            offset = (first + level) * spacing
            price = mid - offset if is_buy else mid + offset
            price = min(price, best_ask - tick) if is_buy else max(price, best_bid + tick)  # never cross: post-only
            if price <= 0 or size * price < MIN_ORDER_USD:
                break
            quotes.append({"is_buy": is_buy, "price": ht._round_price(price, size_decimals),
                           "size": round(size, size_decimals), "reduce_only": kind == "trim", "kind": kind})
            room -= size
    return quotes


def arm_plan(grid: list[dict], mid: float, spacing: float, config: dict, now: float) -> tuple[list[dict], list[dict]]:
    """Which in-memory quotes to place on the book and which to pull: ``(to_place, to_cancel)``.

    A quote is placed once the price is within ``place_within`` spacings of it (and HL has not just refused
    it), and pulled once the price is more than ``cancel_beyond`` spacings away; between the two it stays as
    it is, so a price wobbling near the edge does not place and cancel it over and over. At most
    ``max_live_per_side`` quotes per side rest at once, the nearest to the price, so little shows on the book.
    """
    near, far = config["place_within"] * spacing, config["cancel_beyond"] * spacing
    limit = int(config.get("max_live_per_side", 1))
    distance = lambda quote: abs(quote["price"] - mid)
    to_cancel = [quote for quote in grid if quote.get("oid") and distance(quote) > far]
    to_place = []
    for side in (True, False):
        live = sorted((quote for quote in grid if quote.get("oid") and quote["is_buy"] == side and quote not in to_cancel), key=distance)
        to_cancel += live[limit:]                      # more than the limit on the book (a lowered limit): the farthest go
        live = live[:limit]
        waiting = sorted((quote for quote in grid if not quote.get("oid") and quote["is_buy"] == side and distance(quote) <= near
                          and quote.get("retry_after", 0.0) <= now), key=distance)
        for quote in waiting:
            if len(live) >= limit:
                if distance(quote) < distance(live[-1]):   # a nearer quote takes the place of the farthest one on the book
                    to_cancel.append(live.pop())
                else:
                    break
            to_place.append(quote)
            live.append(quote)
            live.sort(key=distance)
    return to_place, to_cancel


class Ladders:
    """Runs the ladders: re-centres on the mid, keeps the position in its band, survives restarts.

    Each ladder's quotes live in memory (``grid``); only those near the price rest on HL's book."""

    def __init__(self, daemon: "Daemon", path: Path | None = LADDERS_FILE):
        self.daemon = daemon
        self.path = path
        self.lock = threading.RLock()
        self.ladders: dict[tuple[str, str], dict] = {}
        self.backoff_until = 0.0

    # -- bookkeeping
    def save(self) -> None:
        if self.path is None:
            return
        with self.lock:
            records = [{"wallet": item["wallet"], "coin": item["coin"], "dex": item["dex"], "base": item["base"],
                        "started": item["started"]} for item in self.ladders.values()]
        self.path.write_text(json.dumps(records, indent=1))

    def resume(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            records = json.loads(self.path.read_text())
        except ValueError:
            return
        for record in records:
            self._cancel_own(record["wallet"], record["coin"], record["dex"])
            self._add(record["wallet"], record["coin"], record["dex"], record["base"], record["started"])
            log(f"ladder resumed: ...{record['wallet'][-4:]} {record['coin']} around {record['base']:g}")

    def _add(self, wallet: str, coin: str, dex: str, base: float, started: str) -> None:
        with self.lock:
            self.ladders[(wallet, coin)] = {"wallet": wallet, "coin": coin, "dex": dex, "base": base, "started": started,
                                            "oids": [], "grid": [], "center": None, "placed_position": None,
                                            "placed_at": 0.0, "atr": None, "atr_day": "", "fills": 0, "spacing": None,
                                            "trend": (0.0, 0.0), "trend_at": 0.0, "trims_paused": False}
        self.save()

    def _cancel_own(self, wallet: str, coin: str, dex: str) -> None:
        """Cancel every resting order of this coin that the ladder placed (by its client order id)."""
        exchange = self.daemon.exchanges.get(wallet)
        if exchange is None:
            return
        try:
            orders = ht.fetch_open_orders(wallet, [dex])
        except Exception:
            return
        mine = [{"coin": coin, "oid": int(order["oid"])} for order in orders
                if order.get("coin") == coin and str(order.get("cloid") or "").startswith(LADDER_CLOID_PREFIX)]
        if mine:
            exchange.bulk_cancel(mine)

    # -- commands
    def start(self, wallet: str, coin: str, session: "Session") -> int:
        dex = coin.split(":")[0] if ":" in coin else ""
        quantity = self.daemon.state.position_quantity(wallet, coin, dex)
        if not quantity:
            print(f"No {coin} position in ...{wallet[-4:]}: open it first, then start the ladder.")
            return 1
        if wallet not in self.daemon.exchanges:
            print(f"...{wallet[-4:]} is read-only in the daemon (no key).")
            return 1
        quotes, spacing = self.preview(wallet, coin, dex, quantity)
        config = ladder_config(coin)
        side = "LONG" if quantity > 0 else "SHORT"
        print(f"Ladder for ...{wallet[-4:]} {coin} {side} {abs(quantity):g}: {config['levels']} post-only quotes per side, "
              f"{spacing:.6g} apart ({config['spacing_atr']:g} x daily ATR); position kept between "
              f"{(1 - config['band']) * abs(quantity):g} and {(1 + config['band']) * abs(quantity):g}.")
        for quote in quotes:
            print(f"  {'BUY ' if quote['is_buy'] else 'SELL'} {quote['size']:g} @ {quote['price']:.6g} ({quote['kind']})")
        if session.ask("Type START LADDER to quote these live: ") != "START LADDER":
            print("Confirmation did not match; no ladder started.")
            return 0
        self._add(wallet, coin, dex, quantity, time.strftime("%Y-%m-%d %H:%M", time.gmtime()))
        print("Ladder started; the daemon re-centres it as the price moves (hl_daemon.py status shows it).")
        return 0

    def stop(self, wallet: str, coin: str) -> int:
        with self.lock:
            ladder = self.ladders.pop((wallet, coin), None)
        if ladder is None:
            print(f"No ladder runs for ...{wallet[-4:]} {coin}.")
            return 1
        self.save()
        self._cancel_own(wallet, coin, ladder["dex"])
        print(f"Ladder stopped for ...{wallet[-4:]} {coin}; its orders are cancelled, the position stays.")
        return 0

    # -- quoting
    def _atr(self, ladder: dict) -> float:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if ladder["atr"] is None or ladder["atr_day"] != today:
            candles = ht.fetch_daily_candles(ladder["coin"].split(":")[-1], ladder["dex"])
            ladder["atr"], ladder["atr_day"] = float(ht.wilder_atr(candles, 14)), today
        return ladder["atr"]

    def _trend(self, ladder: dict, now: float, config: dict) -> tuple[float, float]:
        """The efficiency ratio and direction of the last ``trend_bars`` closed candles, refreshed every few minutes."""
        if not config.get("trend_pause_er"):
            return 0.0, 0.0
        if now - ladder.get("trend_at", 0.0) >= config["trend_refresh_seconds"]:
            interval, bars = config["trend_interval"], int(config["trend_bars"])
            seconds = {"m": 60, "h": 3600, "d": 86400}[interval[-1]] * int(interval[:-1])
            market_coin, dex = ladder["coin"].split(":")[-1], ladder["dex"]
            request = {"type": "candleSnapshot", "req": {"coin": market_coin, "interval": interval,
                                                         "startTime": int((now - (bars + 3) * seconds) * 1000),
                                                         "endTime": int(now * 1000)}}
            if dex:
                request["dex"] = dex
            candles = sorted(ht.post_info(request) or [], key=lambda candle: int(candle["t"]))
            # bars moves ending now: the forming candle's close is the live price
            ladder["trend"] = efficiency_ratio([float(candle["c"]) for candle in candles][-(bars + 1):])
            ladder["trend_at"] = now
        return ladder["trend"]

    def _book(self, coin: str, dex: str) -> tuple[float, float]:
        request = {"type": "l2Book", "coin": coin}
        if dex:
            request["dex"] = dex
        levels = ht.post_info(request)["levels"]
        return float(levels[0][0]["px"]), float(levels[1][0]["px"])

    def preview(self, wallet: str, coin: str, dex: str, base: float) -> tuple[list[dict], float]:
        market = self.daemon.state.markets([(coin, dex)])[coin]
        bid, ask = self._book(coin, dex)
        atr = float(ht.wilder_atr(ht.fetch_daily_candles(coin.split(":")[-1], dex), 14))
        config = ladder_config(coin)
        mid = (bid + ask) / 2
        spacing = max(config["spacing_atr"] * atr, mid * config["min_spacing_pct"])
        return ladder_quotes(base, base, mid, bid, ask, atr, market.size_decimals, config), spacing

    def _mid(self, coin: str, dex: str) -> float:
        """The live mark from the websocket (a price a second for held coins); the book over REST if it is stale."""
        mark = self.daemon.state.mark(coin)
        if mark:
            return mark
        bid, ask = self._book(coin, dex)
        return (bid + ask) / 2

    def run(self) -> None:
        while not self.daemon.stopped.wait(2):
            if time.time() < self.backoff_until:
                continue
            with self.lock:
                items = list(self.ladders.values())
            for ladder in items:
                try:
                    self.step(ladder, time.time())
                except Exception as exc:
                    text = str(exc)
                    tag = f"...{ladder['wallet'][-4:]} {ladder['coin']}"
                    if "rate limit" in text.lower() or "too many" in text.lower():
                        self.backoff_until = time.time() + 600
                        log(f"ladder: HL rate limit ({text[:120]}); pausing the ladders for 10 minutes")
                        self.daemon.alerts.set("ladders:rate", "ladders paused (HL rate limit)",
                                               "HL limited the account's order actions; the ladders resume in 10 minutes. "
                                               "If this repeats, widen the spacing in ~/.trading-desk/ladder.json.", ttl=900)
                    else:
                        log(f"ladder {tag} failed ({type(exc).__name__}: {exc}); retrying")
                        self.daemon.alerts.set(f"ladder:{ladder['wallet']}:{ladder['coin']}", f"ladder failing ({tag})",
                                               f"{type(exc).__name__}: {str(exc)[:160]}; retrying")
                else:
                    self.daemon.alerts.clear(f"ladder:{ladder['wallet']}:{ladder['coin']}")

    def step(self, ladder: dict, now: float) -> None:
        wallet, coin, dex, base = ladder["wallet"], ladder["coin"], ladder["dex"], ladder["base"]
        position = self.daemon.state.position_quantity(wallet, coin, dex)
        if position is None:
            return  # stale state: wait for fresh data
        if abs(position) < 0.01 * abs(base) or position * base < 0:
            with self.lock:
                self.ladders.pop((wallet, coin), None)
            self.save()
            self._cancel_own(wallet, coin, dex)
            log(f"ladder ...{wallet[-4:]} {coin}: the position is closed or flipped; ladder stopped and its orders cancelled")
            self.daemon.alerts.set(f"ladder-stopped:{wallet}:{coin}", f"ladder stopped (...{wallet[-4:]} {coin})",
                                   "The position was closed or flipped, so the ladder stopped and cancelled its orders.",
                                   ttl=6 * 3600)
            for key in ("band", "adds"):
                self.daemon.alerts.clear(f"ladder-{key}:{wallet}:{coin}")
            return
        config = ladder_config(coin)
        held, low, high = (1 if base > 0 else -1) * position, (1 - config["band"]) * abs(base), (1 + config["band"]) * abs(base)
        band_key = f"ladder-band:{wallet}:{coin}"
        if held > high * 1.02 or held < low * 0.98:
            self.daemon.alerts.set(band_key, f"ladder out of band: rebalance (...{wallet[-4:]} {coin})",
                                   f"Position {position:g} is outside the ladder's band {low:g} to {high:g} (base {base:g}); "
                                   "the ladder only works it back. Rebalance by hand, or stop and restart the ladder at the new size.")
        else:
            self.daemon.alerts.clear(band_key)
        mid = self._mid(coin, dex)
        atr = self._atr(ladder)
        spacing = max(config["spacing_atr"] * atr, mid * config["min_spacing_pct"])
        level_size = config["trim_level_pct"] * abs(base)
        moved = ladder["center"] is None or abs(mid - ladder["center"]) >= config["replace_move"] * spacing
        filled = ladder["placed_position"] is not None and abs(position - ladder["placed_position"]) >= 0.5 * level_size
        stale = now - ladder["placed_at"] >= config["refresh_seconds"]
        # Trend pause; never while the position is over its band (the trims are then the way back in).
        efficiency, direction = self._trend(ladder, now, config)
        paused = trims_paused(efficiency, direction, base, config) and held <= high
        flipped = paused != ladder.get("trims_paused", False)
        if flipped:
            log(f"ladder ...{wallet[-4:]} {coin}: trims {'paused' if paused else 'resumed'} (trend efficiency "
                f"{efficiency:.2f} over {config['trend_bars']} x {config['trend_interval']}, "
                f"{'with' if direction * base > 0 else 'against'} the position)")
        ladder["trims_paused"] = paused
        exchange = self.daemon.exchanges[wallet]
        if (moved or filled or stale or flipped) and (ladder["center"] is None or flipped
                                                      or now - ladder["placed_at"] >= config["min_replace_seconds"]):
            self._rebuild(ladder, position, now, config, atr, filled)
        self._arm(ladder, mid, exchange, config, now)

    def _rebuild(self, ladder: dict, position: float, now: float, config: dict, atr: float, filled: bool) -> None:
        """A fresh grid of quotes around the mid, kept in memory; the quotes resting on the book are pulled first."""
        wallet, coin, dex, base = ladder["wallet"], ladder["coin"], ladder["dex"], ladder["base"]
        allow_trims = not ladder.get("trims_paused", False)
        health = self.daemon.margin_guard.latest_health.get(wallet, 0.0)
        allow_adds = health < config["pause_adds_at_health"]
        if allow_adds:
            self.daemon.alerts.clear(f"ladder-adds:{wallet}:{coin}")
        else:
            self.daemon.alerts.set(f"ladder-adds:{wallet}:{coin}", f"ladder adds paused (...{wallet[-4:]} {coin})",
                                   f"The account is {health:.0%} of the way to liquidation, so the ladder only trims.")
        bid, ask = self._book(coin, dex)  # the true book, so no quote crosses it
        mid = (bid + ask) / 2
        market = self.daemon.state.markets([(coin, dex)])[coin]
        quotes = ladder_quotes(position, base, mid, bid, ask, atr, market.size_decimals, config, allow_adds, allow_trims)
        live = [quote["oid"] for quote in ladder["grid"] if quote.get("oid")] or ladder["oids"]
        if live:
            self.daemon.exchanges[wallet].bulk_cancel([{"coin": coin, "oid": oid} for oid in live])
        if filled:
            ladder["fills"] += 1
        ladder.update(grid=[{**quote, "oid": None} for quote in quotes], oids=[], center=mid, placed_position=position,
                      placed_at=now, spacing=max(config["spacing_atr"] * atr, mid * config["min_spacing_pct"]))
        log(f"ladder ...{wallet[-4:]} {coin}: {len(quotes)} quotes in memory around {mid:.6g} (position {position:g}, "
            f"base {base:g}{'' if allow_adds else ', adds paused: margin'}{'' if allow_trims else ', trims paused: trend'}); "
            "placed as the price nears them")

    def _arm(self, ladder: dict, mid: float, exchange, config: dict, now: float) -> None:
        """Place the in-memory quotes the price has come near, and pull those it has left behind."""
        coin = ladder["coin"]
        to_place, to_cancel = arm_plan(ladder["grid"], mid, ladder["spacing"] or 0.0, config, now)
        if to_cancel:
            exchange.bulk_cancel([{"coin": coin, "oid": quote["oid"]} for quote in to_cancel])
            for quote in to_cancel:
                quote["oid"] = None
        if to_place:
            from hyperliquid.utils.types import Cloid

            requests_ = [{"coin": coin, "is_buy": quote["is_buy"], "sz": quote["size"], "limit_px": quote["price"],
                          "order_type": {"limit": {"tif": "Alo"}}, "reduce_only": quote["reduce_only"],
                          "cloid": Cloid.from_str(LADDER_CLOID_PREFIX + os.urandom(14).hex())} for quote in to_place]
            statuses = ht._status_rows(exchange.bulk_orders(requests_))
            for quote, status in zip(to_place, statuses):
                if isinstance(status, dict) and status.get("resting"):
                    quote["oid"] = int(status["resting"]["oid"])
                elif isinstance(status, dict) and status.get("filled"):
                    quote["oid"], quote["retry_after"] = None, now + config["reject_retry_seconds"]  # the fill re-centres
                else:  # post-only refused (it would have crossed) or another error: try again a little later
                    quote["retry_after"] = now + config["reject_retry_seconds"]
        ladder["oids"] = [quote["oid"] for quote in ladder["grid"] if quote.get("oid")]


# ----------------------------------------------------------------------------------------------
# Research feed: performance by strategy, trades due for review and the screener, hourly

RESEARCH_FILE = Path(__file__).resolve().parents[1] / "data" / "research_state.json"
RESEARCH_EVERY_SECONDS = 3600


class ResearchFeed:
    """Hourly: syncs the journal, reads performance by strategy, flags old directional trades, re-ranks the screener;
    saves it for the local dashboard and notebooks and sends it to the web relay."""

    def __init__(self, daemon: "Daemon", path: Path | None = RESEARCH_FILE):
        self.daemon = daemon
        self.path = path
        self.last = 0.0

    def run(self) -> None:
        while not self.daemon.stopped.wait(60):
            if time.time() - self.last < RESEARCH_EVERY_SECONDS:
                continue
            self.last = time.time()
            try:
                self.step()
            except Exception as exc:  # the dashboards keep the last reading
                log(f"research feed failed ({type(exc).__name__}: {exc})")

    def step(self) -> dict:
        from desk import journal, screener, stress

        state = self.daemon.state
        snapshot = state.snapshot(state.wallets)
        book = state.other[0] if state.other else None
        open_pairs = {(position.wallet[-4:].lower(), position.coin) for position in snapshot.positions}
        journal.close_missing(open_pairs)
        frame = journal.strategy_frame()
        strategies = json.loads(frame.to_json(orient="records")) + [json.loads(pd.Series(journal.total_row(frame)).to_json())]
        overdue = [{"wallet": item["wallet"], "coin": item["coin"], "side": item["side"], "days": round(item["days"], 1)}
                   for item in journal.overdue(journal.load(), open_pairs)]
        exposures = [(("tradfi perp" if position.dex and position.dex != "hyna" else "crypto perp"), position.coin,
                      position.quantity * position.mark_price) for position in snapshot.positions]
        exposures += stress.desk_exposures(book) if book is not None else []
        desk_equity = snapshot.equity + (book.equity_usd if book is not None else 0.0)
        laddered = {item["coin"] for item in list(self.daemon.ladders.ladders.values())}
        free_margin = {}
        for wallet in state.wallets:  # margin above each wallet's 20% reserve, as the executor counts it
            own = state.snapshot([wallet])
            free_margin[wallet[-4:]] = own.target_equity * (1 - ht.RESERVE_PCT) - own.target_margin_used
        try:
            screen = screener.run(desk_equity, exposures, {position.coin for position in snapshot.positions}, laddered, free_margin)
        except Exception as exc:  # no saved candles yet, or HL unreadable: keep the last screen
            log(f"screener not refreshed ({type(exc).__name__}: {exc})")
            screen = screener.load()
        payload = {"as_of": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "strategies": strategies,
                   "overdue": overdue, "screener": screen}
        if self.path is not None:
            self.path.write_text(json.dumps(payload, indent=1))
        try:
            from desk import web_sync

            web_sync._post("/ingest/research", payload)
        except Exception as exc:
            log(f"research not sent to the web relay ({type(exc).__name__}: {exc})")
        return payload


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
        self.alerts = Alerts(None if paper else ALERTS_FILE)
        self.margin_guard = MarginGuard(self)
        self.regime_guard = RegimeGuard(self)
        self.ladders = Ladders(self, None if paper else LADDERS_FILE)
        self.held = HeldTargets(self, None if paper else HELD_TARGETS_FILE)
        self.research = ResearchFeed(self, None if paper else RESEARCH_FILE)
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
            # Building a client downloads HL's market metadata; a rate-limited answer (429) is retried with
            # backoff, so a busy minute does not leave the daemon without its signing clients.
            for attempt in range(6):
                try:
                    return wallet, SerialExchange(ht.build_exchange(wallet, ht._load_key_from_keychain(wallet),
                                                                    self.state.dexes))
                except Exception as exc:
                    if "429" in str(exc)[:20] and attempt < 5:
                        wait = 10 * 2 ** attempt
                        log(f"wallet ...{wallet[-4:]}: HL rate-limited the client setup; retrying in {wait}s")
                        time.sleep(wait)
                        continue
                    log(f"wallet ...{wallet[-4:]} is read-only in the daemon: {str(exc)[:160]}")
                    return wallet, None
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
        if command == "ladder":
            wallet = self.wallet(str(request.get("wallet", "")))
            coin = str(request.get("coin", "")).strip()
            coin = coin if ":" in coin else coin.upper()
            return self.ladders.stop(wallet, coin) if request.get("stop") else self.ladders.start(wallet, coin, session)
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
        stops = ht.resting_stops(wallets, sorted({position.dex for position in snapshot.positions}))
        _, risk = ht.plan_trades([], markets, snapshot, include_external=False, stops=stops)
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
                           "exiting": trade.exiting, "target": trade.hidden_target,
                           "target_on_book": bool(trade.target_oid)})
        return {
            "paper": self.paper, "uptime_s": now - self.started,
            "wallets": [{"wallet": wallet[-4:], "trading": wallet in self.exchanges,
                         "state_age_s": now - self.state.state_time.get(wallet, 0.0)} for wallet in self.state.wallets],
            "streams": [{"name": stream.name, "connected": stream.connected, "age_s": stream.age()}
                        for stream in self.streams],
            "other_venues_age_s": now - other[1] if other else None,
            "kill_switch": ht.kill_switch_engaged(),
            "guards": guards,
            "margin_guard": {"enabled": margin_config()["enabled"], "act": margin_config()["act_maintenance"],
                             "used": {wallet[-4:]: used for wallet, used in self.margin_guard.latest.items()},
                             "health": {wallet[-4:]: share for wallet, share in self.margin_guard.latest_health.items()}},
            "ladders": [{"wallet": item["wallet"][-4:], "coin": item["coin"], "base": item["base"],
                         "quotes": len(item["oids"]), "queued": max(0, len(item.get("grid", [])) - len(item["oids"])),
                         "center": item["center"], "fills": item["fills"]}
                        for item in list(self.ladders.ladders.values())],
            "held_targets": [{"wallet": item["wallet"][-4:], "coin": item["coin"], "side": "buy" if item["is_buy"] else "sell",
                              "size": item["size"], "price": item["price"], "on_book": bool(item.get("oid"))}
                             for item in list(self.held.items)],
            "regime_guard": {"enabled": regime_config()["enabled"],
                             "levels": {key: value for key, value in self.regime_guard.latest.items() if not key.startswith("_")}},
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
            from desk.stress import desk_exposures

            snapshot = dataclasses.replace(snapshot, external_equity=book.equity_usd, external_risk=book.risk_usd,
                                           external_exposures=tuple(desk_exposures(book)))
            snapshot = ht.urgent_fees(snapshot, bool(request.get("urgent")))
            stops = ht.resting_stops(risk_wallets, sorted({position.dex for position in snapshot.positions}))
            plans, risk = ht.plan_trades(signals, markets, snapshot, stops=stops,
                                         conviction=bool(request.get("conviction")),
                                         strategy=request.get("strategy") or "directional",
                                         conviction_pct=float(request.get("conviction_pct") or ht.CONVICTION_NOTIONAL_PCT * 100) / 100)
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
        held_targets = {}
        for key in keys:
            with self.guards.lock:
                old = self.guards.entries.pop(key, None)
            if old is not None and old.trade.hidden_target:
                held_targets[key[1]] = old.trade.hidden_target
        markets = self.state.markets()
        rebuilt = ht.plan_guard_reattachment(wallet, self.state.dexes, markets, {key[1].upper() for key in keys})
        for item in rebuilt:
            if item.trade is not None:
                if not item.trade.target_oid and item.coin in held_targets:  # no take-profit on the book: keep the one in memory
                    item.trade.hidden_target = held_targets[item.coin]
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
    threading.Thread(target=daemon.regime_guard.run, name="regime-guard", daemon=True).start()
    threading.Thread(target=daemon.alerts.run, args=(daemon,), name="alerts", daemon=True).start()
    daemon.ladders.resume()
    threading.Thread(target=daemon.ladders.run, name="ladders", daemon=True).start()
    daemon.held.resume()
    threading.Thread(target=daemon.held.run, name="held-targets", daemon=True).start()
    threading.Thread(target=daemon.research.run, name="research", daemon=True).start()

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
              + (f", take-profit ${guard['target']:,.6g} {'on the book' if guard.get('target_on_book') else 'in memory'}"
                 if guard.get("target") else "")
              + (" — EXITING" if guard["exiting"] else ""))
    for held in data.get("held_targets", []):
        print(f"  take-profit ...{held['wallet']} {held['coin']} {held['side']} {held['size']:g} at ${held['price']:,.6g}, "
              f"{'on the book' if held['on_book'] else 'in memory'}")
    margin = data.get("margin_guard")
    if margin:
        health = ", ".join(f"...{wallet} {share:.0%}" for wallet, share in margin.get("health", {}).items()) or "not read yet"
        print(f"  margin guard {'ON' if margin['enabled'] else 'dry run (logs only)'}: way to liquidation {health} "
              f"(acts at {margin.get('act', 0):.0%})")
    for ladder in data.get("ladders", []):
        center = f"{ladder['center']:.6g}" if ladder["center"] else "not placed yet"
        print(f"  ladder ...{ladder['wallet']} {ladder['coin']} around {ladder['base']:g}: {ladder['quotes']} quotes on the book, "
              f"{ladder.get('queued', 0)} queued in memory, centred at {center}, {ladder['fills']} re-centres on fills")
    regime = data.get("regime_guard")
    if regime:
        levels = ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in regime["levels"].items()) or "not read yet"
        print(f"  regime guard {'ON' if regime['enabled'] else 'dry run (logs only)'}: {levels}")


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
    trade.add_argument("--strategy", choices=("directional", "hedge"), default="directional",
                       help="the journal's tag for the position (--conviction tags it conviction)")
    trade.add_argument("--conviction", action="store_true",
                       help="high-conviction entry: no stop, take-profit or loss guard, sized by --conviction-pct")
    from desk.risk import CONVICTION_NOTIONAL_PCT

    trade.add_argument("--conviction-pct", type=float, default=CONVICTION_NOTIONAL_PCT * 100,
                       help="with --conviction: notional as a percent of the trading wallet's equity")
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
    ladder = commands.add_parser("ladder", help="market-making ladder around a position you opened (or --stop it)")
    ladder.add_argument("--wallet", required=True, help="wallet: address or its last 4+ characters")
    ladder.add_argument("--coin", required=True, help="the position's coin (xyz:NVDA for a HIP-3 market)")
    ladder.add_argument("--stop", action="store_true", help="stop the ladder and cancel its orders")
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
                   "conviction": args.conviction, "conviction_pct": args.conviction_pct, "strategy": args.strategy,
                   "slippage": args.slippage, "max_risk_pct": args.max_risk_pct,
                   "ignore_unreachable": args.ignore_unreachable,
                   "risk_wallets": [item.strip() for item in args.risk_wallets.split(",")] if args.risk_wallets else None,
                   "signals": str(args.signals.resolve()) if args.signals else None,
                   "actions": str(args.actions.resolve()) if args.actions else None}
        return call(request)
    if args.command == "ladder":
        return call({"cmd": "ladder", "wallet": args.wallet, "coin": args.coin, "stop": args.stop})
    if args.command == "reattach":
        return call({"cmd": "reattach", "wallet": args.wallet, "live": args.live,
                     "coins": [item for item in (args.coins or "").split(",") if item.strip()]})
    return call({"cmd": args.command})


if __name__ == "__main__":
    from desk.privacy import mask_output

    mask_output()  # only the last four characters of any wallet or account number are shown
    raise SystemExit(main())
