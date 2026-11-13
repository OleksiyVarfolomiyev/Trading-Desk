"""Adverse funding: an HL perp position paying funding fast enough to matter.

Funding is read from the holder's side (positive when the position earns it, negative when it pays) and
annualised, as in the positions tables' Funding (APR) column. A position is flagged when it pays more than
ADVERSE_APR, which is 0.1% of its value a day, and the payment is at least MIN_DAILY_USD a day (so a small
position at an extreme rate is not noise on the list).

The highlights (desk/overview.py and web/live.js) flag the current rate; the trading daemon flags a rate
that has stayed adverse for SUSTAINED_HOURS hourly payments in a row (execution/hl_daemon.py).
"""

from __future__ import annotations

HOURS_PER_YEAR = 24 * 365
ADVERSE_APR = 0.365          # 0.1% of the position's value a day
MIN_DAILY_USD = 0.25         # ignore payments smaller than this a day
SUSTAINED_HOURS = 6          # the daemon's alert: this many hourly payments in a row past ADVERSE_APR
TITLE = "Adverse funding"


def daily_cost(apr: float, value_usd: float) -> float:
    """What the position pays a day (positive), or earns (negative), at a holder-side APR."""
    return -apr * abs(value_usd) / 365


def is_adverse(apr: float, value_usd: float) -> bool:
    return apr == apr and apr <= -ADVERSE_APR and daily_cost(apr, value_usd) >= MIN_DAILY_USD


def attention_row(items: list[tuple[str, float, float]]) -> tuple[str, str] | None:
    """The highlights row for ``items`` of (name, holder-side APR, value in USD), or None when none pays enough.

    The name reads like "MELANIA short (c248)"; the worst payer comes first.
    """
    flagged = sorted(((name, apr, daily_cost(apr, value)) for name, apr, value in items if is_adverse(apr, value)),
                     key=lambda item: -item[2])
    if not flagged:
        return None
    parts = [f"{name} pays {-apr:.1%} APR, ${cost:,.2f}/day" for name, apr, cost in flagged]
    return TITLE, ("; ".join(parts) + f". Flagged past {ADVERSE_APR:.1%} APR (0.1% of the position a day); "
                   "funding is hourly and can turn, so weigh it against the trade's expected move.")


def sustained(rates: list[float], direction: int) -> float | None:
    """The average holder-side APR over the last SUSTAINED_HOURS hourly ``rates`` (HL's raw rate, positive
    when longs pay) of a position on ``direction`` (+1 long, -1 short), when every one of them was past
    ADVERSE_APR; otherwise None."""
    if len(rates) < SUSTAINED_HOURS:
        return None
    aprs = [-direction * rate * HOURS_PER_YEAR for rate in rates[-SUSTAINED_HOURS:]]
    if all(apr <= -ADVERSE_APR for apr in aprs):
        return sum(aprs) / len(aprs)
    return None
