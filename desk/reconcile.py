"""Cross-checks between the dashboard's tables, as an accountant ties out a ledger: totals that describe the same
money must agree, and shares of a whole must add up to it. A break means a table left something out (as the
strategy table once left out IB's positions) or double counts it.

``checks(tables)`` takes the page's tables (``{"columns", "rows", "total"}`` as desk/app.py builds them)
and returns attention rows, ("Tables disagree", what and by how much), empty when everything ties out. web/live.js
runs the same checks in the browser (``reconcile``).

* **uPnL:** the strategy table's total uPnL and the accounts table's total uPnL, both shares of desk value.
* **Desk shares:** the accounts' "% of desk" adds up to 100%.
* **Weights:** the position weights of the crypto and the stocks tables together add up to 100%.

TOLERANCE allows for the tables being read moments apart (the strategy table is the daemon's hourly reading).
"""

from __future__ import annotations

import math

TOLERANCE = 0.01   # one percentage point of desk value


def _column_sum(table: dict | None, column: str) -> float:
    """The table's total for ``column`` if it has one, else the sum of its rows; NaN without that column."""
    if not table or not any(item.get("key") == column for item in table.get("columns", [])):
        return float("nan")
    total = (table.get("total") or {}).get(column)
    if isinstance(total, (int, float)) and math.isfinite(total):
        return float(total)
    values = [row.get(column) for row in table.get("rows", [])]
    numbers = [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
    return sum(numbers) if numbers else float("nan")


def _row_sum(tables: list[dict | None], column: str) -> float:
    """The sum of ``column`` over the body rows of several tables (their totals left out)."""
    numbers = [float(row[column]) for table in tables if table for row in table.get("rows", [])
               if isinstance(row.get(column), (int, float)) and math.isfinite(row[column])]
    return sum(numbers) if numbers else float("nan")


def _pct(value: float) -> str:
    from desk.numbers import pct

    return pct(value)


def checks(tables: dict[str, dict]) -> list[tuple[str, str]]:
    found = []

    def compare(label: str, left_name: str, left: float, right_name: str, right: float) -> None:
        if math.isfinite(left) and math.isfinite(right) and abs(left - right) > TOLERANCE:
            found.append(("Tables disagree", f"{label}: {left_name} {_pct(left)}, {right_name} {_pct(right)}"))

    compare("uPnL", "strategies", _column_sum(tables.get("strategies"), "uPnL"),
            "accounts", _column_sum(tables.get("accounts"), "uPnL / equity"))
    compare("desk shares", "accounts", _row_sum([tables.get("accounts")], "% of desk"), "expected", 1.0)
    compare("position weights", "crypto and stocks", _row_sum([tables.get("positions"), tables.get("positions_stocks")], "Weight"),
            "expected", 1.0)
    found += strategy_groups(tables)
    found += market_making_excluded(tables)
    return found


def strategy_groups(tables: dict[str, dict]) -> list[tuple[str, str]]:
    """The positions tables split by strategy: every row has a group, and the groups' subtotals add up to the table's own
    total (value and uPnL, in dollars; open risk as a share of the cap). Tables without a Strategy column are skipped."""
    from desk import position_groups

    found = []
    for name in position_groups.TABLES:
        spec = tables.get(name) or {}
        rows, total = spec.get("rows") or [], spec.get("total") or {}
        if not rows or not any(column.get("key") == "Strategy" for column in spec.get("columns", [])):
            continue
        missing = [row.get("Symbol") for row in rows if not row.get("Strategy")]
        if missing:
            found.append(("Tables disagree", f"{name}: {len(missing)} positions have no strategy group"))
        groups = position_groups.group_totals(rows)
        for key, label, tolerance in (("Value", "value", 0.5), ("uPnL", "uPnL", 0.5), ("Risk", "open risk", 1e-6)):
            expected = total.get(key)
            if not isinstance(expected, (int, float)) or not math.isfinite(expected):
                continue
            summed = sum(group[key] for group in groups.values())
            if abs(summed - expected) > tolerance:
                found.append(("Tables disagree", f"{name} strategy groups: {label} {summed:,.4g}, table total {expected:,.4g}"))
    return found


def market_making_excluded(tables: dict[str, dict]) -> list[tuple[str, str]]:
    """Market-making inventory is out of the positions tiles but still in the exposure table: the shown positions plus what
    was taken out must add up to the exposure table's gross value (no silent gap)."""
    taken = sum(float((tables.get(name) or {}).get("excluded", {}).get("gross") or 0.0) for name in ("positions", "positions_stocks"))
    if not taken:
        return []
    shown = sum(abs(float(row["Value"])) for name in ("positions", "positions_stocks") for row in (tables.get(name) or {}).get("rows", [])
                if isinstance(row.get("Value"), (int, float)) and math.isfinite(row["Value"]))
    exposure = (tables.get("exposure") or {}).get("total") or {}
    gross = exposure.get("Gross")
    if isinstance(gross, (int, float)) and math.isfinite(gross) and abs(shown + taken - gross) > 1.0:
        return [("Tables disagree", f"positions with market making taken out: shown {shown:,.2f} + market making {taken:,.2f}, exposure gross {gross:,.2f}")]
    return []


# ---------------------------------------------------------------------------------------------------------------- windows
# Figures compared in one table or chart share their start, end and base (desk policy). ``windows`` flags what the page
# cannot line up by itself: a book line ending more than a day before the desk (a stale book build: the chart would cut
# every line back to it), comparison lines that never overlap, and the columns of "What drove excess return" that start or end on different days.
# Stocks vs SPY run on SPY sessions, so at a weekend their window can end (or start) on Friday's close while crypto's
# runs on: a gap made only of Saturdays and Sundays is allowed (the excess-return tile's hover says so).
WINDOW_LAG_DAYS = 1   # the books are built to yesterday's close; the desk runs to today


def _day(text) -> "datetime.date | None":
    import datetime as _dt

    try:
        return _dt.date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def _weekend_gap(a, b) -> bool:
    """True when every day after the earlier date up to the later one is a Saturday or a Sunday."""
    import datetime as _dt

    low, high = sorted((a, b))
    days = [low + _dt.timedelta(days=n) for n in range(1, (high - low).days + 1)]
    return bool(days) and all(day.weekday() >= 5 for day in days)


def _chart_lines(lines: list[dict]) -> dict[str, tuple]:
    """The return chart's desk and book lines -> (first day, last day)."""
    import re

    out = {}
    for line in lines or []:
        name = str(line.get("name") or "")
        match = re.match(r"^(total|desk|crypto|stocks)", name, re.I)
        days = [_day(x) for x in line.get("x") or []]
        days = [day for day in days if day]
        if match and days:
            out["desk" if match.group(1).lower() in ("total", "desk") else match.group(1).lower()] = (days[0], days[-1])
    return out


def windows(charts: dict | None, comparison: dict | None) -> list[tuple[str, str]]:
    found = []
    lines = _chart_lines((charts or {}).get("twr") or [])
    if "desk" in lines and len(lines) > 1:
        desk_end = lines["desk"][1]
        for name, (first, last) in lines.items():
            if name != "desk" and (desk_end - last).days > WINDOW_LAG_DAYS:
                found.append(("Windows differ", f"return chart: {name} ends {last:%b %-d}, desk {desk_end:%b %-d}"))
        start, end = max(first for first, _ in lines.values()), min(last for _, last in lines.values())
        if start > end:
            found.append(("Windows differ", "return chart: the desk and book lines share no days"))
    # "What drove excess return": the four columns of a window (crypto vs BTC, stocks vs SPY, desk vs BTC, desk vs SPY) are
    # computed over the same matched sessions, so their start and end dates must agree (a weekend gap is allowed).
    names = {"crypto": "crypto vs BTC", "stocks": "stocks vs SPY", "desk_btc": "desk vs BTC", "desk_spy": "desk vs SPY"}
    for window, groups in ((comparison or {}).get("regimes") or {}).items():
        columns = ((groups or {}).get("all") or {}).get("comparisons") or {}
        spans = {key: (_day((columns[key] or {}).get("start")), _day((columns[key] or {}).get("end"))) for key in names if key in columns}
        spans = {key: span for key, span in spans.items() if None not in span}
        first = next(iter(spans.values()), None)
        if first is None or all(all(x == y or _weekend_gap(x, y) for x, y in zip(span, first)) for span in spans.values()):
            continue
        found.append(("Windows differ", f"excess return {window}: " + ", ".join(
            f"{names[key]} {span[0]:%b %-d '%y} to {span[1]:%b %-d}" for key, span in spans.items())))
    return found
