"""Adverse funding: an HL perp position paying funding fast enough to matter.

Funding is read from the holder's side (positive when the position earns it, negative when it pays) and
annualised, as in the positions tables' Funding (APR) column. A position is flagged when it pays more than
ADVERSE_APR, which is 0.1% of its value a day, and the payment is at least MIN_DAILY_USD a day (so a small
position at an extreme rate is not noise on the list).

The highlights (desk/overview.py and web/live.js) flag the current rate; the trading daemon flags a rate
that has stayed adverse for SUSTAINED_HOURS hourly payments in a row (execution/hl_daemon.py). The dashboards
show both as one "Funding" row (``consolidate``): sustained payers with their average, then any payer that is
adverse only now.
"""

from __future__ import annotations

import re
from desk.numbers import pct as _pct

HOURS_PER_YEAR = 24 * 365
ADVERSE_APR = 0.365          # 0.1% of the position's value a day
MIN_DAILY_USD = 0.25         # ignore payments smaller than this a day
SUSTAINED_HOURS = 6          # the daemon's alert: this many hourly payments in a row past ADVERSE_APR
TITLE = "Adverse funding"
ROW_TITLE = "Funding"        # the one highlights row both become
# The daemon's alert text (execution/hl_daemon.py Alerts.watch_funding) and this module's attention_row items.
DAEMON_TEXT = re.compile(r"^\.\.\.(\w+) (\S+) (long|short) (-?[\d.]+)% avg APR over (\d+) hrs")
NOW_ITEM = re.compile(r"^(\S+) (long|short) \((.+?)\) (-?[\d.]+)% APR$")


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
    return TITLE, ", ".join(f"{name} {_pct(apr)} APR" for name, apr, _ in flagged) + "."


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


def _now_items(text: str) -> list[tuple[str, str, str, float]] | None:
    """(coin, side, account, APR) for each item of an attention_row text; None if one does not parse."""
    items = []
    for item in text.rstrip(".").split(", "):
        match = NOW_ITEM.match(item)
        if not match:
            return None
        coin, side, account, apr = match.groups()
        items.append((coin.split(":")[-1], side, account[-4:], float(apr) / 100))
    return items


def consolidate(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``rows`` with the daemon's sustained-funding alerts (title "Funding") and the current-rate row (TITLE)
    merged into one "Funding" row where the first of them stood: sustained payers worst first with their average,
    then the payers adverse only at the current rate. Other rows, and any text not in a known shape, stay as they are."""
    sustained: dict[tuple[str, str], tuple[str, float]] = {}
    now: dict[tuple[str, str], tuple[str, float]] = {}
    hours, kept, at = SUSTAINED_HOURS, [], None
    for title, text in rows:
        match = DAEMON_TEXT.match(text) if title.lower() == ROW_TITLE.lower() else None
        items = _now_items(text) if title == TITLE else None
        if match:
            account, coin, side = match.group(1)[-4:], match.group(2).split(":")[-1], match.group(3)
            sustained[coin, account] = (f"{coin} {side} ({account})", float(match.group(4)) / 100)
            hours = int(match.group(5))
        elif items is not None:
            for coin, side, account, apr in items:
                now[coin, account] = (f"{coin} {side} ({account})", apr)
        else:
            kept.append((title, text))
            continue
        at = len(kept) if at is None else at
    if at is None:
        return list(rows)
    worst_first = lambda values: sorted(values, key=lambda value: value[1])
    parts = []
    if sustained:
        parts.append(", ".join(f"{name} {_pct(apr)}" for name, apr in worst_first(sustained.values())) + f" avg APR over {hours} hrs")
    only_now = [value for key, value in now.items() if key not in sustained]
    if only_now:
        parts.append(", ".join(f"{name} {_pct(apr)}" for name, apr in worst_first(only_now)) + " APR now")
    return kept[:at] + [(ROW_TITLE, "; ".join(parts) + ".")] + kept[at:]
