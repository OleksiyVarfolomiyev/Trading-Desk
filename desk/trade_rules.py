"""Warnings from the desk's own trade history, shown in the pre-flight plan.

From all 233 closed HL perp trades to 2026-11-11 (both wallets; ``research`` reproduces them):

* shorts made $1,006 (profit factor 2.42); longs lost $472 (0.72);
* alts made $879; BTC and ETH lost $267 (0.51);
* trades above $1,500 notional lost $660 (24 trades, profit factor below 0.6);
* trades held over 7 days lost $943 (0.34); see ``desk.journal.REVIEW_AFTER_DAYS``.

These are warnings, not blocks: a hedge or a conviction trade is a decision the history does not
cover, so the side and coin warnings apply to directional trades only.
"""

from __future__ import annotations

from desk.numbers import usd

LARGE_NOTIONAL_USD = 1_500.0
MAJORS = {"BTC", "ETH"}


def entry_notes(coin: str, side: str, notional: float, strategy: str = "directional") -> list[str]:
    """The history's warnings for a planned entry (an empty list when none applies)."""
    notes = []
    if strategy != "hedge" and abs(notional) > LARGE_NOTIONAL_USD:
        notes.append(f"large size ({usd(abs(notional))}): trades above {usd(LARGE_NOTIONAL_USD)} lost money in the desk's history")
    if strategy == "directional":
        if side.upper() == "LONG":
            notes.append("weak side: directional longs lost money in the desk's history (profit factor 0.72; shorts 2.42)")
        if coin.split(":")[-1].upper() in MAJORS:
            notes.append(f"weak market ({coin}): directional BTC and ETH trades lost money in the desk's history (profit factor 0.51)")
    return notes
