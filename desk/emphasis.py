"""What to look at first in a table: the key column, its standout value, and headline rows.

Every desk table names the column(s) that answer its question (``key_columns``). Those cells get
a soft tint and the header an accent colour; in each key column the standout value gets a
stronger tint and bold: the largest by size (``"abs"``), the highest (``"max"``) or the lowest
(``"min"``), or none (``None``). Metric/value tables name their headline rows instead
(``key_rows``, matched against the first column). Total rows are never the standout.

The same colours are used by the notebook tables (``display_interactive_table`` and the pandas
Styler tables in research) and by the live dashboard (``.key`` / ``.key-top`` in app.css).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# Strong enough to read on the notebooks' black tables and on the dashboard's light and dark themes.
KEY_TINT = "background-color:rgba(59,130,246,0.32);"
# The standout keeps the key tint and gets an outline, so red/green text on it stays readable.
TOP_TINT = "background-color:rgba(59,130,246,0.32);box-shadow:inset 0 0 0 2px #60a5fa;font-weight:700;"
ROW_TINT = KEY_TINT + "font-weight:600;"   # headline rows of metric tables
KEY_HEADER = "color:#93c5fd;font-weight:700;"
# Gains and losses on dark tables (the light-page #188038 / #c62828 are too dark on black).
DARK_SIGN_COLORS = {"#188038": "#4ade80", "#c62828": "#f87171", "#d93025": "#f87171"}
EXTREMES = {"abs", "max", "min", None}
# A table's title is its own caption: white on black above the header, as in desk_overview.
TITLE_CAPTION = ("caption-side:top;text-align:left;font-weight:600;padding:0.35em 0.55em;"
                 "color:#f9fafb;background:#000;")


def normalize(key_columns) -> dict:
    """``["A", "B"]`` or ``{"A": "min"}`` -> ``{"A": "abs", ...}`` (a list means largest by size)."""
    if not key_columns:
        return {}
    if isinstance(key_columns, dict):
        bad = {mode for mode in key_columns.values() if mode not in EXTREMES}
        if bad:
            raise ValueError(f"unknown standout mode(s) {bad}; use 'abs', 'max', 'min' or None")
        return dict(key_columns)
    if isinstance(key_columns, str):
        return {key_columns: "abs"}
    return {column: "abs" for column in key_columns}


def _number(value) -> float:
    if isinstance(value, (bool, np.bool_)):
        return float("nan")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return number if math.isfinite(number) else float("nan")


def standout_position(values, mode: str | None, skip: set[int] | None = None) -> int | None:
    """Position (0-based) of the standout value in ``values``, or None (no numbers, or no mode)."""
    if mode is None:
        return None
    best, best_score = None, None
    for position, value in enumerate(values):
        if skip and position in skip:
            continue
        number = _number(value)
        if math.isnan(number):
            continue
        score = abs(number) if mode == "abs" else number if mode == "max" else -number
        if best_score is None or score > best_score:
            best, best_score = position, score
    return best


def titled(styler, title):
    """Give a pandas Styler its title as a caption styled like every other desk table."""
    if not title:
        return styler
    return styler.set_caption(str(title)).set_table_styles(
        [{"selector": "caption", "props": TITLE_CAPTION}], overwrite=False)


def style_frame(styler, frame: pd.DataFrame, key_columns=None, key_rows=None, total_last: bool = False,
                title=None):
    """Apply key-column and key-row emphasis (and the title caption) to a pandas Styler of ``frame``."""
    styler = titled(styler, title)
    keys = {column: mode for column, mode in normalize(key_columns).items() if column in frame.columns}
    skip = {len(frame) - 1} if total_last and len(frame) else set()
    for column, mode in keys.items():
        top = standout_position(frame[column].tolist(), mode, skip)

        def paint(series, top=top):
            return [TOP_TINT if position == top else KEY_TINT for position in range(len(series))]

        styler = styler.apply(paint, subset=[column])
        styler = styler.set_table_styles({column: [{"selector": "th", "props": KEY_HEADER}]}, overwrite=False)
    if key_rows:
        wanted = {str(label) for label in key_rows}
        labels = [str(label) for label in frame.index]

        def paint_row(row):
            return [ROW_TINT if str(row.name) in wanted else "" for _ in row]

        if wanted & set(labels):
            styler = styler.apply(paint_row, axis=1)
    return styler


def key_columns_first(columns, keys, colored=(), text=None) -> list:
    """Column order with the key (tinted) and colour-coded columns moved up, in their own order,
    right behind the leading text columns that name each row (account, symbol, side).

    ``text`` says which columns hold text; by default every column not key or coloured that comes
    before the first one that is.
    """
    keys, colored = set(keys or ()), set(colored or ())
    stands_out = lambda column: column in keys or column in colored
    is_text = (lambda column: column in text) if text is not None else (lambda column: True)
    lead = []
    for column in columns:
        if stands_out(column) or not is_text(column):
            break
        lead.append(column)
    moved = [column for column in columns if stands_out(column) and column not in lead]
    return lead + moved + [column for column in columns if column not in lead and column not in moved]
