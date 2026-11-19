"""Drawdown in the fiduciary sense: how far the money at work fell from its high, period by period.

An investor chooses how much drawdown they accept on the money they put in, then reads quarterly and yearly reports of
how deep the drawdown went in each period. This module gives the desk that view, for the desk (HL + IB, no wallet
tokens: like with like with the books) and each book (crypto, stocks):

* ``period_table``: for each month, quarter and year, and over the whole comparison window, the period's return, its
  maximum drawdown (peak to trough inside the period, starting from the close before it), the drawdown at period end
  (from the high-water mark), the longest stretch under water, whether the high was regained, and return over max
  drawdown. Returns are the books' time-weighted returns on trading equity (desk/books.py chart_returns), so deposits
  and withdrawals do not count as gains or losses.
* Every figure is measured from the same first day: the first day all three series exist (matching windows: the crypto
  book starts later than the stocks book, so earlier stocks-only history is left out of the comparison).
* ``tolerance``: the drawdown the investor accepts, from ``desk/fiduciary.json``; empty by default (no number is
  invented). When set, ``alerts`` gives attention rows at 75% of it and at it, and the daily job sends a phone push on
  a new level. Alert only: nothing trades on it.
* ``write_report``: the quarterly or yearly report (research/reports/), with the excess-return drivers for the period.

    python -m desk.drawdown            publish the tile now
    python -m desk.drawdown report Q   write the latest finished quarter's report (Y for the year)
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
CONFIG = Path(__file__).with_name("fiduciary.json")
REPORTS = REPO / "research" / "reports"
ALERT_STATE = REPO / "data" / "drawdown_alert.json"
NAMES = ("desk", "crypto", "stocks")
WARN_SHARE = 0.75   # the early warning: three quarters of the tolerance


# ------------------------------------------------------------------------------------------------------------- series
def desk_returns(returns_of: dict[str, pd.Series], equity: dict[str, pd.Series]) -> pd.Series:
    """The desk's daily time-weighted return: the books' returns weighted by each book's trading equity going into the day
    (a book without a return or equity that day is left out of that day's weights)."""
    days = sorted(set().union(*[set(r.index) for r in returns_of.values()])) if returns_of else []
    out = {}
    for day in days:
        weight = total = 0.0
        for name, returns in returns_of.items():
            r = returns.get(day)
            e = equity.get(name, pd.Series(dtype=float)).get(day)
            if r is None or e is None or not (math.isfinite(r) and math.isfinite(e)) or e <= 0:
                continue
            weight += e
            total += e * r
        if weight > 0:
            out[day] = total / weight
    return pd.Series(out, dtype=float).sort_index()


def load_levels(path: Path | None = None) -> dict[str, pd.Series]:
    """Growth of 1 for the desk and each book, from the hourly books file, cut to the first day all of them exist."""
    from desk.web_sync import BOOKS_FILE

    data = json.loads((path or BOOKS_FILE).read_text())
    raw = dict(data.get("series", {}))
    if data.get("desk_series"):
        raw["desk"] = data["desk_series"]
    levels = {}
    for name in NAMES:
        points = [(day, value) for day, value in raw.get(name, []) if value is not None]
        if len(points) >= 2:
            levels[name] = pd.Series([1 + v for _, v in points], index=pd.to_datetime([d for d, _ in points])).sort_index()
    return common_window(levels)


def common_window(levels: dict[str, pd.Series]) -> dict[str, pd.Series]:
    """Every series from the latest first day to the earliest last day, rebased to 1 on the first day."""
    if not levels:
        return {}
    start = max(s.index.min() for s in levels.values())
    end = min(s.index.max() for s in levels.values())
    out = {}
    for name, s in levels.items():
        cut = s[(s.index >= start) & (s.index <= end)]
        if len(cut) >= 2 and cut.iloc[0] > 0:
            out[name] = cut / cut.iloc[0]
    return out


# ------------------------------------------------------------------------------------------------------------ measures
def drawdowns(levels: pd.Series) -> pd.Series:
    """Drawdown from the high-water mark on each day (0 at a new high, negative under it)."""
    return levels / levels.cummax() - 1


def longest_under_water(levels: pd.Series, start: pd.Timestamp | None = None) -> int:
    """The longest run of days below the high-water mark (the high since the window's first day), counted from ``start``
    on, in calendar days."""
    under = drawdowns(levels) < -1e-9
    if start is not None:
        under = under[under.index >= start]
    best = 0
    start_day = None
    for day, flag in under.items():
        if flag and start_day is None:
            start_day = day
        if not flag and start_day is not None:
            best = max(best, (day - start_day).days)
            start_day = None
    if start_day is not None:
        best = max(best, (under.index[-1] - start_day).days)
    return best


def period_stats(levels: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> dict | None:
    """One period: the path from the close before ``start`` (or the window's first day) through ``end``."""
    before = levels[levels.index < start]
    inside = levels[(levels.index >= start) & (levels.index <= end)]
    if inside.empty:
        return None
    path = pd.concat([before.iloc[-1:], inside]) if len(before) else inside
    if len(path) < 2:
        return None
    period_return = float(path.iloc[-1] / path.iloc[0] - 1)
    max_dd = float(drawdowns(path).min())
    history = levels[levels.index <= end]
    end_dd = float(drawdowns(history).iloc[-1])
    high_day = history.idxmax()
    return {"Return": period_return, "Max DD": max_dd if max_dd < -1e-9 else None,
            "DD at end": end_dd if end_dd < -1e-9 else None,
            "Under water": longest_under_water(history, start) or None,
            "Status": "at high" if end_dd >= -1e-9 else f"under water since {_since(high_day, end)}",
            "Return / max DD": (period_return / -max_dd) if max_dd < -1e-9 else None,
            "from": path.index[0], "to": path.index[-1], "partial": len(before) == 0 and start < levels.index[0]}


def periods(first: pd.Timestamp, last: pd.Timestamp, months: int = 3, quarters: int = 4) -> list[tuple[str, str, pd.Timestamp, pd.Timestamp]]:
    """(kind, label, start, end): the latest months and quarters, every year, and the whole window."""
    out = []
    for p in pd.period_range(first, last, freq="M")[-months:][::-1]:
        out.append(("M", p.start_time.strftime("%b '%y"), p.start_time, min(p.end_time.normalize(), last)))
    for p in pd.period_range(first, last, freq="Q")[-quarters:][::-1]:
        out.append(("Q", f"Q{p.quarter} '{p.start_time:%y}", p.start_time, min(p.end_time.normalize(), last)))
    for p in pd.period_range(first, last, freq="Y")[::-1]:
        out.append(("Y", str(p.year), p.start_time, min(p.end_time.normalize(), last)))
    out.append(("All", f"Since {first:%b %-d '%y}", first, last))
    return out


def period_table(levels: dict[str, pd.Series]) -> list[dict]:
    """Rows for the tile: each period, the desk then the books."""
    if not levels:
        return []
    first = max(s.index.min() for s in levels.values())
    last = min(s.index.max() for s in levels.values())
    rows = []
    for kind, label, start, end in periods(first, last):
        for name in NAMES:
            if name not in levels:
                continue
            stats = period_stats(levels[name], start, end)
            if stats is None:
                continue
            note = f"{stats['from']:%b %-d '%y} to {stats['to']:%b %-d '%y}"
            if end < (pd.Period(start, freq={"M": "M", "Q": "Q", "Y": "Y"}.get(kind, "D")).end_time.normalize() if kind != "All" else end):
                note += ", period to date"
            rows.append({"Period": label, "Book": name, **{k: stats[k] for k in ("Return", "Max DD", "DD at end", "Under water",
                         "Return / max DD", "Status")}, "_kind": kind, "_window": note})
    return rows


# ----------------------------------------------------------------------------------------------------------- tolerance
def tolerance(path: Path | None = None) -> float | None:
    """The drawdown the investor accepts on the desk, as a positive share (0.2 = 20%); None when not set."""
    try:
        value = json.loads((path or CONFIG).read_text()).get("drawdown_tolerance")
    except (OSError, ValueError):
        return None
    if value in (None, ""):
        return None
    value = abs(float(value))
    return value / 100 if value > 1 else value


def alert_level(current_dd: float | None, limit: float | None) -> str | None:
    """'breach' at or past the tolerance, 'warn' from 75% of it, else None."""
    if limit is None or current_dd is None or not math.isfinite(current_dd):
        return None
    depth = -current_dd
    if depth >= limit - 1e-12:
        return "breach"
    if depth >= WARN_SHARE * limit - 1e-12:
        return "warn"
    return None


def alerts(levels: dict[str, pd.Series], limit: float | None) -> list[list[str]]:
    """Attention rows for the desk's drawdown against the tolerance (alert only)."""
    desk = levels.get("desk")
    if desk is None or desk.empty:
        return []
    current = float(drawdowns(desk).iloc[-1])
    level = alert_level(current, limit)
    if level == "breach":
        return [["Drawdown past tolerance", f"Desk {_pct(current)} from its high; tolerance {_pct(-limit)}. Review exposure."]]
    if level == "warn":
        return [["Drawdown nearing tolerance", f"Desk {_pct(current)} from its high; tolerance {_pct(-limit)}."]]
    return []


def notify(levels: dict[str, pd.Series], limit: float | None, state_path: Path | None = None, send=None) -> str | None:
    """Phone push when the alert level changes to warn or breach (once per level); returns the level sent."""
    desk = levels.get("desk")
    if desk is None or desk.empty:
        return None
    level = alert_level(float(drawdowns(desk).iloc[-1]), limit)
    path = state_path or ALERT_STATE
    try:
        last = json.loads(path.read_text()).get("level")
    except (OSError, ValueError):
        last = None
    if level == last:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"level": level, "at": pd.Timestamp.now(tz="UTC").isoformat()}))
    if level is None:
        return None
    rows = alerts(levels, limit)
    if send is None:
        from desk.push import send
    send(rows[0][0], rows[0][1])
    return level


# --------------------------------------------------------------------------------------------------------------- tile
def _pct(value: float) -> str:
    from desk.numbers import pct

    return pct(value)


def _since(day, now) -> str:
    from desk.numbers import since

    return since(day, now)


def underwater_figure(levels: dict[str, pd.Series]):
    import plotly.graph_objects as go

    colors = {"desk": None, "crypto": "#d95926", "stocks": "#3b82f6"}
    figure = go.Figure()
    for name in NAMES:
        if name in levels:
            dd = drawdowns(levels[name])
            line = {"width": 2.4 if name == "desk" else 1.6}
            if colors[name]:
                line["color"] = colors[name]
            figure.add_scatter(name=name, x=[d.strftime("%Y-%m-%d") for d in dd.index], y=dd.round(4).tolist(),
                               mode="lines", line=line)
    figure.update_layout(yaxis={"tickformat": ".0%"}, hovermode="x unified")
    return figure


def publish(push: bool = True, directory: Path | None = None, path: Path | None = None) -> list[str]:
    from desk import tile_feed

    levels = load_levels(path)
    if not levels:
        return []
    limit = tolerance()
    rows = period_table(levels)
    table = pd.DataFrame([{k: v for k, v in row.items() if not k.startswith("_")} for row in rows])
    window = f"{min(s.index.min() for s in levels.values()):%b %-d '%y}"
    reason = (f"drawdown by period, desk (HL + IB) and books, from {window} when all three trade"
              + (f"; tolerance {_pct(-limit)}" if limit else "; no tolerance set"))
    common = {"hours": 48, "relevance": 0.1, "push": False, "directory": directory}
    tile_feed.publish(table, "Drawdown by period", reason=reason, key="drawdown-periods",
                      formats={"Return": "spct", "Max DD": "spct", "DD at end": "spct", "Under water": "num",
                               "Return / max DD": "num"}, attention=alerts(levels, limit), **common)
    tile_feed.publish(underwater_figure(levels), "Drawdown from the high", reason=reason, key="drawdown-underwater", **common)
    if push:
        tile_feed.push_to_web(directory)
    return ["drawdown-periods", "drawdown-underwater"]


# ------------------------------------------------------------------------------------------------------------- report
def finished_period(kind: str, today: pd.Timestamp | None = None) -> pd.Period:
    """The latest quarter or year that has ended."""
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now(tz="UTC")
    if today.tzinfo is not None:
        today = today.tz_localize(None)
    return pd.Period(today, freq="Q" if kind == "Q" else "Y") - 1


def write_report(kind: str = "Q", today: pd.Timestamp | None = None, path: Path | None = None,
                 out_dir: Path | None = None) -> Path | None:
    """Write research/reports/<period>.md: return, max DD vs tolerance, worst month, recovery, excess-return drivers."""
    from desk.web_sync import BOOKS_FILE

    period = finished_period(kind, today)
    levels = load_levels(path)
    if not levels:
        return None
    start, end = period.start_time, period.end_time.normalize()
    limit = tolerance()
    lines = [f"# Desk report {period}", "",
             f"Window: {start:%b %-d, %Y} to {end:%b %-d, %Y}. Returns are time-weighted on trading equity (HL + IB, no "
             f"wallet tokens); every series is measured from {min(s.index.min() for s in levels.values()):%b %-d, %Y}, "
             "when the desk and both books trade.", "",
             "| Book | Return | Max drawdown | Drawdown at end | Longest under water | Status |", "|---|---|---|---|---|---|"]
    for name in NAMES:
        stats = period_stats(levels[name], start, end) if name in levels else None
        if not stats:
            continue
        lines.append(f"| {name} | {_pct(stats['Return'])} | {_pct(stats['Max DD']) if stats['Max DD'] else ''} | "
                     f"{_pct(stats['DD at end']) if stats['DD at end'] else ''} | "
                     f"{str(stats['Under water']) + ' days' if stats['Under water'] else ''} | {stats['Status']} |")
    desk = period_stats(levels["desk"], start, end) if "desk" in levels else None
    lines.append("")
    if limit is None:
        lines.append("Drawdown tolerance: not set (desk/fiduciary.json).")
    elif desk and desk["Max DD"]:
        used = -desk["Max DD"] / limit
        lines.append(f"Drawdown tolerance {_pct(-limit)}: the desk's deepest drawdown used {used:.0%} of it.")
    if "desk" in levels:
        months = [(p, period_stats(levels["desk"], p.start_time, min(p.end_time.normalize(), end)))
                  for p in pd.period_range(start, end, freq="M")]
        months = [(p, s) for p, s in months if s and s["Max DD"]]
        if months:
            worst = min(months, key=lambda item: item[1]["Max DD"])
            lines.append(f"Worst month: {worst[0].start_time:%B %Y}, drawdown {_pct(worst[1]['Max DD'])}.")
    try:
        attribution = json.loads(BOOKS_FILE.read_text()).get("attribution", {})
        window = "Q" if kind == "Q" else "Y"
        parts = {name: attribution.get(name, {}).get(window) for name in ("crypto", "stocks")}
        dates = next((p for p in parts.values() if p), None)
        aligned = dates is not None and abs((pd.Timestamp(dates["start"]) - start).days) <= 7 \
            and abs((pd.Timestamp(dates["end"]) - end).days) <= 7
        if dates is not None and not aligned:   # matching windows: no drivers from a different window than the period
            lines += ["", f"What drove excess return: the books' stored {window} window ({dates['start']} to {dates['end']}) "
                      "does not match this period, so it is left out; the report written at period end carries it."]
        if aligned:
            lines += ["", f"## What drove excess return ({window} window, {dates['start']} to {dates['end']})", "",
                      "| Driver | Crypto vs BTC | Stocks vs SPY |", "|---|---|---|"]
            for driver in next(p for p in parts.values() if p)["parts"]:
                cells = [f"{100 * parts[n]['parts'][driver]:.1f} pp" if parts[n] and abs(parts[n]["parts"].get(driver) or 0) >= 0.0005
                         else "" for n in ("crypto", "stocks")]   # absent or rounding to 0.0: blank
                lines.append(f"| {driver} | {cells[0]} | {cells[1]} |")
            excess = [f"{100 * parts[n]['excess']:.1f} pp" if parts[n] else "" for n in ("crypto", "stocks")]
            lines.append(f"| Excess return | {excess[0]} | {excess[1]} |")
    except (OSError, ValueError, StopIteration):
        pass
    folder = out_dir or REPORTS
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{period}.md"
    target.write_text("\n".join(lines) + "\n")
    return target


def report_due(today: pd.Timestamp | None = None, out_dir: Path | None = None) -> list[str]:
    """The finished quarter and year whose report is not written yet (the daily job writes them)."""
    folder = out_dir or REPORTS
    due = []
    for kind in ("Q", "Y"):
        period = finished_period(kind, today)
        if not (folder / f"{period}.md").exists():
            due.append(kind)
    return due


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["report"]:
        target = write_report(args[1] if len(args) > 1 else "Q")
        print(f"wrote {target}" if target else "no data")
        return 0
    print("published " + ", ".join(publish()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
