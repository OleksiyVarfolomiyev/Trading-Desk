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
HOUR_MS = 3_600_000
FOLLOWUP_MS = 3 * 86_400_000
KINDS = ("trade", "size", "reduce", "hold", "avoid", "rule", "process", "test", "design", "info")
SCORED_KINDS = ("trade", "avoid", "reduce")
FITS = ("strategy", "additional", "process", "unknown")
CONFIDENCES = ("low", "medium", "high")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TRIGGERS = ("asked", "unprompted", "unknown")


def add(coin: str | None, side: str | None, text: str, kind: str = "trade", confidence: str = "medium", at: str | None = None,
        fit: str = "unknown", ref: str | None = None, path: Path = LOG, source: str | None = None, trigger: str | None = None,
        view: str | None = None) -> dict:
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
    if source:
        row["source"] = source   # transcript:<file>:<line> | memory:<file> | research:<file> | git:<hash>
    if trigger:
        row["trigger"] = trigger if trigger in TRIGGERS else "unknown"   # the user asked for a view, or I gave it unprompted
    if view:
        row["view"] = view       # one line: how I would do it, or why not
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


# Rows logged in error stay in the file (it is append only) but are left out of every count and score; each carries its reason.
VOIDED = {("2026-11-05", "The most you could short is 6,761 HEMI"): "a statement of what fits, not a recommendation"}


def load(path: Path = LOG, include_voided: bool = False) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        row.setdefault("fit", "unknown")   # rows logged before the field existed
        for (day, prefix), reason in VOIDED.items():
            if row["at"][:10] == day and row["text"].startswith(prefix):
                row["voided"] = reason
    return rows if include_voided else [r for r in rows if not r.get("voided")]


def _ms(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _hl_name(coin: str) -> str:
    return coin


def _candles(coin: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    from desk.hl.trade_analytics import post_info as _post_info   # the desk HL client (retries on 429)

    rows = _post_info({"type": "candleSnapshot", "req": {"coin": _hl_name(coin), "interval": "1h", "startTime": start_ms, "endTime": end_ms}})
    return [(int(row["T"]), float(row["c"])) for row in rows]


def moves_from_series(series: list[tuple[int, float]], at: str, now_ms: int | None = None) -> dict[int, float | None]:
    """Use the last completed hourly close at each mark, within one hour."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    start = _ms(at)
    prices = sorted((t, p) for t, p in series if t <= now_ms and p > 0 and math.isfinite(p))

    def mark(target):
        return next((p for t, p in reversed(prices) if target - HOUR_MS <= t <= target), None)

    base = mark(start)
    targets = {**{days: start + days * 86_400_000 for days in HORIZONS_DAYS}, "now": now_ms}
    out = dict.fromkeys(targets)
    if base is not None:
        for horizon, target in targets.items():
            close = mark(target) if target <= now_ms else None
            if close is not None:
                out[horizon] = close / base - 1
    return out


def price_moves(coin: str, at: str, now_ms: int | None = None) -> dict[int, float | None]:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    start = _ms(at)
    end = min(now_ms, start + (max(HORIZONS_DAYS) + 1) * 86_400_000)
    if now_ms < start:
        return moves_from_series([], at, now_ms)
    series = _candles(coin, start - 2 * HOUR_MS, end)
    if now_ms > end:
        try:
            series += _candles(coin, now_ms - 2 * HOUR_MS, now_ms)
        except Exception:
            pass   # historical marks still have coverage
    return moves_from_series(series, at, now_ms)


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
          series_for=None, *, fill_coverage: tuple[int, int] | None = None) -> list[dict]:
    """One result per scoreable suggestion: its move in the suggested direction at each horizon, whether it was followed and with
    which label. ``series_for(coin, at)`` is for tests (a price series); by default the HL candles are read."""
    rows = _without_approximate_duplicates(load() if rows is None else rows)
    results = []
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    observed_fills = [f for f in fills or [] if int(f["time"]) <= now_ms]
    for row in rows:
        if row["kind"] not in SCORED_KINDS or row.get("fit") == "process" or not row.get("coin") or row.get("side") not in ("long", "short"):
            continue
        try:
            moves = (moves_from_series(series_for(row["coin"], row["at"]), row["at"], now_ms) if series_for else price_moves(row["coin"], row["at"], now_ms))
        except Exception as exc:   # an unreadable market is shown as unscored, never as a miss
            moves, error = {}, type(exc).__name__
        else:
            error = None
        sign = direction_sign(row)
        directional = {days: (None if move is None else sign * move) for days, move in moves.items()}
        fill = followed(row["coin"], row["side"], row["at"], observed_fills, kind="trade" if row["kind"] == "avoid" else row["kind"])
        at_ms = _ms(row["at"])
        covered = bool(fill_coverage and fill_coverage[0] <= at_ms and at_ms + FOLLOWUP_MS <= min(now_ms, fill_coverage[1]))
        did = (row["kind"] != "avoid") if fill else ((row["kind"] == "avoid") if covered else None)
        label = chosen_label(row["coin"], row["side"], int(fill["time"]), entries or []) if fill and row["kind"] == "trade" else None
        results.append({**row, "moves": directional, "followed": did, "fill_at": fill and int(fill["time"]), "label": label, "error": error})
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
    # the desk's rule is 20 scored per reading: a horizon with fewer outcomes than that is not read, however many calls there are in all
    longest = max((out["horizons"][d]["n"] for d in HORIZONS_DAYS), default=0)
    out["verdict"] = "too few suggestions scored to say anything" if longest < MIN_SCORED else "enough to read, with the usual caution"
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
    last = {}
    firsts = []
    for f in sorted(fills, key=lambda x: x["time"]):
        if not str(f.get("dir", "")).startswith("Open") or counts[f["coin"]] >= noisy_min:
            continue
        side = "long" if f["side"] == "B" else "short"
        key = (f["coin"], side)
        at_ms = int(f["time"])
        if key in last and at_ms - last[key] < 86_400_000:
            continue
        last[key] = at_ms
        firsts.append((f["coin"], side, at_ms))
    out = []
    for coin, side, at_ms in firsts:
        if (coin.split(":")[-1].upper(), side) in taken:
            continue
        at = datetime.fromtimestamp(at_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            "suggested_not_followed": summary([r for r in trades if r.get("followed") is False]),
            "own_not_suggested": summary(own),
            # every directional call (trade, avoid, reduce), for a larger n; an avoid counts as followed when nothing was opened
            "all_followed": summary([r for r in results if r.get("followed")]),
            "all_not_followed": summary([r for r in results if r.get("followed") is False])}


def counts(rows: list[dict]) -> dict:
    """How many suggestions, by kind, by trigger and by month, and how many are scoreable on price."""
    scoreable = [r for r in rows if r["kind"] in SCORED_KINDS and r.get("coin") and r.get("side") in ("long", "short") and r.get("fit") != "process"]
    return {"n": len(rows), "scoreable": len(scoreable),
            "by_kind": dict(sorted(defaultdict(int, {k: sum(1 for r in rows if r["kind"] == k) for k in KINDS}).items(), key=lambda kv: -kv[1])),
            "by_trigger": {t: sum(1 for r in rows if r.get("trigger", "unknown") == t) for t in TRIGGERS},
            "by_month": {m: sum(1 for r in rows if r["at"][:7] == m) for m in sorted({r["at"][:7] for r in rows})},
            "by_fit": {f: sum(1 for r in rows if r.get("fit") == f) for f in FITS}}


def _fill_history(now_ms: int) -> tuple[list[dict], tuple[int, int] | None]:
    from desk.accounts import accounts_for, load_accounts
    from desk.hl.account_analytics import FILLS_PAGE_SIZE
    from desk.hl.trade_analytics import post_info

    start_ms = now_ms - 21 * 86_400_000
    accounts = accounts_for("hl", load_accounts())
    out = []
    coverage = (start_ms, now_ms) if accounts else None
    for account in accounts:
        try:
            batch = post_info({"type": "userFillsByTime", "user": account.id, "startTime": start_ms, "endTime": now_ms})
            if not isinstance(batch, list) or any(not isinstance(f, dict) or not isinstance(f.get("time"), int) for f in batch):
                raise ValueError("invalid fill history")
            out.extend(batch)
            if len(batch) >= FILLS_PAGE_SIZE:
                coverage = None
        except Exception:
            coverage = None
    return out, coverage


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
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    fills, coverage = _fill_history(now_ms)
    results = score(rows, fills, now_ms, entries=_journal_entries(), fill_coverage=coverage)
    own = own_entries(fills, rows, now_ms) if coverage else []
    scored_keys = {(r["at"], r["text"]) for r in results}
    others = [r for r in rows if (r["at"], r["text"]) not in scored_keys]
    from desk.suggestion_funnel import eligible, linked_commits, suggestion_id

    links = linked_commits()
    follow = [{**r, "done": suggestion_id(r) in links,
               "evidence": (links.get(suggestion_id(r)) or {}).get("commit", "")} for r in others if eligible(r)]
    return {"rows": rows, "results": results, "own": own, "counts": counts(rows), "followups": follow, "summary": summary(results), "comparison": comparison(results, own),
            "by_fit": split_by(results, lambda r: r.get("fit", "unknown")), "by_label": split_by([r for r in results if r.get("followed") and r["kind"] == "trade"], lambda r: r.get("label") or "unknown"),
            "weekly": weekly(results), "weekly_now": weekly(results, "now")}


def _pct(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return f"{value:.1%}"


def _rate(value) -> str:
    """A hit rate as a whole percent, 0% included (a zero hit rate is a finding, not an absent value)."""
    return "n/a" if value is None else f"{value * 100:.0f}%"


def followed_text(row: dict) -> str:
    return {True: "yes", False: "no", None: "unknown"}[row.get("followed")]


def tiles_payload(data: dict):
    """The 'My suggestions' table (one row per scored call, summary rows after) and the 'over time' figure."""
    import pandas as pd

    table = []
    for r in sorted(data["results"], key=lambda r: r["at"], reverse=True):
        table.append({"Date": r["at"][:10], "Call": f"{r['kind']} {r['side']} {r['coin'].split(':')[-1]}", "Fit": r.get("fit", ""), "Confidence": r["confidence"],
                      "Followed": followed_text(r), "Label": r.get("label") or "",
                      "Since": r["moves"].get("now"), "1d": r["moves"].get(1), "3d": r["moves"].get(3), "7d": r["moves"].get(7)})
    s = data["summary"]
    for days in ("now",) + HORIZONS_DAYS:
        h = s["horizons"][days]
        if h["n"]:
            name = "so far" if days == "now" else f"{days}d"
            table.append({"Date": "", "Call": f"Hit rate {name} (n {h['n']})" + ("" if h["n"] >= MIN_SCORED else ", too few"), "Fit": "", "Confidence": "", "Followed": "",
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


def log_frame(data: dict):
    """Show each kind's share of the log and the rates within that kind."""
    import pandas as pd

    done = defaultdict(list)
    for r in data["followups"]:
        done[r["kind"]].append(r["done"])
    scored = defaultdict(int)
    for r in data["results"]:
        scored[r["kind"]] += any(value is not None for value in r["moves"].values())
    rows = []
    for kind, n in data["counts"]["by_kind"].items():
        if not n:
            continue
        group = [r for r in data["rows"] if r["kind"] == kind]
        d = done.get(kind) or []
        rows.append({"Kind": kind, "Share of log": n / data["counts"]["n"], "n": n,
                     "Asked": sum(r.get("trigger") == "asked" for r in group) / n,
                     "Unprompted": sum(r.get("trigger") == "unprompted" for r in group) / n,
                     "Scored on price": scored[kind] / n,
                     "Linked commit": (sum(d) / len(d)) if d else None})
    frame = pd.DataFrame(rows)
    return frame


def publish_log(data: dict) -> bool:
    from desk import tile_feed

    frame = log_frame(data)
    if frame.empty:
        return False
    formats = {column: "pct" for column in frame.columns if column not in ("Kind", "n")}
    tile_feed.publish(frame, "Suggestions log",
                      reason="Share of all suggestions. Asked, unprompted and price-scored rates are within each kind. Commit links are a share of eligible follow-ups.",
                      relevance=0.1, hours=24 * 14, key="claude-suggestions-log", formats={**formats, "n": "int"}, push=False)
    return True


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
    if publish_log(data):
        keys.append("claude-suggestions-log")
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
- **followed**: a trade, an opening fill in that coin and direction within 3 days; a reduce, a closing fill on that side within 3 days; an avoid, no opening fill in a fully observed window. Missing or incomplete fill coverage is shown as unknown. ENA and
  XMR count as followed for the reduce because both longs were closed within a day (a full close, for a different reason: the longs were accidental), which is more than the
  trim that was suggested.
- **price moves** are measured from the hour of the suggestion in the suggested direction at 1, 3 and 7 days; 'so far' is the move to the latest price and is interim."""


COVERAGE = """The transcripts cover **2026-11-02 19:52 UTC to 2026-11-20 20:20 UTC** (six session files in three Claude project folders: 2 Crypto-Portfolio-Analytics sessions,
the old Dropbox trading-desk session, and 3 sessions in the current folder). The repo started on **2026-10-16**, so **2026-10-16 to 2026-11-02 is not covered by any transcript**:
whatever I advised in those two weeks is not in this log. Other sources used for the record, only where a recommendation of mine is clear: the memory files (dated by their
modified time; 5 rows), research notes dated by git (4 rows) and git history (only to check whether a suggestion was followed by a matching commit, never to invent one).
data/feedback.jsonl holds your clicks, not my advice, so it is not a source."""


def report_text(data: dict) -> str:
    s, c = data["summary"], data["counts"]
    lines = ["# Suggestions review", "", f"Generated from desk/suggestions.py (log: data/suggestions.jsonl, append only). **{c['n']} suggestions logged**, **{c['scoreable']} scoreable on price**, "
             f"{s['horizons']['now']['n']} scored so far (followed: {s['followed']}). Verdict: **{s['verdict']}**.", "", "## What is covered", "", COVERAGE, "",
             "A suggestion here is any advice I gave in an exchange (a view on what to do: how to size, open/close/hold/avoid, which tool or rule, what to test next, whether a design or "
             "number is right, why something should not be done), asked for or unprompted. Extracted from the assistant messages by sentence (first-person views, 'I'd', 'I recommend', "
             "'my read', 'not worth', 'better to', 'don't ...'), then classified by rule and reviewed on the price calls; recall is not perfect.", ""]
    lines += ["## Counts", "", "| Kind | n | Asked | Unprompted |", "|---|---|---|---|"]
    for kind, n in c["by_kind"].items():
        if n:
            g = [r for r in data["rows"] if r["kind"] == kind]
            lines.append(f"| {kind} | {n} | {sum(1 for r in g if r.get('trigger') == 'asked')} | {sum(1 for r in g if r.get('trigger') == 'unprompted')} |")
    lines += ["", f"By trigger: asked {c['by_trigger']['asked']}, unprompted {c['by_trigger']['unprompted']}, unknown {c['by_trigger']['unknown']} (rows logged before the field). "
              f"By fit: " + ", ".join(f"{k} {v}" for k, v in c["by_fit"].items() if v) + ". By month: " + ", ".join(f"{k} {v}" for k, v in c["by_month"].items()) + ".", ""]
    lines += ["## Hit rate (price moved the suggested way)", "", "| Horizon | n | Hit rate | Mean move | z vs 50% |", "|---|---|---|---|---|"]
    for d in ("now",) + HORIZONS_DAYS:
        h = s["horizons"][d]
        z = "" if h.get("z") is None else f"{h['z']:.1f}"
        lines.append(f"| {'so far (interim)' if d == 'now' else str(d) + 'd'} | {h['n']} | {_rate(h['hit'])} | {_pct(h['mean']) or 'n/a'} | {z} |")
    lines += ["", "## Suggested vs the user's own entries", "", "| Group | n | Hit 1d (n) | Hit 3d (n) | Hit 7d (n) | Hit so far (n) | Mean so far |", "|---|---|---|---|---|---|---|"]
    for name, label in (("suggested_followed", "Suggested trades, followed"), ("suggested_not_followed", "Suggested trades, not followed"), ("all_followed", "All directional calls, followed"),
                        ("all_not_followed", "All directional calls, not followed"), ("own_not_suggested", "Your own entries, never suggested")):
        g = data["comparison"][name]
        cell = lambda d: f"{_rate(g['horizons'][d]['hit'])} ({g['horizons'][d]['n']})"
        lines.append(f"| {label} | {g['n']} | {cell(1)} | {cell(3)} | {cell(7)} | {cell('now')} | {_pct(g['horizons']['now']['mean']) or 'n/a'} |")
    lines += ["", "All of these sit in one regime (a falling crypto market with the desk net short), so the calls and the entries are not independent: a high hit rate for shorts here "
              "says the regime, not skill. Directional calls measured: trade, avoid, reduce. An avoid is followed when no opening fill was found in a fully observed window.", ""]
    lines += ["## By fit (strategy vs additional)", "", "| Fit | n scored | Hit 3d (n) | Hit so far (n) |", "|---|---|---|---|"]
    for fit, out in data["by_fit"].items():
        h3, now = out["horizons"][3], out["horizons"]["now"]
        lines.append(f"| {fit} | {out['n']} | {_rate(h3['hit'])} ({h3['n']}) | {_rate(now['hit'])} ({now['n']}) |")
    lines += ["", "## Followed trades by the label the user chose", "", "| Label | n | Hit 3d (n) | Hit so far (n) |", "|---|---|---|---|"]
    for label, out in data["by_label"].items():
        h3, now = out["horizons"][3], out["horizons"]["now"]
        lines.append(f"| {label} | {out['n']} | {_rate(h3['hit'])} ({h3['n']}) | {_rate(now['hit'])} ({now['n']}) |")
    if not data["by_label"]:
        lines.append("| (none) | 0 | n/a | n/a |")
    lines += ["", "## The rest: was it done?", "", "The table counts explicit links to commits. The suggestion funnel checks deployment timestamps and exact usage targets separately.", "",
              "| Kind | n | With an explicit commit link |", "|---|---|---|"]
    by = defaultdict(list)
    for r in data["followups"]:
        by[r["kind"]].append(r["done"])
    for kind, d in sorted(by.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"| {kind} | {len(d)} | {sum(d)} ({sum(d) / len(d):.0%}) |")
    lines += ["", "## The scored calls", "", "| At | Kind | Side | Coin | Fit | Conf | Followed | Label | so far | 1d | 3d | 7d |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(data["results"], key=lambda r: r["at"]):
        m = r["moves"]
        f = lambda v: "" if v is None else f"{v * 100:+.1f}%"
        lines.append(f"| {r['at'][:16]} | {r['kind']} | {r['side']} | {r['coin']} | {r.get('fit', '')} | {r['confidence']} | {followed_text(r)} | {r.get('label') or ''} | "
                     f"{f(m.get('now'))} | {f(m.get(1))} | {f(m.get(3))} | {f(m.get(7))} |")
    voided = [r for r in load(include_voided=True) if r.get("voided")]
    if voided:
        lines += ["", "## Voided (logged in error, left out of every count)", ""] + [f"- {r['at'][:10]}: {r['text'][:100]} ({r['voided']})" for r in voided]
    lines += ["", "## Classification", "", CLASSIFICATION_NOTES, ""]
    n_s, n_a = data["by_fit"].get("strategy", {}).get("n", 0), data["by_fit"].get("additional", {}).get("n", 0)
    lines += ["## The honest read", "",
              f"- **Coverage:** {c['n']} suggestions in the 18 days the transcripts cover; nothing for 2026-10-16 to 2026-11-02, and the extraction is by sentence, so some advice buried in "
              "long lists or tables is missed.",
              "- **Price calls:** the 1-day, 3-day and 7-day hit rates are in the table with their sample sizes. Each horizon needs at least 20 scores.",
              f"- **Strategy-fit vs additional:** {n_s} strategy-fit and {n_a} additional calls are scored. Compare their hit rates only with their n beside them.",
              "- **Followed vs not followed vs your own entries:** see the table; the groups share one market regime, so differences between them are not evidence of skill.",
              "- **Design, process, rules and tests:** explicit links to commits document implementation. The suggestion funnel checks release and usage evidence.", ""]
    return "\n".join(lines).rstrip() + "\n"


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
    adder.add_argument("--source")
    adder.add_argument("--trigger", choices=list(TRIGGERS))
    adder.add_argument("--view")
    adder.add_argument("--text", required=True)
    adder.add_argument("--at", help="UTC time of the suggestion, YYYY-MM-DDTHH:MM:SSZ (default: now)")
    sub.add_parser("score")
    sub.add_parser("publish")
    sub.add_parser("report")
    args = parser.parse_args(argv)
    if args.command == "add":
        print(json.dumps(add(args.coin, args.side, args.text, args.kind, args.confidence, args.at, args.fit, args.ref, source=args.source, trigger=args.trigger, view=args.view)))
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
        print(f"{r['at'][:10]} {r['kind']:6} {r.get('side') or '':5} {r.get('coin') or '':10} {r.get('fit', ''):10} {moves:38} {followed_text(r):9} {r.get('label') or '':12} {r['text'][:50]}")
    print(json.dumps({k: data[k] for k in ("summary", "comparison", "by_fit", "by_label")}, default=str)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
