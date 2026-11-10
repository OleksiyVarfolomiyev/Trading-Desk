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
