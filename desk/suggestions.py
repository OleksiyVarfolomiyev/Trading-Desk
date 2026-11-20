"""The log of Claude's suggestions, and how they scored against the market and against what was actually done.

A suggestion is a call with a symbol, a side, a kind, a confidence, a ``fit`` and the quote. It is logged when it is made
(``python -m desk.suggestions add ...``) and never edited afterwards, so the record cannot be flattered.

* ``kind``: trade (open a side), avoid (do not open it), reduce (cut an existing position), size (a size view), hold (wait or leave
  it), info (analysis that is not a call). Only trade, avoid and reduce are scored against the price.
* ``fit``: strategy (it applies a rule or thesis the user set: short-crypto regime default, thesis review first, ladders
  take-profit-only, the caps, a standing rule), additional (my own take beyond the user's strategy: a new idea, a carry trade, a sizing
  view, "I'd add X"), process (ops, UI or engineering advice: never scored), unknown (rows logged before this field existed).
* ``score`` reads the log against Hyperliquid candles (did the price move the suggested way over 1, 3 and 7 days from the
  suggestion), the desk's fills (was it done: an opening fill for a trade, a closing one for a reduce, within 3 days) and the
  journal (the strategy LABEL the user chose at execution). ``comparison`` sets suggested-and-followed against
  suggested-and-not-followed against the user's own entries that were never suggested. ``weekly`` gives the cumulative hit rate by
  week and fit. Below 20 scored suggestions it says "too few" (the desk's significance rule); a hit rate on a handful is noise.

    python -m desk.suggestions add --coin xyz:SHAZ --side short --text "..." --confidence medium --fit strategy
    python -m desk.suggestions score
    python -m desk.suggestions publish      # the dashboard tiles
    python -m desk.suggestions report       # research/suggestions_review.md
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "data" / "suggestions.jsonl"
REVIEW = ROOT / "research" / "suggestions_review.md"
HORIZONS_DAYS = (1, 3, 7)
MIN_SCORED = 20
KINDS = ("trade", "size", "reduce", "hold", "avoid", "info")
SCORED_KINDS = ("trade", "avoid", "reduce")
FITS = ("strategy", "additional", "process", "unknown")
CONFIDENCES = ("low", "medium", "high")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(coin: str | None, side: str | None, text: str, kind: str = "trade", confidence: str = "medium", at: str | None = None,
        fit: str = "unknown", ref: str | None = None, path: Path = LOG) -> dict:
    """Append one suggestion (never an edit). ``side`` is long or short: for a trade the side to open, for a reduce the side of the
    position being cut; None for the kinds that are not scored."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if fit not in FITS:
        raise ValueError(f"fit must be one of {', '.join(FITS)}")
    row = {"at": at or now_iso(), "coin": coin, "side": (side or "").lower() or None, "kind": kind, "confidence": confidence,
           "fit": fit, "text": text}
    if ref:
        row["ref"] = ref
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def load(path: Path = LOG) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        row.setdefault("fit", "unknown")   # rows logged before the field existed
    return rows


def _ms(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _hl_name(coin: str) -> str:
    return coin


def _candles(coin: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    from desk.hl.trade_analytics import post_info as _post_info   # the desk HL client (retries on 429)

    rows = _post_info({"type": "candleSnapshot", "req": {"coin": _hl_name(coin), "interval": "1h", "startTime": start_ms, "endTime": end_ms}})
    return [(int(row["t"]), float(row["c"])) for row in rows]


def moves_from_series(series: list[tuple[int, float]], at: str, now_ms: int | None = None) -> dict[int, float | None]:
    """The price move from the suggestion's hour to each horizon, None where the horizon is still in the future."""
    now_ms = now_ms or int(time.time() * 1000)
    start = _ms(at)
    if not series:
        return {**{days: None for days in HORIZONS_DAYS}, "now": None}
    base = min(series, key=lambda point: abs(point[0] - start))[1]
    out = {"now": series[-1][1] / base - 1}   # so far: the latest price against the price at the suggestion (interim, never a verdict)
    for days in HORIZONS_DAYS:
        target = start + days * 86400000
        if target > now_ms:
            out[days] = None
            continue
        point = min(series, key=lambda p: abs(p[0] - target))
        out[days] = point[1] / base - 1
    return out


def price_moves(coin: str, at: str, now_ms: int | None = None) -> dict[int, float | None]:
    now_ms = now_ms or int(time.time() * 1000)
    start = _ms(at)
    end = min(now_ms, start + (max(HORIZONS_DAYS) + 1) * 86400000)
    return moves_from_series(_candles(coin, start - 3600000, end), at, now_ms)


def followed(coin: str, side: str, at: str, fills: list[dict], window_days: int = 3, kind: str = "trade") -> dict | None:
    """The first fill that does what was suggested within ``window_days``: for a trade an opening fill in that direction (a short
    sells, a long buys); for a reduce a closing fill on that side's position (cutting a long sells, cutting a short buys)."""
    start, end = _ms(at), _ms(at) + window_days * 86400000
    if kind == "reduce":
        want, prefix = ("A" if side == "long" else "B"), "Close"
    else:
        want, prefix = ("A" if side == "short" else "B"), "Open"
    for fill in sorted(fills, key=lambda f: f["time"]):
        if fill.get("coin") == coin and fill.get("side") == want and start <= int(fill["time"]) <= end and str(fill.get("dir", "")).startswith(prefix):
            return fill
    return None


def chosen_label(coin: str, side: str, fill_time_ms: int, entries: list[dict]) -> str | None:
    """The strategy label the journal holds for the position that fill opened: same coin (bare name) and side, opened within a day."""
    bare = coin.split(":")[-1].upper()
    best = None
    for entry in entries:
        if str(entry.get("coin", "")).split(":")[-1].upper() != bare or str(entry.get("side", "")).lower() != side:
            continue
        try:
            opened = int(datetime.strptime(entry["opened"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            continue
        gap = abs(opened - fill_time_ms)
        if gap <= 86400000 and (best is None or gap < best[0]):
            best = (gap, entry.get("strategy"))
    return best[1] if best else None


def direction_sign(row: dict) -> int:
    """+1 when the price rising is right, -1 when it falling is right. trade long +1, trade short -1; avoid inverts a trade;
    reduce long is right when the price falls (-1), reduce short when it rises (+1)."""
    long_ = row["side"] == "long"
    if row["kind"] == "trade":
        return 1 if long_ else -1
    if row["kind"] == "avoid":
        return -1 if long_ else 1
    return -1 if long_ else 1   # reduce


def score(rows: list[dict] | None = None, fills: list[dict] | None = None, now_ms: int | None = None, entries: list[dict] | None = None,
          series_for=None) -> list[dict]:
    """One result per scoreable suggestion: its move in the suggested direction at each horizon, whether it was followed and with
    which label. ``series_for(coin, at)`` is for tests (a price series); by default the HL candles are read."""
    rows = _without_approximate_duplicates(load() if rows is None else rows)
    results = []
    for row in rows:
        if row["kind"] not in SCORED_KINDS or row.get("fit") == "process" or not row.get("coin") or row.get("side") not in ("long", "short"):
            continue
        try:
            moves = (moves_from_series(series_for(row["coin"], row["at"]), row["at"], now_ms) if series_for else price_moves(row["coin"], row["at"], now_ms))
        except Exception as exc:   # an unreadable market is shown as unscored, never as a miss
            results.append({**row, "moves": {}, "error": type(exc).__name__, "followed": False, "label": None})
            continue
        sign = direction_sign(row)
        directional = {days: (None if move is None else sign * move) for days, move in moves.items()}
        fill = followed(row["coin"], row["side"], row["at"], fills or [], kind="trade" if row["kind"] == "avoid" else row["kind"])
        did = (fill is None) if row["kind"] == "avoid" else bool(fill)   # avoiding a long is followed when no long was opened
        label = chosen_label(row["coin"], row["side"], int(fill["time"]), entries or []) if fill and row["kind"] == "trade" else None
        results.append({**row, "moves": directional, "followed": did, "fill_at": fill and int(fill["time"]), "label": label})
    return results


def _without_approximate_duplicates(rows: list[dict]) -> list[dict]:
    """A row logged with an approximate time is dropped from scoring when an exact entry for the same call exists the same day."""
    exact = {(r.get("coin"), r.get("side"), r["kind"], r["at"][:10]) for r in rows if "approximate" not in r["text"].lower()}
    return [r for r in rows if "approximate" not in r["text"].lower() or (r.get("coin"), r.get("side"), r["kind"], r["at"][:10]) not in exact]


def hit_stats(moves: list[float]) -> dict:
    """Hit rate (right direction) with a two-sided binomial z against 50%, and the mean move; n is always reported."""
    n = len(moves)
    if not n:
        return {"n": 0, "hit": None, "mean": None, "z": None}
    hits = sum(1 for m in moves if m > 0)
    z = (hits - n / 2) / math.sqrt(n / 4) if n else None
    return {"n": n, "hit": hits / n, "mean": sum(moves) / n, "z": z}


def summary(results: list[dict]) -> dict:
    """Hit rate per horizon with n, how often it was followed; below MIN_SCORED the verdict is 'too few'."""
    out = {"n": len(results), "followed": sum(1 for r in results if r.get("followed")), "horizons": {}}
    for days in HORIZONS_DAYS + ("now",):
        out["horizons"][days] = hit_stats([r["moves"][days] for r in results if r.get("moves", {}).get(days) is not None])
    out["verdict"] = "too few suggestions scored to say anything" if len(results) < MIN_SCORED else "enough to read, with the usual caution"
    return out


def split_by(results: list[dict], key) -> dict:
    groups = defaultdict(list)
    for r in results:
        groups[key(r)].append(r)
    return {name: summary(rows) for name, rows in groups.items()}


def weekly(results: list[dict], horizon: int = 3) -> dict:
    """Cumulative hit rate by week of the suggestion, one series per fit (strategy, additional): [(week, n_cumulative, hit)]."""
    out = {}
    for fit in ("strategy", "additional"):
        rows = sorted((r for r in results if r.get("fit") == fit and r.get("moves", {}).get(horizon) is not None), key=lambda r: r["at"])
        weeks, seen = [], []
        by_week = defaultdict(list)
        for r in rows:
            d = datetime.strptime(r["at"], "%Y-%m-%dT%H:%M:%SZ")
            by_week[f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"].append(r["moves"][horizon])
        for week in sorted(by_week):
            seen += by_week[week]
            weeks.append((week, len(seen), sum(1 for m in seen if m > 0) / len(seen)))
        out[fit] = weeks
    return out


def own_entries(fills: list[dict], suggested: list[dict], now_ms: int | None = None, series_for=None, noisy_min: int = 8) -> list[dict]:
    """The user's own entries that were never suggested: the first opening fill of each coin and side (clusters within a day count
    once), leaving out coins with many fills in the window (a market-making ladder) and anything matching a suggestion. Each carries
    its price move in its own direction at the horizons."""
    counts = defaultdict(int)
    for f in fills:
        counts[f.get("coin")] += 1
    taken = {(r["coin"].split(":")[-1].upper(), r["side"]) for r in suggested if r.get("coin") and r.get("side")}
    firsts = {}
    for f in sorted(fills, key=lambda x: x["time"]):
        if not str(f.get("dir", "")).startswith("Open") or counts[f["coin"]] >= noisy_min:
            continue
        side = "long" if f["side"] == "B" else "short"
        key = (f["coin"], side)
        if key in firsts and int(f["time"]) - firsts[key]["time"] < 86400000:
            continue
        firsts[key] = {"coin": f["coin"], "side": side, "time": int(f["time"])}
    out = []
    for (coin, side), e in firsts.items():
        if (coin.split(":")[-1].upper(), side) in taken:
            continue
        at = datetime.fromtimestamp(e["time"] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            moves = moves_from_series(series_for(coin, at), at, now_ms) if series_for else price_moves(coin, at, now_ms)
        except Exception:
            continue
        sign = 1 if side == "long" else -1
        out.append({"coin": coin, "side": side, "at": at, "moves": {d: (None if m is None else sign * m) for d, m in moves.items()}})
    return out


def comparison(results: list[dict], own: list[dict]) -> dict:
    """The key comparison: suggested and followed, suggested and not followed, the user's own entries never suggested."""
    trades = [r for r in results if r["kind"] in ("trade",)]
    return {"suggested_followed": summary([r for r in trades if r.get("followed")]),
            "suggested_not_followed": summary([r for r in trades if not r.get("followed")]),
            "own_not_suggested": summary(own)}


def _fills() -> list[dict]:
    from desk.accounts import accounts_for, load_accounts
    from desk.hl.trade_analytics import post_info as _post_info

    out = []
    for account in accounts_for("hl", load_accounts()):
        try:
            out += _post_info({"type": "userFillsByTime", "user": account.id, "startTime": int((time.time() - 21 * 86400) * 1000)})
        except Exception:
            continue
    return out


def _journal_entries() -> list[dict]:
    """The journal the desk trades from lives on the server; the Mac's copy may be stale or empty."""
    try:
        text = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "desk", "cat trading-desk/data/trade_journal.json"], capture_output=True, text=True,
                              timeout=20).stdout
        data = json.loads(text)
        return data if isinstance(data, list) else data.get("entries") or data.get("trades") or []
    except Exception:
        from desk import journal

        return journal.load()


def build(now_ms: int | None = None) -> dict:
    """Everything the tiles and the report need: scored rows, the comparison, by-fit and by-label splits, the weekly lines."""
    rows = load()
    fills = _fills()
    results = score(rows, fills, now_ms, entries=_journal_entries())
    own = own_entries(fills, rows, now_ms)
    return {"rows": rows, "results": results, "own": own, "summary": summary(results), "comparison": comparison(results, own),
            "by_fit": split_by(results, lambda r: r.get("fit", "unknown")), "by_label": split_by([r for r in results if r.get("followed") and r["kind"] == "trade"], lambda r: r.get("label") or "unknown"),
            "weekly": weekly(results), "weekly_now": weekly(results, "now")}


def _pct(value, signed: bool = True) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = f"{abs(value) * 100:.1f}".rstrip("0").rstrip(".") if abs(value) * 100 >= 0.05 else ""
    return ("" if not text else (("-" if value < 0 else "") + text + "%"))


def _rate(value) -> str:
    """A hit rate as a whole percent, 0% included (a zero hit rate is a finding, not an absent value)."""
    return "n/a" if value is None else f"{value * 100:.0f}%"


def tiles_payload(data: dict):
    """The 'My suggestions' table (one row per scored call, summary rows after) and the 'over time' figure."""
    import pandas as pd

    table = []
    for r in sorted(data["results"], key=lambda r: r["at"], reverse=True):
        table.append({"Date": r["at"][:10], "Call": f"{r['kind']} {r['side']} {r['coin'].split(':')[-1]}", "Fit": r.get("fit", ""), "Confidence": r["confidence"],
                      "Followed": "yes" if r.get("followed") else "no", "Label": r.get("label") or "",
                      "Since": r["moves"].get("now"), "1d": r["moves"].get(1), "3d": r["moves"].get(3), "7d": r["moves"].get(7)})
    s = data["summary"]
    for days in ("now",) + HORIZONS_DAYS:
        h = s["horizons"][days]
        if h["n"]:
            name = "so far" if days == "now" else f"{days}d"
            table.append({"Date": "", "Call": f"Hit rate {name} (n {h['n']})" + ("" if s["n"] >= MIN_SCORED else ", too few"), "Fit": "", "Confidence": "", "Followed": "",
                          "Label": "", "Since": h["hit"] if days == "now" else None, "1d": h["hit"] if days == 1 else None,
                          "3d": h["hit"] if days == 3 else None, "7d": h["hit"] if days == 7 else None})
    frame = pd.DataFrame(table)
    figure = None
    weekly_data = data["weekly"] if any(data["weekly"].values()) else data.get("weekly_now", {})   # until 3-day outcomes exist: the interim 'so far'
    if any(weekly_data.values()):
        import plotly.graph_objects as go

        figure = go.Figure()
        for fit, color in (("strategy", "#4c78a8"), ("additional", "#d95926")):
            points = weekly_data.get(fit) or []
            if points:
                figure.add_scatter(name=fit, x=[w for w, _, _ in points], y=[h for _, _, h in points], mode="lines+markers", line={"color": color},
                                   text=[f"n {n}" + ("" if n >= MIN_SCORED else ", too few") for _, n, _ in points], hovertemplate="%{x}: %{y:.0%} (%{text})<extra>" + fit + "</extra>")
        figure.update_layout(yaxis={"tickformat": ".0%", "range": [0, 1]}, showlegend=True, meta={"legend": True}, legend={"orientation": "h", "x": 0, "y": 1.1})
    return frame, figure


def publish(push: bool = True) -> list[str]:
    from desk import tile_feed

    data = build()
    frame, figure = tiles_payload(data)
    keys = []
    if len(frame):
        formats = {"Since": "spct", "1d": "spct", "3d": "spct", "7d": "spct"}
        tile_feed.publish(frame, "My suggestions", reason="Claude's calls scored against the market and your execution", relevance=0.1, hours=24 * 14,
                          key="claude-suggestions", formats=formats, push=False)
        keys.append("claude-suggestions")
    if figure is not None:
        interim = not any(data["weekly"].values())
        tile_feed.publish(figure, "My suggestions over time" + (" (so far)" if interim else ""), reason="cumulative hit rate by week, strategy-fit vs additional", relevance=0.1, hours=24 * 14,
                          key="claude-suggestions-weekly", push=False)
        keys.append("claude-suggestions-weekly")
    if push and keys:
        tile_feed.push_to_web()
    return keys


CLASSIFICATION_NOTES = """- **fit = strategy** when the call applies a rule or thesis you set (the caps, the sizing rule, ladders take-profit-only, thesis review first, the short-crypto
  default). The four-shorts hold, the XMR/ENA reduces, the SHAZ call and the CVX size are strategy-fit. The SHAZ call is the ambiguous one: *which* of the four to open
  and at what size is my pick, but re-shorting the four was your decision and the size is the rule's, so it is classed strategy.
- **fit = additional** when it is my own idea beyond your strategy: the BVIV view (a new instrument, a carry-trade framing), the Fear & Greed signal test, the ETH ladder
  'let it run to 200 fills', the NEAR 'leave it', the meme-spike detector.
- **fit = process** for ops, UI and engineering advice ('I'd recommend yes' to a dashboard change): logged for the record, never scored.
- **kind**: trade/avoid/reduce are scored on price; hold and size are capacity or sizing views and cannot be scored on price (a hold that waits for room is not a price call);
  info is analysis or process.
- **confidence**: 'I'd recommend yes' = high, 'I'd' / 'My view' / 'My suggestion' = medium, 'probably' and 'if you want' and options offered without a recommendation = low.
- **followed**: a trade, an opening fill in that coin and direction within 3 days; a reduce, a closing fill on that side within 3 days; an avoid, no opening fill. ENA and
  XMR count as followed for the reduce because both longs were closed within a day (a full close, for a different reason: the longs were accidental), which is more than the
  trim that was suggested.
- **price moves** are measured from the hour of the suggestion in the suggested direction at 1, 3 and 7 days; 'so far' is the move to the latest price and is interim."""


def report_text(data: dict) -> str:
    s = data["summary"]
    lines = ["# Suggestions review", "", f"Generated from desk/suggestions.py (log: data/suggestions.jsonl, append only). Scored suggestions: **{s['n']}** (followed: {s['followed']}). "
             f"Verdict: **{s['verdict']}**.", ""]
    lines += ["## Hit rate (price moved the suggested way)", "", "| Horizon | n | Hit rate | Mean move |", "|---|---|---|---|"]
    for d in ("now",) + HORIZONS_DAYS:
        h = s["horizons"][d]
        lines.append(f"| {'so far (interim)' if d == 'now' else str(d) + 'd'} | {h['n']} | {_rate(h['hit'])} | {_pct(h['mean']) or 'n/a'} |")
    lines += ["", "## Suggested vs the user's own entries (so far / 3d)", "", "| Group | n (so far) | Hit rate so far | Mean so far | n (3d) | Hit 3d |", "|---|---|---|---|---|---|"]
    for name, label in (("suggested_followed", "Suggested and followed"), ("suggested_not_followed", "Suggested, not followed"), ("own_not_suggested", "Own entries, never suggested")):
        now, h3 = data["comparison"][name]["horizons"]["now"], data["comparison"][name]["horizons"][3]
        lines.append(f"| {label} | {now['n']} | {_rate(now['hit'])} | {_pct(now['mean']) or 'n/a'} | {h3['n']} | {_rate(h3['hit'])} |")
    lines += ["", "## By fit (strategy vs additional)", "", "| Fit | n scored | Hit rate so far (n) | Hit rate 3d (n) |", "|---|---|---|---|"]
    for fit, out in data["by_fit"].items():
        now, h3 = out["horizons"]["now"], out["horizons"][3]
        lines.append(f"| {fit} | {out['n']} | {_rate(now['hit'])} ({now['n']}) | {_rate(h3['hit'])} ({h3['n']}) |")
    lines += ["", "## Followed suggestions by the label the user chose", "", "| Label | n | Hit rate so far (n) |", "|---|---|---|"]
    for label, out in data["by_label"].items():
        now = out["horizons"]["now"]
        lines.append(f"| {label} | {out['n']} | {_rate(now['hit'])} ({now['n']}) |")
    if not data["by_label"]:
        lines.append("| (none followed with a label yet) | 0 | n/a |")
    lines += ["", "## Every suggestion logged", "", "| At (UTC) | Kind | Fit | Conf | Coin | Side | Ref | Quote |", "|---|---|---|---|---|---|---|---|"]
    for r in sorted(data["rows"], key=lambda r: r["at"]):
        lines.append(f"| {r['at']} | {r['kind']} | {r.get('fit', '')} | {r['confidence']} | {r.get('coin') or ''} | {r.get('side') or ''} | {r.get('ref', '')} | {r['text'][:200].replace('|', '/')} |")
    lines += ["", "## How the ambiguous ones were classified", "", CLASSIFICATION_NOTES, ""]
    sc = data["by_fit"]
    n_strategy, n_additional = sc.get("strategy", {}).get("n", 0), sc.get("additional", {}).get("n", 0)
    lines += ["## The honest read", "",
              f"- **Too few to say anything.** {s['n']} suggestions are scoreable (trade, avoid, reduce), none has a 1, 3 or 7 day outcome yet (the oldest is under two days old), "
              f"and the desk's rule is 20 scored before a hit rate means anything. The 'so far' column is interim.",
              f"- **Strategy-fit vs additional:** {n_strategy} strategy-fit and {n_additional} additional calls are scoreable. Whether my additional takes add anything beyond your "
              f"strategy cannot be said; the weekly lines in the tile fill in as the horizons arrive.",
              "- **What the record already shows:** most of what I recommended in these sessions was process advice (not scored). The calls on positions were few: two reduces "
              "(XMR, ENA), one trade (SHAZ), one avoid (BVIV), four capacity holds.",
              "- **The SHAZ call** was right in direction so far (SHAZ fell after the call) but you opened it about 16 hours later and after most of that fall; the call named the size "
              "('smallest') and not the timing.",
              "- **Where I was wrong or unclear:** I denied making the SHAZ suggestion a day later; the log now carries its exact quote and time. The 'offered' XMR/ENA trims were not "
              "recommendations and are logged at low confidence for that reason.",
              "- **Not measurable:** hold and size calls (waiting for cap room is not a price call), process advice, and anything I said in prose without a symbol and side.", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    adder = sub.add_parser("add")
    adder.add_argument("--coin")
    adder.add_argument("--side", choices=["long", "short"])
    adder.add_argument("--kind", default="trade", choices=KINDS)
    adder.add_argument("--confidence", default="medium", choices=CONFIDENCES)
    adder.add_argument("--fit", default="unknown", choices=FITS)
    adder.add_argument("--ref")
    adder.add_argument("--text", required=True)
    adder.add_argument("--at", help="UTC time of the suggestion, YYYY-MM-DDTHH:MM:SSZ (default: now)")
    sub.add_parser("score")
    sub.add_parser("publish")
    sub.add_parser("report")
    args = parser.parse_args(argv)
    if args.command == "add":
        print(json.dumps(add(args.coin, args.side, args.text, args.kind, args.confidence, args.at, args.fit, args.ref)))
        return 0
    if args.command == "publish":
        print("published", publish())
        return 0
    data = build()
    if args.command == "report":
        REVIEW.parent.mkdir(parents=True, exist_ok=True)
        REVIEW.write_text(report_text(data))
        print("wrote", REVIEW)
        return 0
    for r in data["results"]:
        moves = ", ".join((f"{'so far' if d == 'now' else str(d) + 'd'} {format(m, '+.1%')}" if m is not None else f"{d}d -") for d, m in r["moves"].items())
        print(f"{r['at'][:10]} {r['kind']:6} {r.get('side') or '':5} {r.get('coin') or '':10} {r.get('fit', ''):10} {moves:38} {'followed' if r.get('followed') else 'not done':9} {r.get('label') or '':12} {r['text'][:50]}")
    print(json.dumps({k: data[k] for k in ("summary", "comparison", "by_fit", "by_label")}, default=str)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
