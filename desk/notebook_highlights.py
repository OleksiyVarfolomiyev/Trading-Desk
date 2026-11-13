"""Highlights shown at the top of a notebook: what stands out, what needs attention, and what
changed since the notebook last ran.

Each report builds two lists of ``(area, text)`` rows and optionally compares a few key
numbers with the previous run (saved in a small JSON file). ``render`` turns that into one
HTML block. ``HighlightsPanel`` reserves the block at the top of a notebook and fills it in
later, for notebooks whose highlights depend on sections that run further down.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from IPython.display import HTML, display
from desk.numbers import clock

Row = tuple[str, str]


def no_math(markup: str) -> str:
    """Mark HTML so notebook viewers do not typeset it as math.

    Jupyter and nbconvert pass HTML outputs through MathJax, which reads the text between two ``$`` as a
    formula: "MELANIA $110.97 (3.9%), ICP $42.18" lost its dollar signs and set the amounts in math italics.
    ``tex2jax_ignore`` is the class MathJax skips. Every ``display(HTML(...))`` of desk output goes through here.
    """
    return f"<div class='tex2jax_ignore'>{markup}</div>"
GOOD = "#188038"
BAD = "#d93025"
NEUTRAL = "#5f6368"


def money(value: float) -> str:
    from desk.numbers import usd

    return usd(value)


def signed_money(value: float) -> str:
    from desk.numbers import usd

    return usd(value)


def percent(value: float, signed: bool = False, digits: int = 1) -> str:
    from desk.numbers import pct

    return pct(value)  # positive values carry no "+" sign anywhere on the desk; digits by size (desk/numbers.py)


FORMATS = {
    "usd": (money, signed_money),
    "pct": (lambda v: percent(v, digits=2), lambda v: f"{v * 100:.2f} pts" if math.isfinite(v) else "n/a"),
    "num": (lambda v: f"{v:,.2f}" if math.isfinite(v) else "n/a", lambda v: f"{v:,.2f}" if math.isfinite(v) else "n/a"),
    "int": (lambda v: f"{v:,.0f}", lambda v: f"{v:,.0f}"),
}


@dataclass
class SinceLastRun:
    """Changes in key numbers and named items (positions, assets) since the previous run."""

    last_run: pd.Timestamp | None = None
    changes: list[tuple[str, str, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class HighlightsState:
    """The previous run's key numbers, loaded once per data fetch so a rerun keeps its baseline."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        try:
            self.baseline = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.baseline = None

    def compare(self, metrics: dict[str, tuple[float, str]], items: dict[str, list[str]] | None = None,
                save: bool = True) -> SinceLastRun:
        """Compare ``metrics`` ({name: (value, format)}) and ``items`` ({name: [labels]}) with the
        baseline, then save the current values for the next run."""
        items = items or {}
        metrics = {name: (float(value), kind) for name, (value, kind) in metrics.items()
                   if value is not None and math.isfinite(float(value))}
        since = SinceLastRun()
        if self.baseline:
            since.last_run = pd.Timestamp(self.baseline.get("ts"))
            previous = self.baseline.get("metrics", {})
            for name, (value, kind) in metrics.items():
                before = previous.get(name)
                if before is None or abs(value - before) < 1e-9:
                    continue
                plain, delta = FORMATS.get(kind, FORMATS["num"])
                since.changes.append((name, plain(before), plain(value), delta(value - before)))
            for name, labels in items.items():
                old = set(self.baseline.get("items", {}).get(name, []))
                if not old and name not in self.baseline.get("items", {}):
                    continue
                added, removed = sorted(set(labels) - old), sorted(old - set(labels))
                if added:
                    since.notes.append(f"{name} added: {', '.join(added)}.")
                if removed:
                    since.notes.append(f"{name} removed: {', '.join(removed)}.")
        if save:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({
                    "ts": pd.Timestamp.now(tz="UTC").isoformat(),
                    "metrics": {name: value for name, (value, _) in metrics.items()},
                    "items": {name: sorted(labels) for name, labels in items.items()},
                }, indent=1))
            except OSError:
                pass  # highlights never fail a notebook over a read-only folder
        return since


def _rows_html(title: str, rows: list[Row], color: str) -> str:
    head = (f"<div style='font-weight:600;margin:10px 0 4px;color:{color}'>{html.escape(title)}</div>")
    if not rows:
        return head + "<div style='margin:0 0 4px;opacity:.75'>Nothing to report.</div>"
    body = "".join(
        f"<tr><td style='padding:3px 12px 3px 8px;border-left:3px solid {color};white-space:nowrap;"
        f"vertical-align:top;font-weight:500'>{html.escape(area)}</td>"
        f"<td style='padding:3px 8px;text-align:left'>{html.escape(text)}</td></tr>"
        for area, text in rows)
    return head + f"<table style='border-collapse:collapse;margin:0'>{body}</table>"


def render(well: list[Row], attention: list[Row], since: SinceLastRun | None = None,
           footnote: str = "") -> str:
    """One HTML block: what stands out, what needs attention, and what changed since the last run."""
    from desk import funding_watch

    attention = funding_watch.consolidate(list(attention))  # one "Funding" row, as on the dashboards
    parts = ["<div style='font-size:13px;line-height:1.45'>",
             _rows_html("WHAT STANDS OUT", well, GOOD), _rows_html("NEEDS ATTENTION", attention, BAD)]
    if since is not None:
        heading = f"font-weight:600;margin:10px 0 4px;color:{NEUTRAL}"
        last = (f"last run {clock(since.last_run)}"
                if since.last_run is not None else "")
        if since.last_run is not None and since.changes:
            # The heading is the table's own caption, as in every desk table.
            header = "".join(f"<th style='padding:3px 10px;text-align:{align}'>{name}</th>"
                             for name, align in (("Metric", "left"), ("Last run", "right"),
                                                 ("Now", "right"), ("Change", "right")))
            body = "".join(
                "<tr>" + "".join(f"<td style='padding:2px 10px;text-align:{'left' if i == 0 else 'right'}'>"
                                 f"{html.escape(cell)}</td>" for i, cell in enumerate(row)) + "</tr>"
                for row in since.changes)
            caption = (f"<caption style='caption-side:top;text-align:left;{heading};padding:0'>"
                       f"SINCE LAST RUN <span style='font-weight:400;opacity:.75'>({last})</span></caption>")
            parts.append(f"<table style='border-collapse:collapse;margin:4px 0'>{caption}<tr>{header}</tr>{body}</table>")
        else:
            parts.append(f"<div style='{heading}'>SINCE LAST RUN</div>")
            if since.last_run is None:
                parts.append("<div style='opacity:.75'>No earlier run saved yet; changes appear from the next run.</div>")
            else:
                parts.append(f"<div style='opacity:.75'>{last[0].upper() + last[1:]}.</div>")
        if since.last_run is not None:
            for note in since.notes:
                parts.append(f"<div>{html.escape(note)}</div>")
            if not since.changes and not since.notes:
                parts.append("<div>No key number changed.</div>")
    if footnote:
        parts.append(f"<div style='margin-top:8px;opacity:.7;font-size:12px'>{html.escape(footnote)}</div>")
    parts.append("</div>")
    return "".join(parts)


def show(well: list[Row], attention: list[Row], since: SinceLastRun | None = None, footnote: str = "") -> None:
    display(HTML(no_math(render(well, attention, since, footnote))))


class HighlightsPanel:
    """Reserve the highlights block where it is created; ``show`` fills it in from a later cell."""

    PLACEHOLDER = ("<div style='font-size:13px;opacity:.7'>Highlights appear here once the sections below "
                   "have run (Run All fills this in).</div>")

    def __init__(self):
        self.handle = display(HTML(no_math(self.PLACEHOLDER)), display_id=True)

    def show(self, well: list[Row], attention: list[Row], since: SinceLastRun | None = None,
             footnote: str = "") -> None:
        block = HTML(no_math(render(well, attention, since, footnote)))
        if self.handle is not None:
            self.handle.update(block)
            print("Highlights updated at the top of the notebook.")
        else:  # outside a notebook display() returns None
            display(block)
