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
MANAGED_SINCE = "2026-10-16"   # the repo start: the desk's risk measures (caps, guardrails, the short leg) begin here


def managed_since(path: Path | None = None) -> pd.Timestamp:
    """First day of the managed desk (desk/fiduciary.json "managed_since", the repo start). Before it the book was held
    passively: IB long only, crypto untraded. Reports separate the two eras."""
    try:
        value = json.loads((path or CONFIG).read_text()).get("managed_since")
    except (OSError, ValueError):
        value = None
    return pd.Timestamp(value or MANAGED_SINCE)


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


def load_raw(path: Path | None = None) -> dict[str, pd.Series]:
    """Growth of 1 for the desk and each book over everything the books file has (each series from its own first day)."""
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
    return levels


def load_levels(path: Path | None = None, era: str = "managed", since: pd.Timestamp | None = None) -> dict[str, pd.Series]:
    """Growth of 1 for the desk and each book, cut to the first day all of them exist. ``era`` "managed" (default) starts
    at the close before the managed desk's first day, so its first day's move counts; "passive" is everything before it."""
    levels = load_raw(path)
    since = since if since is not None else managed_since()
    out = {}
    for name, s in levels.items():
        if era == "managed":
            before = s[s.index < since]
            cut = pd.concat([before.iloc[-1:], s[s.index >= since]])
        else:
            cut = s[s.index < since]
        if len(cut) >= 2 and cut.iloc[0] > 0:
            out[name] = cut / cut.iloc[0]
    return common_window(out) if era == "managed" else out


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


def periods(first: pd.Timestamp, last: pd.Timestamp, months: int = 3, quarters: int = 4,
            inception: pd.Timestamp | None = None) -> list[tuple[str, str, pd.Timestamp, pd.Timestamp]]:
    """(kind, label, start, end): the latest months and quarters, every year (only once the window is a year long, else
    the whole window says it), and the whole window, named from ``inception`` when given."""
    out = []
    for p in pd.period_range(first, last, freq="M")[-months:][::-1]:
        out.append(("M", p.start_time.strftime("%b '%y"), p.start_time, min(p.end_time.normalize(), last)))
    for p in pd.period_range(first, last, freq="Q")[-quarters:][::-1]:
        out.append(("Q", f"Q{p.quarter} '{p.start_time:%y}", p.start_time, min(p.end_time.normalize(), last)))
    if (last - first).days >= 365:
        for p in pd.period_range(first, last, freq="Y")[::-1]:
            out.append(("Y", str(p.year), p.start_time, min(p.end_time.normalize(), last)))
    named = inception if inception is not None else first
    out.append(("All", f"Since {named:%b %-d '%y}", first, last))
    return out


def period_table(levels: dict[str, pd.Series], inception: pd.Timestamp | None = None) -> list[dict]:
    """Rows for the tile: each period, the desk then the books."""
    if not levels:
        return []
    first = max(s.index.min() for s in levels.values())
    last = min(s.index.max() for s in levels.values())
    rows = []
    for kind, label, start, end in periods(first, last, inception=inception):
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


# ------------------------------------------------------------------------------------------------ the passive era
def spy_closes(days: int = 800) -> pd.Series:
    """SPY's daily closes from Yahoo (what the passive stock book accepted); empty when it cannot be read."""
    try:
        from desk.live_risk import yahoo_stock_closes

        closes = yahoo_stock_closes("SPY", days)
        closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
        return closes.dropna().sort_index()
    except Exception as exc:
        from desk import degraded

        degraded.report("SPY history", exc)
        return pd.Series(dtype=float)


def passive_stats(levels: pd.Series, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> dict | None:
    start = start if start is not None else levels.index[0]
    end = end if end is not None else levels.index[-1]
    return period_stats(levels, start, end)


def passive_table(path: Path | None = None, since: pd.Timestamp | None = None, spy: pd.Series | None = None) -> list[dict]:
    """The passive era (before the managed desk), each series over its own window: the stock book beside SPY over the same
    window (the drawdown it accepted), then crypto and the desk as held. ``From``/``To`` are the window's first and last day."""
    passive = load_levels(path, era="passive", since=since)
    if not passive:
        return []
    spy = spy_closes() if spy is None else spy
    rows = []

    def row(name, label, levels):
        stats = passive_stats(levels)
        if stats is None:
            return
        rows.append({"Held": label, "From": f"{levels.index[0]:%b %-d '%y}", "To": f"{levels.index[-1]:%b %-d '%y}",
                     "Return": stats["Return"], "Max DD": stats["Max DD"], "DD at end": stats["DD at end"],
                     "Under water": stats["Under water"], "Return / max DD": stats["Return / max DD"]})

    if "stocks" in passive:
        stocks = passive["stocks"]
        row("stocks", "Stocks (IB, long only)", stocks)
        window = spy[(spy.index >= stocks.index[0]) & (spy.index <= stocks.index[-1])] if len(spy) else spy
        if len(window) >= 2:
            row("spy", "SPY, same days", window / window.iloc[0])
    if "crypto" in passive:
        row("crypto", "Crypto (untraded)", passive["crypto"])
    if "desk" in passive:
        row("desk", "Desk as held", passive["desk"])
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
        return [["Drawdown past tolerance", f"Managed desk {_pct(current)} from its high; tolerance {_pct(-limit)}. Review exposure."]]
    if level == "warn":
        return [["Drawdown nearing tolerance", f"Managed desk {_pct(current)} from its high; tolerance {_pct(-limit)}."]]
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


def publish(push: bool = True, directory: Path | None = None, path: Path | None = None, spy: pd.Series | None = None) -> list[str]:
    """Three tiles: the managed desk's drawdown by period and from its high (the desk's record, from the repo start), and
    the passive era before it, shown apart (stocks beside SPY, the drawdown that book accepted)."""
    from desk import tile_feed

    since = managed_since()
    levels = load_levels(path)
    if not levels:
        return []
    limit = tolerance()
    rows = period_table(levels, inception=since)
    table = pd.DataFrame([{k: v for k, v in row.items() if not k.startswith("_")} for row in rows])
    reason = (f"managed desk (HL + IB) and books from {since:%b %-d '%y}, the repo start"
              + (f"; tolerance {_pct(-limit)}" if limit else "; no tolerance set"))
    common = {"hours": 48, "relevance": 0.1, "push": False, "directory": directory}
    formats = {"Return": "spct", "Max DD": "spct", "DD at end": "spct", "Under water": "num", "Return / max DD": "num"}
    tile_feed.publish(table, "Drawdown by period, managed desk", reason=reason, key="drawdown-periods", formats=formats,
                      attention=alerts(levels, limit), **common)
    tile_feed.publish(underwater_figure(levels), "Drawdown from the high, managed desk", reason=reason, key="drawdown-underwater", **common)
    keys = ["drawdown-periods", "drawdown-underwater"]
    passive = passive_table(path, since, spy)
    if passive:
        tile_feed.publish(pd.DataFrame(passive), "Drawdown before the managed desk", key="drawdown-passive", formats=formats,
                          reason=f"passive holding before {since:%b %-d '%y}: IB long only (SPY beside it), crypto untraded; not the desk's record",
                          **common)
        keys.append("drawdown-passive")
    else:
        tile_feed.withdraw("drawdown-passive", push=False, directory=directory)
    if push:
        tile_feed.push_to_web(directory)
    return keys


# ------------------------------------------------------------------------------------------------------------- report
def finished_period(kind: str, today: pd.Timestamp | None = None) -> pd.Period:
    """The latest quarter or year that has ended."""
    today = pd.Timestamp(today) if today is not None else pd.Timestamp.now(tz="UTC")
    if today.tzinfo is not None:
        today = today.tz_localize(None)
    return pd.Period(today, freq="Q" if kind == "Q" else "Y") - 1


def write_report(kind: str = "Q", today: pd.Timestamp | None = None, path: Path | None = None,
                 out_dir: Path | None = None, spy: pd.Series | None = None) -> Path | None:
    """Write research/reports/<period>.md. A period inside the managed era reports the desk: return, max DD vs tolerance,
    worst month, recovery, the excess-return drivers. A period before the repo start is the passive era (IB long only,
    crypto untraded): labelled as such, the stock book beside SPY, with no tolerance or drivers (those are the desk's)."""
    from desk.web_sync import BOOKS_FILE

    period = finished_period(kind, today)
    since = managed_since()
    start, end = period.start_time, period.end_time.normalize()
    if end < since:
        return _write_passive_report(period, start, end, since, path, out_dir, spy)
    levels = load_levels(path)
    if not levels:
        return None
    start = max(start, since)
    limit = tolerance()
    lines = [f"# Desk report {period}: managed desk", "",
             f"Window: {start:%b %-d, %Y} to {end:%b %-d, %Y}. The managed desk begins {since:%b %-d, %Y} (the repo start; earlier "
             "periods are the passive era and are reported apart). Returns are time-weighted on trading equity (HL + IB, no "
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
        lines.append("Drawdown tolerance (managed desk): not set (desk/fiduciary.json).")
    elif desk and desk["Max DD"]:
        used = -desk["Max DD"] / limit
        lines.append(f"Drawdown tolerance {_pct(-limit)} (managed desk): its deepest drawdown used {used:.0%} of it.")
    if "desk" in levels:
        months = [(p, period_stats(levels["desk"], max(p.start_time, start), min(p.end_time.normalize(), end)))
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
    return _save(period, lines, out_dir)


def _save(period: pd.Period, lines: list[str], out_dir: Path | None) -> Path:
    folder = out_dir or REPORTS
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{period}.md"
    target.write_text("\n".join(lines) + "\n")
    return target


def _write_passive_report(period, start, end, since, path, out_dir, spy) -> Path | None:
    """The passive era's period: each series over the period's days it has, the stock book beside SPY (what it accepted)."""
    raw = load_raw(path)
    spy = spy_closes() if spy is None else spy
    lines = [f"# Report {period}: passive era, before the managed desk", "",
             f"Window: {start:%b %-d, %Y} to {end:%b %-d, %Y}. This is before the managed desk ({since:%b %-d, %Y}, the repo "
             "start): IB was held long only and accepted SPY-like drawdowns, crypto was not traded. It is not the desk's record, "
             "so there is no drawdown tolerance or excess-return attribution here. Each row starts on its own first day "
             "(shown); returns are time-weighted.", "",
             "| Held | From | Return | Max drawdown | Drawdown at end | Longest under water | Status |", "|---|---|---|---|---|---|---|"]
    sources = [("Stocks (IB, long only)", raw.get("stocks")), ("SPY, same days", None),
               ("Crypto (untraded)", raw.get("crypto")), ("Desk as held", raw.get("desk"))]
    stocks_stats = None
    for label, levels in sources:
        if label.startswith("SPY"):
            if stocks_stats is None or not len(spy):
                continue
            window = spy[(spy.index >= stocks_stats["from"]) & (spy.index <= stocks_stats["to"])]
            if len(window) < 2:
                continue
            levels = window / window.iloc[0]
            stats = period_stats(levels, levels.index[0], levels.index[-1])
        else:
            stats = period_stats(levels[levels.index < since], start, end) if levels is not None else None
            if label.startswith("Stocks"):
                stocks_stats = stats
        if not stats:
            continue
        lines.append(f"| {label} | {stats['from']:%b %-d, %Y} | {_pct(stats['Return'])} | "
                     f"{_pct(stats['Max DD']) if stats['Max DD'] else ''} | {_pct(stats['DD at end']) if stats['DD at end'] else ''} | "
                     f"{str(stats['Under water']) + ' days' if stats['Under water'] else ''} | {stats['Status']} |")
    if len(lines) <= 8:
        return None
    return _save(period, lines, out_dir)


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
