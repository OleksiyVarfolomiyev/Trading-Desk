"""The desk's two books, crypto and stocks, each analysed on its own: what the desk actually holds and earned.

A single desk beta mixes a short-alt crypto book with a long-stock book, so it describes neither: a short crypto
book against BTC and a long stock book against SPY partly cancel in one regression. Here each book is measured
against its own benchmark (crypto vs BTC on every day, stocks vs SPY between its closes).

Where the money is made, exactly:

* HL, per coin and per UTC day, marked to the daily close: yesterday's position times the price change, plus each
  fill's price against that day's close, less fees, plus funding. Spot tokens the same way from their rebuilt
  balances. Perps on stock, index and commodity markets (HIP-3, and gold on the main DEX) are the stock book;
  everything else on HL is crypto.
* IB: NAV change less deposits (IB Flex), all of it the stock book.
* Wallet tokens on other chains have no history: they are in today's holdings and ex-ante risk, not in returns.

Returns are each book's PnL over the whole desk's value the day before, so the two books add up to the desk (the
reconciliation table checks it), and a beta reads as "the desk moves this much of its value per 1% BTC (or SPY)
through this book".
"""

from __future__ import annotations

from desk.overview import trading_equity as _trading_equity

import math
from typing import Iterable

import numpy as np
import pandas as pd
from IPython.display import display

from desk.charts import show as show_chart
from desk.overview import SYMBOLIC_HOLDINGS
from desk.numbers import pct as _pct, since as _since, usd as _usd

KINDS = {"Crypto": "BTC", "Stocks": "SPY"}
WINDOWS = {"7D": 7, "30D": 30, "90D": 90}
REALIZED_DAYS = 90       # realized statistics over the last 90 days (the regime the desk trades now)
HISTORY_DAYS = 365


# ----------------------------------------------------------------------------------------------------------------
# Which book a holding belongs to
# ----------------------------------------------------------------------------------------------------------------
def hl_kind(kind: str, asset: str) -> str:
    """"Crypto" or "Stocks" for an HL holding (``kind`` "perp" or "spot")."""
    from desk.hl.account_analytics import perp_market_group
    from desk.hl.book_exposure import leg_of

    return "Stocks" if leg_of(kind, asset, perp_market_group) == "TradFi" else "Crypto"


def holding_kind(venue: str, symbol: str) -> str:
    """The book of a ``live_risk.position_values`` row: IB is stocks, HL by market, spot and wallet tokens crypto."""
    from desk.live_risk import holding_book

    return holding_book(venue, symbol)


def position_kind(asset_class: str) -> str:
    """The book of an overview position by its asset class (same rule as ``desk.overview.market_kind``)."""
    return "Crypto" if str(asset_class).startswith("crypto") else "Stocks"


# ----------------------------------------------------------------------------------------------------------------
# HL PnL per coin per day
# ----------------------------------------------------------------------------------------------------------------
def _signed(fill: dict) -> float:
    return float(fill["sz"]) if fill.get("side") == "B" else -float(fill["sz"])


def coin_day_pnl(position_before: float, path: pd.DataFrame, closes: pd.Series, funding: pd.Series,
                 days: pd.DatetimeIndex) -> pd.DataFrame:
    """One market's daily PnL, marked to each day's close.

    ``path``: the market's fills (ts, day, signed, px, fee, after) oldest first; ``position_before`` the position before
    the first of them; ``closes`` daily closes by UTC day; ``funding`` funding received per day (negative: paid).
    Returns columns price (holding through the day), trading (fills against the close), fees (paid: negative), funding and
    held (position at the day's end).
    """
    closes = closes.reindex(days).ffill()
    if len(path):
        held = path.groupby("day")["after"].last().reindex(days).ffill()
        held = held.fillna(position_before)
        held[days < path["day"].iloc[0]] = position_before
    else:
        held = pd.Series(position_before, index=days)
    before = held.shift(1)
    before.iloc[0] = position_before if len(path) and days[0] <= path["day"].iloc[0] else held.iloc[0]
    price = (before * closes.diff()).fillna(0.0)
    if len(path):
        at_close = closes.reindex(path["day"]).to_numpy()
        trade = path["signed"] * (at_close - path["px"])
        trading = trade.groupby(path["day"].to_numpy()).sum().reindex(days).fillna(0.0)
        fees = (-path["fee"]).groupby(path["day"].to_numpy()).sum().reindex(days).fillna(0.0)
        # The direction of each day's first fill: the side of a position opened and closed within the day.
        opened = np.sign(path.groupby(path["day"].to_numpy())["signed"].first()).reindex(days).fillna(0.0)
    else:
        trading = fees = opened = pd.Series(0.0, index=days)
    return pd.DataFrame({"price": price, "trading": trading, "fees": fees,
                         "funding": funding.reindex(days).fillna(0.0), "held": held.fillna(0.0), "opened": opened})


def hl_wallet_pnl(wallet: str, days: pd.DatetimeIndex) -> tuple[pd.DataFrame, list[str]]:
    """Daily PnL of one HL wallet per market: long frame (day, kind, asset, price, trading, funding, held, value)."""
    from desk.hl.account_analytics import fetch_user_fills, fetch_user_funding, post, spot_base_token_names
    from desk.hl.book_exposure import (STABLECOINS, WRAPPED, is_spot_market, spot_balance_path, spot_pairs,
                                       daily_holdings)
    from desk.hl.trade_analytics import fetch_daily_candles

    lookback = int((pd.Timestamp.now(tz="UTC") - days[0]).days) + 5
    notes: list[str] = []
    candles: dict[str, pd.Series | None] = {}

    def closes_for(market: str) -> pd.Series | None:
        if market not in candles:
            try:
                frame = fetch_daily_candles(market, lookback_days=lookback)
                candles[market] = pd.Series(frame["close"].astype(float).to_numpy(),
                                            index=pd.to_datetime(frame["time"], utc=True).dt.normalize())
            except Exception:
                candles[market] = None
        return candles[market]

    fills = fetch_user_fills(wallet)
    funding = pd.DataFrame([{"day": pd.Timestamp(int(e["time"]), unit="ms", tz="UTC").normalize(),
                             "coin": str(e["delta"].get("coin")), "usd": float(e["delta"]["usdc"])}
                            for e in fetch_user_funding(wallet) if (e.get("delta") or {}).get("usdc") is not None],
                           columns=["day", "coin", "usd"])
    rows = []
    perps: dict[str, list[dict]] = {}
    for fill in fills:
        if fill.get("time") is not None and not is_spot_market(str(fill.get("coin", ""))):
            perps.setdefault(fill["coin"], []).append(fill)
    # Markets held with no fill in the window still earn: today's positions seed them.
    state = post({"type": "clearinghouseState", "user": wallet}) or {}
    open_now = {p["position"]["coin"]: float(p["position"]["szi"]) for p in state.get("assetPositions", [])}
    for coin in set(perps) | set(open_now):
        group = perps.get(coin, [])
        path = pd.DataFrame([{"ts": pd.Timestamp(int(f["time"]), unit="ms", tz="UTC"), "signed": _signed(f),
                              "px": float(f["px"]), "fee": float(f.get("fee") or 0.0),
                              "after": float(f.get("startPosition") or 0.0) + _signed(f)} for f in group],
                            columns=["ts", "signed", "px", "fee", "after"])
        path["day"] = path["ts"].dt.normalize()
        first = float(group[0].get("startPosition") or 0.0) if group else open_now.get(coin, 0.0)
        path = path[path["day"] >= days[0]]
        if len(group) and len(path) < len(group):
            # Fills before the window: the position going in is the last one before it.
            earlier = [f for f in group if pd.Timestamp(int(f["time"]), unit="ms", tz="UTC").normalize() < days[0]]
            first = float(earlier[-1].get("startPosition") or 0.0) + _signed(earlier[-1])
        if not len(path) and not first:
            continue
        series = closes_for(coin)
        if series is None:
            notes.append(f"{coin}: no HL price history, left out")
            continue
        paid = funding[funding["coin"] == coin].groupby("day")["usd"].sum()
        frame = coin_day_pnl(first, path.reset_index(drop=True), series, paid, days)
        frame["value"] = frame["held"] * series.reindex(days).ffill()
        frame["kind"], frame["asset"] = "perp", coin
        rows.append(frame.rename_axis("day").reset_index())
    # Spot tokens: balances rebuilt from fills and transfers, marked to the token's perp (or its spot pair).
    spot = post({"type": "spotClearinghouseState", "user": wallet}) or {}
    balances = {b["coin"]: float(b["total"]) for b in spot.get("balances", [])}
    staking = post({"type": "delegatorSummary", "user": wallet}) or {}
    staked = sum(float(staking.get(key) or 0.0) for key in ("delegated", "undelegated", "totalPendingWithdrawal"))
    if staked:
        balances["HYPE"] = balances.get("HYPE", 0.0) + staked
    ledger = post({"type": "userNonFundingLedgerUpdates", "user": wallet, "startTime": 0}) or []
    token_of = spot_base_token_names()
    path = spot_balance_path(wallet, fills, ledger, balances, token_of)
    if len(path):
        held = daily_holdings(path, days)
        pairs = None
        spot_fills = [f for f in fills if f.get("time") is not None and is_spot_market(str(f.get("coin", "")))]
        for (_, token), units in held.items():
            if token in STABLECOINS or not (units.abs() > 0).any():
                continue
            symbol = WRAPPED.get(token, token)
            series = closes_for(symbol)
            if series is None:
                pairs = pairs if pairs is not None else spot_pairs(post({"type": "spotMeta"}) or {})
                series = closes_for(pairs[token]) if token in pairs else None
            if series is None:
                notes.append(f"{token} (spot): no HL price history, left out")
                continue
            close = series.reindex(days).ffill()
            before = units.shift(1).fillna(units.iloc[0])
            trades = [f for f in spot_fills
                      if (f["coin"].split("/")[0] if "/" in f["coin"] else token_of(f["coin"])) == token]
            trade = pd.Series(0.0, index=days)
            fees = pd.Series(0.0, index=days)
            for f in trades:
                day = pd.Timestamp(int(f["time"]), unit="ms", tz="UTC").normalize()
                if day in trade.index and not math.isnan(close.get(day, np.nan)):
                    fee = float(f.get("fee") or 0.0) * (1.0 if f.get("feeToken") in STABLECOINS else float(f["px"]))
                    trade[day] += _signed(f) * (close[day] - float(f["px"]))
                    fees[day] -= fee
            frame = pd.DataFrame({"price": (before * close.diff()).fillna(0.0), "trading": trade, "fees": fees,
                                  "funding": 0.0, "held": units, "value": units * close})
            frame["kind"], frame["asset"] = "spot", token
            rows.append(frame.rename_axis("day").reset_index())
    columns = ["day", "kind", "asset", "price", "trading", "fees", "funding", "held", "value", "opened"]
    out = pd.concat(rows, ignore_index=True).reindex(columns=columns) if rows else pd.DataFrame(columns=columns)
    out["opened"] = out["opened"].fillna(0.0)
    out["wallet"] = wallet
    return out, notes


def ib_statement_fills(folder) -> tuple[pd.DataFrame, pd.Timestamp | None]:
    """Every stock trade in the IB activity statements saved in ``folder`` (``ib_statement_*.csv`` and the older
    ``transactions.csv``), in the saved-fills layout (time UTC, symbol, side, quantity, price, commission), and the
    last day the statements cover. Statement times are US Eastern. Non-USD trades are left out (their prices are in
    another currency); what they made stays in IB other."""
    import re
    from pathlib import Path

    from desk.venues import ib_statement

    rows, covered = [], None
    for path in sorted(Path(folder).glob("ib_statement_*.csv")) + sorted(Path(folder).glob("transactions.csv")):
        try:
            sections = ib_statement.read_sections(path)
        except Exception:
            continue
        end = re.search(r"_(\d{8})\.csv$", path.name)
        for row in sections.get("Trades", []):
            if row.get("DataDiscriminator") != "Order" or row.get("Asset Category") != "Stocks" or row.get("Currency") != "USD":
                continue
            when = pd.Timestamp(row["Date/Time"].replace(",", "")).tz_localize("America/New_York").tz_convert("UTC")
            quantity = float(str(row["Quantity"]).replace(",", ""))
            rows.append({"time": str(when), "symbol": row["Symbol"], "side": "BOT" if quantity > 0 else "SLD",
                         "quantity": abs(quantity), "price": float(str(row["T. Price"]).replace(",", "")),
                         "commission": float(str(row.get("Comm/Fee") or 0).replace(",", "") or 0)})
            day = pd.Timestamp(end.group(1), tz="UTC") + pd.Timedelta(days=1) if end else when.normalize() + pd.Timedelta(days=1)
            covered = day if covered is None or day > covered else covered
    frame = pd.DataFrame(rows, columns=["time", "symbol", "side", "quantity", "price", "commission"])
    return frame.drop_duplicates(), covered


def ib_name_pnl(positions: list, fills: pd.DataFrame, days: pd.DatetimeIndex, closes_for) -> tuple[pd.DataFrame, list[str]]:
    """IB's daily PnL per stock, marked to each day's close like HL's: today's quantities, walked back through the
    saved fills (``trading-desk-data/ib/ib_fills_*.csv``; TWS keeps about a week, so the desk saves them each run).
    Before the first saved fill the position is taken as it was then; what that misses (older trades, dividends,
    fees) is the gap to IB's NAV, shown as its own row. ``closes_for(symbol, ref)`` gives daily closes by UTC day."""
    now = {p.symbol: (float(p.quantity), p.ref) for p in positions}
    fills = fills.copy()
    if len(fills):
        fills["ts"] = pd.to_datetime(fills["time"], utc=True, format="mixed")
        fills["signed"] = np.where(fills["side"].str.upper().str.startswith("B"), 1.0, -1.0) * fills["quantity"].astype(float)
        fills = fills[fills["ts"].dt.normalize() >= days[0]]
    rows, notes = [], []
    for symbol in sorted(set(now) | set(fills["symbol"] if len(fills) else [])):
        quantity, ref = now.get(symbol, (0.0, ""))
        group = fills[fills["symbol"] == symbol].sort_values("ts") if len(fills) else fills
        path = pd.DataFrame({"ts": group["ts"] if len(group) else [], })
        if len(group):
            after = quantity - group["signed"][::-1].cumsum()[::-1].shift(-1, fill_value=0.0)
            path = pd.DataFrame({"ts": group["ts"].to_numpy(), "signed": group["signed"].to_numpy(),
                                 "px": group["price"].astype(float).to_numpy(),
                                 "fee": group["commission"].astype(float).abs().fillna(0.0).to_numpy(),
                                 "after": after.to_numpy()})
            path["day"] = path["ts"].dt.normalize()
            start = float(path["after"].iloc[0] - path["signed"].iloc[0])
        else:
            path = pd.DataFrame(columns=["ts", "signed", "px", "fee", "after", "day"])
            start = quantity
        try:
            series = closes_for(symbol, ref)
        except Exception:
            series = None
        if series is None or not len(series):
            notes.append(f"{symbol}: no daily closes, left in IB other")
            continue
        frame = coin_day_pnl(start, path, series, pd.Series(dtype=float), days)
        frame.loc[frame["held"].abs() < 1e-9, "held"] = 0.0   # a fully sold position, not rounding dust (-4e-14 shares)
        frame["value"] = frame["held"] * series.reindex(days).ffill()
        frame["kind"], frame["asset"] = "stock", symbol
        rows.append(frame.rename_axis("day").reset_index())
    columns = ["day", "kind", "asset", "price", "trading", "fees", "funding", "held", "value", "opened"]
    frame = pd.concat(rows, ignore_index=True).reindex(columns=columns) if rows else pd.DataFrame(columns=columns)
    frame["opened"] = frame["opened"].fillna(0.0)
    return frame, notes


def side_held(frame: pd.DataFrame, keys: list[str]) -> np.ndarray:
    """"Long" or "Short" per market-day: the side held going into the day, or for a day opened from flat the side
    it closed on (a short opened from flat is short, not long)."""
    before = frame.groupby(keys)["held"].shift(1).fillna(0.0)
    # Held going in decides; a day that starts flat takes the side of its first fill (2026-11-10: three INIT shorts
    # and a small long opened at 23:32 is a short day), and only then the side it closed on.
    opened = frame["opened"].fillna(0.0) if "opened" in frame else pd.Series(0.0, index=frame.index)
    basis = before.where(before.abs() > 1e-12, opened.where(opened != 0, frame["held"]))
    return np.where(basis < 0, "Short", "Long")


def ib_daily_pnl(history: pd.DataFrame) -> pd.Series:
    """IB's daily PnL (NAV change less deposits) by UTC day, from ``desk_accounts.ib_history``."""
    frame = history.copy()
    frame["day"] = pd.to_datetime(frame["ts"], utc=True).dt.normalize()
    series = frame.drop_duplicates("day", keep="last").set_index("day")["pnl"].sort_index()
    return series.diff().dropna()


def daily_equity(histories: dict[str, pd.DataFrame], days: pd.DatetimeIndex) -> pd.Series:
    """The desk's trading equity (every HL wallet and IB) at each day's end."""
    total = pd.Series(0.0, index=days)
    for history in histories.values():
        series = history.set_index(pd.to_datetime(history["ts"], utc=True))["accountValue"].sort_index()
        series = series[~series.index.duplicated(keep="last")]
        daily = series.resample("1D").last()
        daily.index = daily.index.normalize()
        total = total.add(daily.reindex(days).ffill().fillna(0.0), fill_value=0.0)
    return total


def window_sum(series: pd.Series, days: int, end: pd.Timestamp | None = None) -> float:
    if series.empty:
        return float("nan")
    end = series.index.max() if end is None else end
    return float(series[series.index > end - pd.Timedelta(days=days)].sum())


# ----------------------------------------------------------------------------------------------------------------
# The book
# ----------------------------------------------------------------------------------------------------------------
class Book:
    """One of the desk's books ("Crypto" or "Stocks"): holdings, exposure, risk, performance and attribution."""

    def __init__(self, kind: str, data_dir: str = "data", history_days: int = HISTORY_DAYS, since: str | None = None):
        """``history_days`` (or ``since``, a date): how far back the per-market rebuild reaches; a year by default.
        The stocks notebook starts at IB's first deposit (its statements go back to January 2025)."""
        if since is not None:
            history_days = (pd.Timestamp.now(tz="UTC").normalize() - pd.Timestamp(since, tz="UTC")).days + 1
        self.history_days = history_days
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {list(KINDS)}")
        from desk.desk_accounts import DeskAccounts

        self.kind = kind
        self.benchmark = KINDS[kind]
        self.desk = DeskAccounts(data_dir=data_dir)
        self.notes: list[str] = []
        self.coins = pd.DataFrame()

    # -- data ------------------------------------------------------------------------------------------------------
    def fetch_data(self):
        from concurrent.futures import ThreadPoolExecutor

        from desk.accounts import accounts_for

        self._memo = {}   # holdings, their returns and open risk: read once per fetch, shared by every section
        end = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1)
        self.days = pd.date_range(end - pd.Timedelta(days=self.history_days - 1), end, freq="D")
        # Each wallet's per-coin rebuild runs in its own thread while the accounts are read here (IB needs this
        # thread's event loop); the desk's regime detection is skipped, the books measure their own active days.
        pool = ThreadPoolExecutor(max_workers=4)
        jobs = [(account, pool.submit(hl_wallet_pnl, account.id, self.days))
                for account in accounts_for("hl", self.desk.accounts)]
        self.desk.fetch_data(detect_regime=False, refresh_history=self._history_stale())
        self.overview = self.desk.overview
        frames = []
        for account, job in jobs:
            if account.name in self.desk.excluded:
                continue
            try:
                frame, notes = job.result()
            except Exception as exc:
                self.notes.append(f"{account.name}: PnL per coin unavailable ({type(exc).__name__}: {exc}).")
                continue
            frame["account"] = account.name
            frames.append(frame)
            self.notes += [f"{account.name}: {note}." for note in notes]
        pool.shutdown(wait=False)
        coins = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["day", "kind", "asset", "price", "trading", "fees", "funding", "held", "value", "opened", "account"])
        coins["venue"] = "hl"
        coins["book"] = [hl_kind(k, a) for k, a in zip(coins["kind"], coins["asset"])]
        coins["pnl"] = coins["price"] + coins["trading"] + coins["fees"] + coins["funding"]
        # The side the market was held on going into the day (a fill-only day counts as the side it ended).
        coins["side"] = side_held(coins, ["account", "kind", "asset"])
        self.all_coins = coins
        self.coins = coins[coins["book"] == self.kind].copy()
        ib = pd.Series(0.0, index=self.days)
        self.ib_pnl = pd.Series(dtype=float)
        for label, history in self.desk.histories.items():
            if label.startswith("IB"):
                daily = ib_daily_pnl(history)
                self.ib_pnl = daily[daily.index >= self.days[0]]
                ib = ib.add(self.ib_pnl.reindex(self.days).fillna(0.0), fill_value=0.0)
        self.ib_daily = ib
        if self.kind == "Stocks":
            self._add_ib_names()
        self.equity = daily_equity(self.desk.histories, self.days)
        # Returns and shares use trading equity (HL wallets and IB): the value whose PnL is recorded. Wallet tokens on
        # other chains are in desk value but have no PnL record, so dividing by them would understate every return.
        self.capital = float(self.equity.iloc[-1]) if len(self.equity) else float("nan")
        live = _trading_equity(self.overview)   # today's shares on today's trading equity (HL and IB)
        if live > 0:
            self.capital = live
        hl_book = self.coins[self.coins["venue"] == "hl"].groupby("day")["pnl"].sum().reindex(self.days).fillna(0.0)
        self.pnl = hl_book + (ib if self.kind == "Stocks" else 0.0)
        before = self.equity.shift(1)
        self.returns = (self.pnl / before).where(before > 0).dropna()
        self.benchmark_closes = self.desk.benchmarks.get(self.benchmark, pd.Series(dtype=float)).dropna()
        for note in self.notes:
            print(f"note: {note}")
        return self

    def _history_stale(self, max_age: float = 1800) -> bool:
        """Whether the saved HL value histories are older than ``max_age`` seconds: a refresh asks HL for four
        windows per wallet (about 11 s), and the desk notebooks share the files, so a recent one is reused."""
        import time

        from desk.accounts import accounts_for
        from desk.desk_accounts import ROOT_DATA
        from desk.privacy import account_file

        for account in accounts_for("hl", self.desk.accounts):
            path = account_file(ROOT_DATA, "hl_portfolio_history", account.id)
            if not path.exists() or time.time() - path.stat().st_mtime > max_age:
                return True
        return False

    def _add_ib_names(self):
        """IB's PnL per stock (``ib_name_pnl``) into the per-market frame; the NAV total stays the book's IB PnL."""
        from desk import live_risk
        from desk.accounts import ib_data_dir
        from desk.privacy import account_file

        for snapshot in self.overview.snapshots:
            if snapshot.venue != "ib":
                continue
            account_id = self.desk.ib_ids.get(snapshot.label, "")
            path = account_file(ib_data_dir(), "ib_fills", account_id) if account_id else None
            fills = pd.read_csv(path) if path is not None and path.exists() else pd.DataFrame()
            # The activity statements hold every trade up to their end; the saved TWS fills carry on after it.
            statement, covered = ib_statement_fills(ib_data_dir())
            if len(statement):
                if len(fills) and covered is not None:
                    fills = fills[pd.to_datetime(fills["time"], utc=True, format="mixed") >= covered]
                fills = pd.concat([statement, fills[statement.columns] if len(fills) else fills], ignore_index=True)
            ib_refs = {}
            try:
                ib_refs = self.desk._ib_closes([p.ref for p in snapshot.positions if p.ref]) or {}
            except Exception:
                ib_refs = {}

            def read(symbol):
                try:
                    return symbol, live_risk.yahoo_stock_closes(symbol, self.history_days + 10)
                except Exception:
                    return symbol, None

            from concurrent.futures import ThreadPoolExecutor

            symbols = sorted({p.symbol for p in snapshot.positions if p.ref not in ib_refs}
                             | set(fills["symbol"] if len(fills) else []))
            with ThreadPoolExecutor(max_workers=6) as pool:   # Yahoo per name, six at a time (it throttles bursts)
                yahoo = dict(pool.map(read, symbols))

            def closes_for(symbol, ref):
                return ib_refs[ref] if ref in ib_refs else yahoo.get(symbol)

            frame, notes = ib_name_pnl(snapshot.positions, fills, self.days, closes_for)
            if frame.empty:
                continue
            frame["wallet"], frame["account"], frame["venue"], frame["book"] = "", snapshot.label, "ib", "Stocks"
            frame["pnl"] = frame["price"] + frame["trading"] + frame["fees"] + frame["funding"]
            frame["side"] = side_held(frame, ["asset"])
            self.coins = pd.concat([self.coins, frame[self.coins.columns]], ignore_index=True)
            self.notes += [f"{snapshot.label}: {note}." for note in notes]

    # -- current book ----------------------------------------------------------------------------------------------
    def positions(self) -> pd.DataFrame:
        frame = self.overview.positions_frame()
        if frame.empty:
            return frame
        # The trading accounts only (HL and IB): wallet tokens are held, not traded, and have their own section.
        frame = frame[(frame["Asset class"].map(position_kind) == self.kind) & (frame["Venue"] != "onchain")].copy()
        return frame

    def _remember(self, key, make):
        memo = self.__dict__.setdefault("_memo", {})
        if key not in memo:
            memo[key] = make()
        return memo[key]

    def holding_returns(self):
        """(holdings, unpriced, returns, missing) for today's holdings over the ex-ante window, read once."""
        from desk import live_risk

        def make():
            holdings, unpriced = self.holdings()
            if holdings.empty:
                return holdings, unpriced, pd.DataFrame(), []
            returns, missing = live_risk.holding_returns(holdings, live_risk.EX_ANTE_DAYS, self.desk._ib_closes)
            return holdings, unpriced, returns, missing

        return self._remember("holding_returns", make)

    def holdings(self) -> tuple[pd.DataFrame, list[str]]:
        """This book's holdings for ex-ante risk (``live_risk.position_values`` rows), and tokens with no price."""
        return self._remember("holdings", self._read_holdings)

    def _read_holdings(self) -> tuple[pd.DataFrame, list[str]]:
        from desk import live_risk
        from desk.accounts import accounts_for

        unpriced: list[str] = []
        spot: list[dict] = []
        if self.kind == "Crypto":
            wallets = [a.id for a in accounts_for("hl", self.desk.accounts) if a.name not in self.desk.excluded]
            spot, unpriced = live_risk.hl_spot_values(wallets)   # HL spot is in a trading account; wallet tokens are not
        holdings = live_risk.position_values(self.overview._trading(), spot)
        holdings = holdings[[holding_kind(v, s) == self.kind for v, s in zip(holdings["venue"], holdings["symbol"])]]
        return holdings.reset_index(drop=True), unpriced

    def exposure(self) -> pd.DataFrame:
        """Each holding's value, weight, and beta to the book's benchmark (60 and 90 days), with its benchmark
        equivalent (value x beta); a net row last. Beta-weighted exposure is what the book is betting on the
        benchmark; the rest is the bet on the names themselves."""
        from desk import live_risk

        holdings, unpriced, returns, missing = self.holding_returns()
        if holdings.empty:
            return pd.DataFrame()
        bench = self.benchmark_closes
        equity = self.capital
        rows = []
        for row in holdings.to_dict("records"):
            record = {"Holding": row["symbol"], "Venue": row["venue"], "Value": row["value"]}
            for days in (60, 90):
                beta = corr = np.nan
                if self.kind == "Crypto" and row["venue"] == "hl" and row["symbol"] == "BTC":
                    beta = corr = 1.0
                elif row["key"] in returns:
                    series = returns[row["key"]].dropna().tail(days)
                    pair = live_risk.paired_returns(series, bench[bench.index >= series.index.min() - pd.Timedelta(days=5)])
                    if len(pair) >= 15 and pair["bench"].var() > 0:
                        beta = float(pair["book"].cov(pair["bench"]) / pair["bench"].var())
                        corr = float(pair["book"].corr(pair["bench"]))
                record[f"Beta {days}d"] = beta
                if days == 90:
                    record["Correlation 90d"] = corr
                record[f"{self.benchmark}-eq {days}d"] = row["value"] * beta
            rows.append(record)
        table = pd.DataFrame(rows).sort_values("Value", key=lambda v: v.abs(), ascending=False)
        # Wallet and spot tokens under the dashboards' dust cut are one row: they count in the net, not in the eye.
        from desk.overview import WALLET_MIN_SHARE

        small = table["Venue"].isin(["wallet", "hl-spot"]) & (table["Value"].abs() < WALLET_MIN_SHARE * equity)
        if small.sum() > 1:
            dust = table[small]
            row = {"Holding": f"{len(dust)} small tokens", "Venue": "wallet", "Value": dust["Value"].sum()}
            for days in (60, 90):
                row[f"{self.benchmark}-eq {days}d"] = dust[f"{self.benchmark}-eq {days}d"].sum(min_count=1)
            table = pd.concat([table[~small], pd.DataFrame([row])], ignore_index=True)
        gross = table["Value"].abs().sum()
        table.insert(3, "Weight", table["Value"].abs() / gross if gross else np.nan)
        total = {"Holding": "Net", "Venue": "", "Value": table["Value"].sum(), "Weight": np.nan,   # no 100% total
                 f"{self.benchmark}-eq 60d": table[f"{self.benchmark}-eq 60d"].sum(min_count=1),
                 f"{self.benchmark}-eq 90d": table[f"{self.benchmark}-eq 90d"].sum(min_count=1)}
        table = pd.concat([table, pd.DataFrame([total])], ignore_index=True)
        table["/ equity"] = table["Value"] / equity if equity else np.nan
        self.not_priced = sorted(set(missing + unpriced))
        return table

    def summary(self) -> dict:
        """The book's long, short, net and gross exposure, beta-weighted exposure and open risk, against equity."""
        positions = self.positions()
        equity = self.capital
        value = positions["Value"] if len(positions) else pd.Series(dtype=float)
        out = {"Long": value.clip(lower=0).sum(), "Short": value.clip(upper=0).sum()}
        out["Net"], out["Gross"] = out["Long"] + out["Short"], out["Long"] - out["Short"]
        for key in ("Long", "Short", "Net", "Gross"):
            out[f"{key} / equity"] = out[key] / equity if equity else np.nan
        out["uPnL"] = positions["uPnL"].sum() if len(positions) else 0.0
        risk, _, cap = self.open_risk()
        out["Open risk"], out["Open risk / cap"] = risk, risk / cap if cap else np.nan
        return out

    def open_risk(self) -> tuple[float, pd.DataFrame, float]:
        """This book's open risk per position and the desk cap (read once per fetch; see ``_read_open_risk``)."""
        return self._remember("open_risk", self._read_open_risk)

    def _read_open_risk(self) -> tuple[float, pd.DataFrame, float]:
        """This book's open risk (to each position's stop, else the ATR stop distance, plus costs, as the executors
        count it), per position, and the desk cap it shares with the other book."""
        from desk.accounts import accounts_for, short_id
        from desk.risk import hl_position_risks

        rows = []
        wallets = [a.id for a in accounts_for("hl", self.desk.accounts) if a.name not in self.desk.excluded]
        try:
            for item in hl_position_risks(wallets):
                if hl_kind("perp", item["coin"]) == self.kind:
                    rows.append({"Account": f"HL {short_id(item['wallet'])}", "Symbol": item["coin"],
                                 "Open risk": item["risk"], "Basis": item["basis"]})
        except Exception as exc:
            self.notes.append(f"HL open risk per position unavailable ({type(exc).__name__}: {exc}).")
        if self.kind == "Stocks":
            for entry in self.overview.book:
                if entry.venue == "ib":
                    rows += [{"Account": entry.label, "Symbol": symbol, "Open risk": risk, "Basis": basis}
                             for symbol, risk, basis in entry.position_risks]
        frame = pd.DataFrame(rows, columns=["Account", "Symbol", "Open risk", "Basis"])
        _, _, cap = self.overview.risk_totals()
        self._open_risk = (float(frame["Open risk"].sum()), frame, cap)
        return self._open_risk

    # -- realized --------------------------------------------------------------------------------------------------
    def performance(self) -> pd.DataFrame:
        """PnL and return over 7, 30 and 90 days: the book, its parts, and its benchmark."""
        rows = []
        end = self.days[-1]
        parts = {"HL": self.coins[self.coins["venue"] == "hl"].groupby("day")["pnl"].sum()}
        if self.kind == "Stocks":
            parts["IB"] = self.ib_daily
        for label, days in WINDOWS.items():
            cut = end - pd.Timedelta(days=days)
            window = self.returns[self.returns.index > cut]
            row = {"Window": label, "PnL": window_sum(self.pnl, days),
                   "Return (on equity)": float((1 + window).prod() - 1) if len(window) else np.nan}
            for name, series in parts.items():
                row[f"{name} PnL"] = window_sum(series, days)
            closes = self.benchmark_closes
            if len(closes):
                start = closes[closes.index <= cut]
                row[self.benchmark] = float(closes.iloc[-1] / start.iloc[-1] - 1) if len(start) else np.nan
            # Like with like: the same dollars the book held each day, in the benchmark, also as a return on equity.
            same = self._same_in_benchmark()
            same_window = same[same.index > cut]
            row[f"Same $ in {self.benchmark}"] = float((1 + same_window).prod() - 1) if len(same_window) else np.nan
            rows.append(row)
        return pd.DataFrame(rows)

    def _same_in_benchmark(self) -> pd.Series:
        """Daily return on trading equity of holding the book's exposure (each market's value going into the day)
        in the benchmark instead: what the same dollars, long or short, would have made."""
        def make():
            closes = self.benchmark_closes.copy()
            closes.index = pd.to_datetime(closes.index, utc=True).normalize()
            bench = closes.pct_change(fill_method=None).reindex(self.days).fillna(0.0)
            coins = self.coins.copy()
            coins["before"] = coins.groupby(["account", "kind", "asset"])["value"].shift(1).fillna(0.0)
            exposure = coins.groupby("day")["before"].sum().reindex(self.days).fillna(0.0)
            before = self.equity.shift(1)
            return (exposure * bench / before).where(before > 0).dropna()

        return self._remember("same_in_benchmark", make)

    def realized_risk(self) -> pd.DataFrame:
        """Realized and ex-ante risk with bootstrap ranges (computed once per fetch; see ``_realized_risk``)."""
        return self._remember("realized_risk", self._realized_risk)

    def _realized_risk(self) -> pd.DataFrame:
        """Realized statistics of the book's daily returns over the last 90 days and the last year, with beta and
        alpha against its own benchmark (90% bootstrap range); plus the ex-ante row for today's holdings."""
        from desk import live_risk
        from desk.hl.trade_analytics import bootstrap_btc_regression, bootstrap_var

        equity = self.capital
        rows = []
        try:
            holdings, unpriced, returns, missing = self.holding_returns()
            if len(holdings):
                # On trading equity (HL and IB), the base of the realized rows and of the dashboards' book betas.
                row = live_risk.ex_ante_row(holdings, returns, self.capital,
                                            self.desk.benchmarks[[self.benchmark]], missing + unpriced)
                row["Basis"] = "Today's holdings (ex-ante, 90d)"
                rows.append(row)
        except Exception as exc:
            self.notes.append(f"Ex-ante risk unavailable ({type(exc).__name__}: {exc}).")
        periods = 365
        active = self.returns[self.pnl.reindex(self.returns.index).abs() > 0]
        start = active.index.min() if len(active) else None
        for label, days in (("Last 90 days (realized)", REALIZED_DAYS), ("Last year (realized)", HISTORY_DAYS)):
            returns = self.returns[self.returns.index > self.days[-1] - pd.Timedelta(days=days)]
            if start is not None:
                returns = returns[returns.index >= start]
            if len(returns) < 8:
                continue
            path = (1 + returns).cumprod()
            long_enough = len(returns) >= live_risk.MIN_RATIO_DAYS
            row = {"Basis": label, "Periods": len(returns), "From": _since(returns.index.min()),
                   "To": _since(returns.index.max()), "Return (total)": float(path.iloc[-1] - 1),
                   "Volatility (/yr)": float(returns.std() * math.sqrt(periods)),
                   "Sharpe": (float(returns.mean() / returns.std() * math.sqrt(periods))
                              if long_enough and returns.std() > 0 else np.nan),
                   "Max drawdown": float((path / path.cummax() - 1).min())}
            if len(returns) >= 20:
                for confidence in (0.95, 0.99):
                    row[f"VaR {confidence:.0%}"] = bootstrap_var(returns, confidence)["var"] * equity
            pair = live_risk.paired_returns(returns, self.benchmark_closes)
            if len(pair) >= 8:
                stats = bootstrap_btc_regression(pair["book"], pair["bench"],
                                                 periods_per_year=365 if self.kind == "Crypto" else 252, samples=2000)
                row[f"Beta {self.benchmark}"] = stats["beta"]
                row[f"Beta {self.benchmark} 90% range"] = (f"{stats['beta_low']:.2f} to {stats['beta_high']:.2f}"
                                                           if np.isfinite(stats.get("beta_low", np.nan)) else "n/a")
                row["Correlation"] = float(pair["book"].corr(pair["bench"]))
                if long_enough:
                    row[f"Alpha vs {self.benchmark} (/yr)"] = stats["alpha"] / 100
            rows.append(row)
        return pd.DataFrame(rows)

    def attribution(self, days: int = 30) -> pd.DataFrame:
        """PnL by account and market over the last ``days``: price (holding through each day), trading (fills against
        the day's close, fees in) and funding; with the side held now. For IB, what the per-stock marks do not
        explain against its NAV (trades before the saved fills, dividends, fees) is its own row, so IB ties out."""
        cut = self.days[-1] - pd.Timedelta(days=days)
        window = self.coins[self.coins["day"] > cut]
        if window.empty and not (self.kind == "Stocks" and len(self.ib_pnl)):
            return pd.DataFrame()
        keys = ["account", "asset", "side"]   # a market traded both ways shows a row per side
        table = window.groupby(keys)[["price", "trading", "fees", "funding", "pnl"]].sum()
        table = table.reset_index()
        # The side held now, from today's positions (the daily marks end at yesterday's close).
        positions = self.overview.positions_frame()
        now = {(row.Account, str(row.Symbol).split(":")[-1]): ("Long" if row.Value > 0 else "Short")
               for row in positions.itertuples(index=False)} if len(positions) else {}
        table["Side now"] = [now.get((account, str(asset).split(":")[-1]), "Closed")
                             for account, asset in zip(table["account"], table["asset"])]
        if self.kind == "Stocks" and len(self.ib_pnl):
            ib_names = window[window["venue"] == "ib"]
            for account, group in ib_names.groupby("account") if len(ib_names) else [("IB", ib_names)]:
                other = window_sum(self.ib_pnl, days) - group["pnl"].sum()
                table.loc[len(table)] = {"account": account, "asset": "IB other (older trades, dividends, fees)",
                                         "side": "", "price": np.nan, "trading": np.nan, "fees": np.nan,
                                         "funding": np.nan, "pnl": other, "Side now": ""}
        table = table[table["pnl"].abs() > 0.005].sort_values("pnl")
        table["side"] = table["side"].map({"Short": "▼ Short", "Long": "▲ Long"}).fillna("")
        table = table.rename(columns={"account": "Account", "asset": "Market", "side": "Side", "price": "Price",
                                      "trading": "Fills vs close", "fees": "Fees",
                                      "funding": "Funding", "pnl": "PnL"})
        equity = self.capital
        table["PnL / equity"] = table["PnL"] / equity if equity else np.nan
        return table.reset_index(drop=True)

    def sides(self, days: int = 30) -> pd.DataFrame:
        """Long and short legs' PnL over 7, 30 and 90 days (per market, the side held going into each day; IB other is
        in neither)."""
        rows = []
        for label, n in WINDOWS.items():
            window = self.coins[self.coins["day"] > self.days[-1] - pd.Timedelta(days=n)]
            by = window.groupby("side")["pnl"].sum()
            rows.append({"Window": label, "Long": by.get("Long", 0.0), "Short": by.get("Short", 0.0),
                         "Funding": window["funding"].sum(), "Fills vs close": window["trading"].sum(),
                         "Fees": window["fees"].sum()})
        return pd.DataFrame(rows)

    def reconciliation(self) -> pd.DataFrame:
        """Crypto + stocks against the desk's own PnL (HL value history and IB NAV, less deposits), per window. Neither
        side holds wallet tokens; the residual is what the per-market marks miss: HL spot tokens without a price
        history, close-of-day timing (UTC close against HL's last reading of the day) and gaps in HL's history."""
        rows = []
        hl = self.all_coins.groupby(["day", "book"])["pnl"].sum().unstack().reindex(self.days).fillna(0.0)
        desk_pnl = pd.Series(0.0, index=self.days)
        for label, history in self.desk.histories.items():
            frame = history.copy()
            frame["day"] = pd.to_datetime(frame["ts"], utc=True).dt.normalize()
            series = frame.groupby("day")["pnl"].last().reindex(self.days).ffill()
            desk_pnl = desk_pnl.add(series.diff().fillna(0.0), fill_value=0.0)
        for label, days in WINDOWS.items():
            crypto = window_sum(hl.get("Crypto", pd.Series(0.0, index=self.days)), days)
            stocks = window_sum(hl.get("Stocks", pd.Series(0.0, index=self.days)), days) + window_sum(self.ib_daily, days)
            total = window_sum(desk_pnl, days)
            rows.append({"Window": label, "Crypto": crypto, "Stocks": stocks, "Books": crypto + stocks,
                         "Desk (accounts)": total, "Unexplained": total - crypto - stocks})
        return pd.DataFrame(rows)

    # -- display ---------------------------------------------------------------------------------------------------
    def _table(self, frame: pd.DataFrame, title: str, formats: dict, bold_last: bool = False, key: dict | None = None):
        from desk.hl.account_analytics import display_interactive_table

        def style(row_index, column, value, row):
            if isinstance(value, (int, float)) and np.isfinite(value) and column in signed:
                return "color:#16a34a" if value > 0 else "color:#dc2626" if value < 0 else ""
            return ""

        signed = {c for c, f in formats.items() if f in (_usd, _signed_pct)}
        display_interactive_table(frame, formatters=formats, cell_style=style, bold_last=bold_last, title=title,
                                  key_columns=key or {})

    def show_highlights(self):
        """The few lines that matter: return vs the benchmark, beta now and realized, exposure, risk, movers."""
        from desk.notebook_highlights import show as show_lines

        perf = self.performance().set_index("Window")
        good, attention = [], []
        for window, short in (("7D", "W"), ("30D", "M")):   # the dashboard's period letters
            if window in perf.index:
                r = perf.at[window, "Return (on equity)"]
                same = perf.at[window, f"Same $ in {self.benchmark}"] if f"Same $ in {self.benchmark}" in perf else np.nan
                text = f"{_pct(r)}, {_pct(same)} same $ in {self.benchmark}"
                (good if r > 0 else attention).append((f"Return {short}", text))
        summary = self.summary()
        exposure = self.exposure()
        risk = self.realized_risk()
        today = risk[risk["Basis"].str.startswith("Today")] if len(risk) else risk
        if len(today) and f"Beta {self.benchmark}" in today and pd.notna(today.iloc[0].get(f"Beta {self.benchmark}")):
            eq = exposure.iloc[-1].get(f"{self.benchmark}-eq 90d", np.nan) if len(exposure) else np.nan
            beta = today.iloc[0][f'Beta {self.benchmark}']
            gross = abs(summary["Gross"])
            held = beta * self.capital / gross if gross else np.nan
            attention.append(("Beta now", f"{beta:.2f}, {held:.2f} per $, net {_pct(summary['Net / equity'])}, "
                              f"gross {_pct(summary['Gross / equity'])}"))
        realized = risk[risk["Basis"].str.startswith("Last 90")] if len(risk) else risk
        if len(realized) and f"Beta {self.benchmark}" in realized:
            row = realized.iloc[0]
            attention.append(("Beta realized", f"{row[f'Beta {self.benchmark}']:.2f} over {row['Periods']:.0f} days, "
                              f"{row.get(f'Beta {self.benchmark} 90% range', 'n/a')}"))
        if np.isfinite(summary["Open risk / cap"]):
            attention.append(("Open risk", f"{_pct(summary['Open risk / cap'])} of cap"))
        movers = self.attribution(7)
        movers = movers[(movers["Market"] != "Total") & ~movers["Market"].isin(SYMBOLIC_HOLDINGS)] if len(movers) else movers   # KYIV and SWMR are symbolic
        if len(movers):
            best, worst = movers.iloc[-1], movers.iloc[0]
            if best["PnL"] > 0:
                good.append(("Best W", f"{best['Market']} {_pct(best['PnL / equity'])}"))
            if worst["PnL"] < 0:
                attention.append(("Worst W", f"{worst['Market']} {_pct(worst['PnL / equity'])}"))
        show_lines(good, attention)

    def show_positions(self):
        """Today's positions in this book (the overview's table, this book only)."""
        self.overview.show_positions(kind=self.kind)

    def show_exposure(self):
        table = self.exposure()
        if table.empty:
            print("No holdings in this book.")
            return
        b = self.benchmark
        formats = {"Value": _usd, "Weight": _pct, "Beta 60d": "{:.2f}".format, "Beta 90d": "{:.2f}".format,
                   "Correlation 90d": "{:.2f}".format, f"{b}-eq 60d": _usd, f"{b}-eq 90d": _usd, "/ equity": _signed_pct}
        self._table(table, f"EXPOSURE AND BETA TO {b}", formats, bold_last=True, key={f"{b}-eq 90d": "abs"})
        if getattr(self, "not_priced", None):
            print("No price history (no beta): " + ", ".join(self.not_priced))

    def show_risk(self):
        risk, frame, cap = self.open_risk()
        if frame.empty:
            print("No open risk in this book.")
            return
        frame = frame.sort_values("Open risk", ascending=False)
        frame["Share of cap"] = frame["Open risk"] / cap if cap else np.nan
        total = {"Account": "Total", "Symbol": "", "Open risk": risk, "Basis": "",
                 "Share of cap": risk / cap if cap else np.nan}
        frame = pd.concat([frame, pd.DataFrame([total])], ignore_index=True)
        self._table(frame[["Account", "Symbol", "Open risk", "Share of cap", "Basis"]], "OPEN RISK",
                    {"Open risk": _usd_plain, "Share of cap": _pct}, bold_last=True, key={"Share of cap": "max"})

    def show_performance(self):
        table = self.performance()
        formats = {c: _usd for c in table if "PnL" in c}
        formats.update({"Return (on equity)": _signed_pct, self.benchmark: _signed_pct,
                        f"Same $ in {self.benchmark}": _signed_pct})
        self._table(table, "PERFORMANCE", formats, key={"Return (on equity)": None})
        self.plot_performance()

    def plot_performance(self):
        import plotly.graph_objects as go

        # From the day the book first held 10% of equity (the factor model's and picks chart's rule): before that
        # a few dollars of positions make no book.
        active = active_days(self)
        start = active.min() if len(active) else self.days[0]
        returns = chart_returns(self)
        returns = returns[returns.index >= start]
        if returns.empty:
            return
        path = 100 * ((1 + returns).cumprod() - 1)
        figure = go.Figure(go.Scatter(x=path.index, y=path, name=f"{self.kind} book (on equity)"))
        closes = self.benchmark_closes[self.benchmark_closes.index >= returns.index[0] - pd.Timedelta(days=1)]
        if len(closes):
            figure.add_trace(go.Scatter(x=closes.index, y=100 * (closes / closes.iloc[0] - 1), name=self.benchmark,
                                        line={"color": "#F7931A" if self.benchmark == "BTC" else "#16a34a"}))
        # Each day's return, on its own axis: up days green, down days red, one legend entry each.
        daily = 100 * returns
        for name, color, part in (("Up day", "#16a34a", daily.where(daily >= 0)),
                                  ("Down day", "#dc2626", daily.where(daily < 0))):
            figure.add_trace(go.Bar(x=part.index, y=part, name=name, yaxis="y2", opacity=0.4, marker_color=color))
        figure.update_layout(title=f"{self.kind} book vs {self.benchmark} (%, since {_since(returns.index[0])})",
                             template="plotly_dark", hovermode="x unified", yaxis_ticksuffix="%", barmode="overlay",
                             yaxis2={"overlaying": "y", "side": "right", "ticksuffix": "%", "showgrid": False,
                                     "title": "daily"},
                             legend={"orientation": "h", "y": 1.08})
        show_chart(figure)

    def show_realized_risk(self):
        table = self.realized_risk()
        if table.empty:
            print("Not enough history yet.")
            return
        b = self.benchmark
        formats = {"Return (total)": _signed_pct, "Volatility (/yr)": _pct, "Sharpe": "{:.2f}".format,
                   "Max drawdown": _signed_pct, "VaR 95%": _usd_plain, "VaR 99%": _usd_plain, f"Beta {b}": "{:.2f}".format,
                   "Correlation": "{:.2f}".format, f"Alpha vs {b} (/yr)": _signed_pct, "Coverage": _pct,
                   "Periods": "{:.0f}".format}
        keep = [c for c in ["Basis", "Periods", "From", "To", "Return (total)", "Volatility (/yr)", "Sharpe",
                            "Max drawdown", "VaR 95%", "VaR 99%", f"Beta {b}", f"Beta {b} 90% range", "Correlation",
                            f"Alpha vs {b} (/yr)", "Coverage", "Not priced"] if c in table]
        self._table(table[keep], f"RISK AND BETA TO {b}", {k: v for k, v in formats.items() if k in keep},
                    key={f"Beta {b}": None})

    def show_attribution(self, days: int = 30):
        table = self.attribution(days)
        if table.empty:
            print("No PnL in this window.")
            return
        total = {"Account": "Total", "Market": "", "Side": "", "Price": table["Price"].sum(),
                 "Fills vs close": table["Fills vs close"].sum(), "Fees": table["Fees"].sum(),
                 "Funding": table["Funding"].sum(), "PnL": table["PnL"].sum(), "Side now": "",
                 "PnL / equity": table["PnL / equity"].sum()}
        table = pd.concat([table, pd.DataFrame([total])], ignore_index=True)
        columns = ["Account", "Market", "Side", "PnL", "PnL / equity", "Price", "Fills vs close", "Fees", "Funding", "Side now"]
        if self.kind == "Stocks" and not (table["Funding"].abs() > 0.005).any():
            columns.remove("Funding")
        self._table(table[columns], f"PNL BY MARKET, LAST {days} DAYS",
                    {"PnL": _usd, "PnL / equity": _signed_pct, "Price": _usd, "Fills vs close": _usd, "Fees": _usd,
                     "Funding": _usd},
                    bold_last=True, key={"PnL": "abs"})

    def show_sides(self):
        """The long and short legs' PnL over 7, 30 and 90 days."""
        sides = self.sides()
        self._table(sides, "LONG AND SHORT LEGS", {c: _usd for c in sides if c != "Window"})

    def show_market_making(self):
        """The ladders' table (desk/market_making.py), this book's coins only."""
        from desk.market_making import show_market_making

        show_market_making(keep=lambda coin: bool(coin) and hl_kind("perp", coin) == self.kind, notes=False)

    def show_reconciliation(self):
        table = self.reconciliation()
        self._table(table, "BOOKS TIE OUT TO THE DESK", {c: _usd for c in table if c != "Window"})


def _signed_pct(value) -> str:
    try:
        if value is None or not np.isfinite(value):
            return ""
    except TypeError:
        return ""
    return _pct(value)   # no "+" on positive numbers (desk style)


def _usd_plain(value) -> str:
    try:
        if value is None or not np.isfinite(value):
            return ""
    except TypeError:
        return ""
    return _usd(value)


# ----------------------------------------------------------------------------------------------------------------
# Factor model of the book's actual returns (desk/book_factors.py)
# ----------------------------------------------------------------------------------------------------------------
ACTIVE_GROSS = 0.10   # a day counts for the factor model when the book's gross is at least 10% of trading equity


def active_days(book: "Book", threshold: float = ACTIVE_GROSS) -> pd.DatetimeIndex:
    """Days the book held something meaningful: gross exposure at least ``threshold`` of trading equity, judged on the
    day before (the position that earned the day). Crypto: HL positions marked to the close. Stocks: HL stock perps
    plus IB's account value (IB's per-stock history only reaches back to the first saved fills)."""
    hl = book.coins[book.coins["venue"] == "hl"]
    gross = hl.assign(gross=hl["value"].abs()).groupby("day")["gross"].sum().reindex(book.days).fillna(0.0)
    if book.kind == "Stocks":
        for label, history in book.desk.histories.items():
            if label.startswith("IB"):
                frame = history.assign(day=pd.to_datetime(history["ts"], utc=True).dt.normalize())
                value = frame.groupby("day")["accountValue"].last().reindex(book.days).ffill().fillna(0.0)
                gross = gross + value
    share = (gross / book.equity.where(book.equity > 0)).shift(1)
    return share.index[share >= threshold]


def chart_returns(book: "Book") -> pd.Series:
    """The book's daily returns for its performance line. Stocks skip the days it held under 1% of equity (IB all
    cash, 2026-03-30 to 2026-05-11): a flat stretch is no performance, and the line should read as the stocks held."""
    returns = book.returns
    if book.kind != "Stocks":
        return returns
    gross = book.coins.assign(gross=book.coins["value"].abs()).groupby("day")["gross"].sum().reindex(book.days).fillna(0.0)
    share = (gross / book.equity.where(book.equity > 0)).shift(1).reindex(returns.index)
    return returns[~(share < 0.01)]


def _factor_inputs(book: "Book") -> tuple[pd.Series, pd.DataFrame, int]:
    from desk import book_factors

    days = active_days(book)
    if book.kind == "Crypto":
        factors = book_factors.crypto_factors(book.benchmark_closes)
        return book.returns[book.returns.index.isin(days)], factors, 365
    factors, spy = book_factors.stock_factors()
    returns = book_factors.on_calendar(book.returns, spy)
    return returns[returns.index.isin(days)], factors, 252


def show_factor_model(book: "Book"):
    """The book's returns on the factors it trades, split-sample validation, and rolling betas."""
    import plotly.graph_objects as go

    from desk import book_factors

    returns, factors, periods = _factor_inputs(book)
    sample = pd.concat([returns, factors], axis=1, sort=True).dropna().index
    if len(sample) < book_factors.MIN_DAYS:
        since = f" (active since {_since(sample.min())})" if len(sample) else ""
        print(f"{len(sample)} active days{since}: too few for a factor model, which needs {book_factors.MIN_DAYS}.")
        return
    print(f"{len(sample)} active days, {_since(sample.min())} to {_since(sample.max())} (gross at least "
          f"{ACTIVE_GROSS:.0%} of equity).")
    betas, check, rolling = book_factors.model_tables(returns, factors, periods)
    fmt = {"Beta": lambda v: "" if not np.isfinite(v) else f"{v:.2f}", "t": lambda v: "" if not np.isfinite(v) else f"{v:.1f}"}
    shown = betas.copy()
    shown["Beta"] = [(_pct(v) if name == "Alpha (/yr)" else f"{v:.0f}" if name == "Days" else f"{v:.2f}")
                     if np.isfinite(v) else "" for name, v in shown["Beta"].items()]
    shown["t"] = [f"{v:.1f}" if np.isfinite(v) else "" for v in shown["t"]]
    book._table(shown.reset_index(names="Factor"), "FACTOR MODEL", {}, key={})
    check_fmt = {"Alpha (/yr)": _pct_plain, "t": lambda v: "" if not np.isfinite(v) else f"{v:.1f}",
                 "R²": lambda v: "" if not np.isfinite(v) else f"{v:.2f}", "Days": "{:.0f}".format}
    book._table(check.fillna({"Verdict": ""}), "SPLIT-SAMPLE CHECK", check_fmt, key={"Alpha (/yr)": None})
    if len(rolling):
        figure = go.Figure([go.Scatter(x=rolling.index, y=rolling[c], name=c) for c in rolling.columns])
        figure.update_layout(title=f"Rolling {book_factors.ROLLING_DAYS}-day betas", template="plotly_dark",
                             hovermode="x unified", legend={"orientation": "h", "y": 1.08})
        show_chart(figure)


def _pct_plain(value) -> str:
    try:
        return "" if value is None or not np.isfinite(value) else _pct(value)
    except TypeError:
        return ""


Book.show_factor_model = show_factor_model


# ----------------------------------------------------------------------------------------------------------------
# Picks against trading the benchmark itself
# ----------------------------------------------------------------------------------------------------------------
PICKS_MIN_HELD = 0.01   # a day counts when the leg holds at least 1% of equity


def versus_benchmark(book: "Book") -> pd.DataFrame:
    """What the book's markets earned against the same dollars in its benchmark: each day, the exposure held going in
    (per leg, short and long) times the benchmark's move. The gap is what picking the names added or cost; funding and
    fees stay with the picks. A benchmark that does not trade that day (SPY at weekends) moves 0."""
    from research.factor_model import newey_west_mean

    # HL markets, and IB's stocks rebuilt from the activity statements and saved fills.
    hl = book.coins.copy()
    closes = book.benchmark_closes.copy()
    closes.index = pd.to_datetime(closes.index, utc=True).normalize()
    bench = closes.pct_change(fill_method=None).reindex(book.days)
    hl["before"] = hl.groupby(["account", "kind", "asset"])["value"].shift(1).fillna(0.0)
    # A day opened from flat belongs to the leg it opened (the position at the day's close): what the entry made
    # against the close is part of the picks, and the same dollars in the benchmark earned nothing that day.
    # So the table's PnL ties out to the book's.
    dust = 0.01   # under a cent of exposure is flat
    live = (hl["before"].abs() > dust) | (hl["pnl"] != 0)
    rows = []   # legs by the books' one side rule (side_held): held going in, else the day's first fill
    for leg, mask in (("Short", live & (hl["side"] == "Short")), ("Long", live & (hl["side"] == "Long")), ("Net", live)):
        part = hl[mask]
        actual = part.groupby("day")["pnl"].sum().reindex(book.days).fillna(0.0)
        exposure = part.groupby("day")["before"].sum().reindex(book.days).fillna(0.0)
        same = (exposure * bench).fillna(0.0)
        gap = actual - same
        gross = part.assign(g=part["before"].abs()).groupby("day")["g"].sum().reindex(book.days).fillna(0.0)
        before_equity = book.equity.shift(1).where(lambda e: e > 0)   # no equity yet: no day to judge
        active = gross >= PICKS_MIN_HELD * before_equity
        def row(label, window, **extra):
            # Picks added (%): the gap over the average dollars held on those days, the picks' return less the
            # benchmark's on the money actually invested.
            held = exposure[window & active].abs().mean()
            return {"Leg": leg, "Window": label, "Picks": actual[window].sum(),
                    f"Same $ in {book.benchmark}": same[window].sum(), "Picks added": gap[window].sum(),
                    "Picks added (%)": gap[window].sum() / held if held else np.nan, "Avg held": held,
                    "Days held": int((active & window).sum()), **extra}

        for label, days in WINDOWS.items():
            rows.append(row(label, book.days > book.days[-1] - pd.Timedelta(days=days)))
        share = (gap[active] / book.equity.shift(1)[active]).replace([np.inf, -np.inf], np.nan).dropna()
        _, t, _ = newey_west_mean(share)
        rows.append(row("Active days", pd.Series(True, index=book.days), t=t))
    return pd.DataFrame(rows)


def show_versus_benchmark(book: "Book"):
    table = versus_benchmark(book)
    table = table[table["Days held"] > 0]
    if table.empty:
        print("No HL positions in this book yet.")
        return
    money = {c: _usd for c in table if c in ("Picks", f"Same $ in {book.benchmark}", "Picks added", "Avg held")}
    columns = ["Leg", "Window", "Picks added (%)", "Picks added", "Picks", f"Same $ in {book.benchmark}", "Avg held",
               "Days held", "t"]
    book._table(table.reindex(columns=columns), f"PICKS VS {book.benchmark}",
                {**money, "Picks added (%)": _signed_pct, "Days held": "{:.0f}".format,
                 "t": lambda v: "" if not np.isfinite(v) else f"{v:.1f}"},
                key={"Picks added (%)": "abs"})


Book.versus_benchmark = versus_benchmark
Book.show_versus_benchmark = show_versus_benchmark


def plot_picks(book: "Book"):
    """Cumulative return per dollar at work against the same dollars in the benchmark, and the gap between them
    (picks added), from the day the book first held 10% of equity; days a position was opened from flat count."""
    import plotly.graph_objects as go

    closes = book.benchmark_closes.copy()
    closes.index = pd.to_datetime(closes.index, utc=True).normalize()
    bench = closes.pct_change(fill_method=None).reindex(book.days).fillna(0.0)
    coins = book.coins.copy()
    coins["before"] = coins.groupby(["account", "kind", "asset"])["value"].shift(1).fillna(0.0)
    # The dollars at work each day: what was held going in, and for a position opened from flat, its size at the
    # close (its entry against the close is part of the picks; the benchmark earned nothing on it that day).
    fresh = coins["before"].abs() <= 0.01
    coins["base"] = np.where(fresh, coins["value"].abs(), coins["before"].abs())
    actual = coins.groupby("day")["pnl"].sum().reindex(book.days).fillna(0.0)
    exposure = coins.groupby("day")["before"].sum().reindex(book.days).fillna(0.0)
    gross = coins.groupby("day")["base"].sum().reindex(book.days).fillna(0.0)
    # From the day the book is meaningful (10% of equity, the factor model's rule): tiny early days are noise.
    before_equity = book.equity.shift(1).where(lambda e: e > 0)   # no equity yet: no day to judge
    active = gross >= ACTIVE_GROSS * before_equity
    if active.sum() < 5:
        return
    first = active.idxmax()
    picks = (actual / gross).where(active, 0.0)[first:]
    same = (exposure * bench / gross).where(active, 0.0)[first:]
    path = lambda r: 100 * ((1 + r).cumprod() - 1)
    figure = go.Figure([
        go.Scatter(x=picks.index, y=path(picks), name="Picks"),
        go.Scatter(x=same.index, y=path(same), name=f"Same $ in {book.benchmark}",
                   line={"color": "#F7931A" if book.benchmark == "BTC" else "#16a34a"}),
    ])
    # Picks added: green where the picks are ahead of the benchmark, red where behind; one legend entry.
    from desk.hl.account_analytics import split_at_zero

    ahead, behind = split_at_zero(path(picks - same))
    for part, color, name in ((ahead, "#16a34a", "Picks added: ahead"), (behind, "#dc2626", "Picks added: behind")):
        if part.notna().any():   # a legend entry only for a part that is drawn, in the colour it is drawn in
            figure.add_trace(go.Scatter(x=part.index, y=part, name=name, line={"color": color, "dash": "dot"}))
    figure.update_layout()
    figure.update_layout(title=f"Picks vs {book.benchmark} on the money held (%, since {_since(first)})",
                         template="plotly_dark", hovermode="x unified", yaxis_ticksuffix="%",
                         legend={"orientation": "h", "y": 1.08})
    show_chart(figure)


_show_versus = show_versus_benchmark


def show_versus_benchmark_with_chart(book: "Book"):
    _show_versus(book)
    plot_picks(book)


Book.show_versus_benchmark = show_versus_benchmark_with_chart


def _closed_positions(book: "Book") -> pd.DataFrame | None:
    """The book's closed positions (HL fills and, for stocks, the IB statements' closing trades); None with too few."""
    from desk import return_distribution as dist
    from desk.accounts import accounts_for
    from desk.hl.account_analytics import fetch_user_fills

    fills = {account.id: fetch_user_fills(account.id) for account in accounts_for("hl", book.desk.accounts)}
    closed = dist.closed_returns(fills)
    if book.kind == "Stocks":
        from desk.accounts import ib_data_dir

        closed = pd.concat([closed, dist.ib_statement_closes(ib_data_dir())], ignore_index=True)
    closed = closed[closed["book"] == book.kind]
    if len(closed) < 20:
        print(f"Only {len(closed)} closed {book.kind.lower()} positions: too few for a distribution.")
        return None
    return closed


def show_returns_on_closed(book: "Book", closed: pd.DataFrame | None = None):
    """How much volume the book closed at each return, with the density behind it (the chart)."""
    from desk import return_distribution as dist

    closed = _closed_positions(book) if closed is None else closed
    if closed is not None:
        dist.plot_return_distribution(closed, f"{book.kind} closes")


def show_closed_positions(book: "Book", closed: pd.DataFrame | None = None):
    """The closed positions' table: volume, hit rate, payoff, expectancy. The closes are also saved as a dataset."""
    from pathlib import Path

    from desk import return_distribution as dist

    closed = _closed_positions(book) if closed is None else closed
    if closed is None:
        return
    table = dist.metrics_table(closed)
    book._table(table, "CLOSED POSITIONS", {"Volume": _usd, "In profit": _pct, "Avg win": _pct, "Avg loss": _pct,
                                           "Expectancy": _pct, "PnL": _usd, "Fees": _usd,
                                           "Payoff": lambda v: f"{v:.2f}", "Profit factor": lambda v: f"{v:.2f}",
                                           "Median hold (days)": lambda v: f"{v:.1f}"})
    try:   # the closes as a dataset for modelling and backtests
        path = Path(__file__).resolve().parents[1] / "data" / f"closed_trades_{book.kind.lower()}.csv"
        closed.to_csv(path, index=False)
        print(f"{len(closed)} closes saved to data/{path.name}: when, venue, account, coin, side, size, entry, exit, "
              "notional, pnl, fees, return, opened, hold_days.")
    except OSError:
        pass


def show_return_distribution(book: "Book"):
    """The chart and the table of the closed positions together."""
    closed = _closed_positions(book)
    show_returns_on_closed(book, closed)
    show_closed_positions(book, closed)


Book.show_return_distribution = show_return_distribution
Book.show_returns_on_closed = show_returns_on_closed
Book.show_closed_positions = show_closed_positions


def show_trades_vs_pnl(book: "Book"):
    """Fills per day against the book's daily return, on the days the book held something meaningful."""
    from desk import return_distribution as dist
    from desk.accounts import accounts_for, ib_data_dir
    from desk.hl.account_analytics import fetch_user_fills

    fills = {account.id: fetch_user_fills(account.id) for account in accounts_for("hl", book.desk.accounts)}
    ib = ib_statement_fills(ib_data_dir())[0] if book.kind == "Stocks" else None
    returns = book.returns.reindex(active_days(book)).dropna()
    dist.plot_trades_vs_pnl(dist.trades_per_day(fills, book.kind, ib), returns, f"{book.kind} book")


Book.show_trades_vs_pnl = show_trades_vs_pnl
