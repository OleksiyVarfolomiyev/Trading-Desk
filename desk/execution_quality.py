"""Execution quality from the HL fills: how much of the traded money was made (resting, maker) rather than taken, and what
the fees cost in basis points of it. Slippage against the mid is not in the fills, so it is not claimed here."""

from __future__ import annotations

import time

DAYS = 30


def summarize(fills: list[dict], days: int = DAYS, now: float | None = None) -> dict:
    """``{"maker_share", "fee_bps", "notional", "fees", "taker_excess", "days"}`` over the last ``days`` days.

    ``maker_share`` is the share of traded notional that rested on the book; ``taker_excess`` is what the taker fills
    would have saved at the maker rate seen in the same fills (USD, 0 where there is no maker fill to compare).
    """
    now = time.time() if now is None else now
    since = (now - days * 86400) * 1000
    maker = taker = maker_fee = taker_fee = 0.0
    for fill in fills:
        if float(fill.get("time") or 0) < since or fill.get("liquidation"):
            continue
        notional = abs(float(fill.get("px") or 0) * float(fill.get("sz") or 0))
        fee = float(fill.get("fee") or 0)
        if fill.get("crossed"):
            taker += notional
            taker_fee += fee
        else:
            maker += notional
            maker_fee += fee
    total = maker + taker
    if total <= 0:
        return {}
    maker_rate = maker_fee / maker if maker else None
    excess = max(0.0, taker_fee - taker * maker_rate) if maker_rate is not None else 0.0
    return {"maker_share": maker / total, "fee_bps": 1e4 * (maker_fee + taker_fee) / total, "notional": total,
            "fees": maker_fee + taker_fee, "taker_excess": excess, "days": days}


def desk_summary(days: int = DAYS) -> dict:
    """The summary over every configured HL wallet (network: HL's fills)."""
    from desk.accounts import accounts_for
    from desk.hl.account_analytics import fetch_user_fills

    fills = []
    for account in accounts_for("hl"):
        fills += fetch_user_fills(account.id)
    return summarize(fills, days)
