"""The desk's change over the last 24 hours, net of deposits, withdrawals and transfers.

Each venue is measured where its PnL is known without its cash flows:

* HL wallets: HL's own 24-hour PnL (the ``day`` portfolio window: value change less deposits,
  withdrawals and transfers), the figure the HL app shows.
* IB: the current holdings times each symbol's price move since this time yesterday (Yahoo
  Finance). Cash does not move, deposits are not PnL, and over a weekend the change is zero.
* Wallets outside HL: the current token balances times DefiLlama's 24-hour price change, so a
  token moved in or out is not counted as a gain or loss.

Trades in the last 24 hours on IB and in the wallets are valued as if held all day (a share bought
this morning counts its move since yesterday); HL is exact. An account that cannot be priced is
left out and named in ``missing``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

DAY = pd.Timedelta(hours=24)


@dataclass
class DayChange:
    change: float                                   # USD, net of flows
    value: float                                    # desk value now, of the accounts measured
    parts: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def pct(self) -> float:
        """Change over the value 24 hours ago (today's value less the change)."""
        base = self.value - self.change
        return self.change / base if base > 0 else float("nan")

    def as_dict(self) -> dict:
        return {"change": self.change, "pct": self.pct, "parts": self.parts, "missing": self.missing}


def hl_change(wallet: str) -> float:
    """HL's 24-hour PnL for a wallet (perps and spot, net of deposits and transfers)."""
    from desk.hl.account_analytics import _fetch_portfolio_periods, _period_history

    history = _period_history(_fetch_portfolio_periods(wallet), "day")
    if len(history) < 2:
        raise ValueError("HL returned no 24-hour history")
    return float(history["pnl"].iloc[-1] - history["pnl"].iloc[0])


def holdings_change(positions, since: pd.Timestamp) -> float:
    """Price move since ``since`` on stock/ETF holdings (Yahoo); raises when one cannot be priced."""
    from desk.venues import yahoo

    total = 0.0
    for position in positions:
        then, now, currency = yahoo.price_change(position.symbol, since)
        if currency != position.currency:
            raise ValueError(f"{position.symbol}: Yahoo quotes {currency}, IB holds {position.currency}")
        total += position.quantity * position.multiplier * position.fx_to_usd * (now - then)
    return total


def token_keys(positions) -> dict[str, str]:
    """DefiLlama price key for each wallet holding's ``chain:token`` ref."""
    from desk.venues.onchain import CHAINS, price_key

    keys = {}
    for position in positions:
        chain, _, token = position.ref.partition(":")
        if chain in CHAINS and token:
            keys[position.ref] = price_key(CHAINS[chain], token).lower()
    return keys


def wallet_change(positions, changes: dict[str, float]) -> float:
    """Value now times the 24-hour price change: value × r / (1 + r) is the dollar move."""
    keys = token_keys(positions)
    total = 0.0
    for position in positions:
        change = changes.get(keys.get(position.ref, ""))
        if change is None or change <= -1:
            raise ValueError(f"no 24-hour price change for {position.symbol}")
        total += position.notional_usd * change / (1 + change)
    return total


def desk_day_change(snapshots, now: pd.Timestamp | None = None) -> DayChange | None:
    """The 24-hour change of every account that could be read and priced; None when none could."""
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    since = now - DAY
    result = DayChange(change=0.0, value=0.0)
    wallets = [snapshot for snapshot in snapshots if snapshot.venue == "onchain"]
    changes: dict[str, float] = {}
    if any(snapshot.positions for snapshot in wallets):
        from desk.venues.onchain import fetch_changes_24h

        try:
            changes = fetch_changes_24h({key for snapshot in wallets
                                         for key in token_keys(snapshot.positions).values()})
        except Exception:  # every wallet ends up in ``missing`` below
            changes = {}
    for snapshot in snapshots:
        try:
            if snapshot.venue == "hl":
                change = hl_change(snapshot.account)
            elif snapshot.venue == "ib":
                change = holdings_change(snapshot.positions, since)
            elif snapshot.venue == "onchain":
                change = wallet_change(snapshot.positions, changes)
            else:
                raise ValueError(f"no 24-hour source for venue {snapshot.venue}")
        except Exception:
            result.missing.append(snapshot.label)
            continue
        if not math.isfinite(change):
            result.missing.append(snapshot.label)
            continue
        result.parts[snapshot.label] = round(change, 2)
        result.change += change
        result.value += snapshot.equity_usd
    return result if result.parts else None


HOURS_PER_YEAR = 24 * 365


def position_funding(snapshots) -> dict[tuple[str, str], float]:
    """Each HL perp's funding at the current rate, annualised, from the holder's side, keyed (account label,
    symbol): positive when the position earns it (a short while longs pay), negative when it pays."""
    positions = [(snapshot.label, position) for snapshot in snapshots if snapshot.venue == "hl"
                 for position in snapshot.positions]
    if not positions:
        return {}
    from desk.hl.account_analytics import fetch_market_contexts

    contexts = fetch_market_contexts(sorted({position.ref for _, position in positions}))
    rates: dict[tuple[str, str], float] = {}
    for label, position in positions:
        try:
            rate = float(contexts[(position.ref or "HL", position.symbol)]["funding"])
        except (KeyError, TypeError, ValueError):
            continue
        rates[label, position.symbol] = (-rate if position.quantity > 0 else rate) * HOURS_PER_YEAR  # rate is hourly
    return rates


def position_changes(snapshots, now: pd.Timestamp | None = None) -> dict[tuple[str, str], float]:
    """Each open position's 24-hour price change, as a fraction, keyed (account label, symbol).

    HL perps use HL's mark against its price 24 hours ago (``prevDayPx``), IB holdings Yahoo's
    move since this time yesterday, and wallet tokens DefiLlama's 24-hour change. A position that
    cannot be priced is left out.
    """
    now = pd.Timestamp.now(tz="UTC") if now is None else now
    changes: dict[tuple[str, str], float] = {}
    by_venue: dict[str, list] = {}
    for snapshot in snapshots:
        for position in snapshot.positions:
            by_venue.setdefault(snapshot.venue, []).append((snapshot.label, position))

    if by_venue.get("hl"):
        from desk.hl.account_analytics import fetch_market_contexts

        try:
            contexts = fetch_market_contexts(sorted({position.ref for _, position in by_venue["hl"]}))
        except Exception:
            contexts = {}
        for label, position in by_venue["hl"]:
            context = contexts.get((position.ref or "HL", position.symbol), {})
            try:
                mark, before = float(context["markPx"]), float(context["prevDayPx"])
            except (KeyError, TypeError, ValueError):
                continue
            if before > 0:
                changes[label, position.symbol] = mark / before - 1

    if by_venue.get("ib"):
        from desk.venues import yahoo

        moves: dict[str, float] = {}
        for label, position in by_venue["ib"]:
            if position.symbol not in moves:
                try:
                    then, latest, _ = yahoo.price_change(position.symbol, now - DAY)
                    moves[position.symbol] = latest / then - 1 if then > 0 else float("nan")
                except Exception:
                    moves[position.symbol] = float("nan")
            if math.isfinite(moves[position.symbol]):
                changes[label, position.symbol] = moves[position.symbol]

    if by_venue.get("onchain"):
        from desk.venues.onchain import fetch_changes_24h

        keys = token_keys([position for _, position in by_venue["onchain"]])
        try:
            moves = fetch_changes_24h(set(keys.values()))
        except Exception:
            moves = {}
        for label, position in by_venue["onchain"]:
            move = moves.get(keys.get(position.ref, ""))
            if move is not None and math.isfinite(move):
                changes[label, position.symbol] = move
    return changes
