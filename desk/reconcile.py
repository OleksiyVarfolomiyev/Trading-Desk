"""Cross-checks between the dashboard's tables, as an accountant ties out a ledger: totals that describe the same
money must agree, and shares of a whole must add up to it. A break means a table left something out (as the
strategy table once left out IB's positions) or double counts it.

``checks(tables)`` takes the page's tables (``{"columns", "rows", "total"}`` as desk/dashboard.py builds them)
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
    return found
