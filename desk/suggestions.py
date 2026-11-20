"""The log of Claude's trade suggestions, and how they scored against the market and against what was actually done.

A suggestion is a call with a symbol, a side (or "hold"/"reduce"), a size note, the reason and a confidence. Each is logged when
it is made (``python -m desk.suggestions add ...``), never edited afterwards, so the record cannot be flattered. ``score`` reads
the log against Hyperliquid candles (did the price move the suggested way over 1, 3 and 7 days from the suggestion) and the
desk's fills (did the user do it, when, at what size), and prints the hit rate with its sample size. A hit rate on a handful of
calls is not a result: below 20 scored suggestions it says "too few" (the desk's significance rule).

    python -m desk.suggestions add --coin xyz:SHAZ --side short --text "smallest size when there is room" --confidence medium
    python -m desk.suggestions score
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOG = Path(__file__).resolve().parents[1] / "data" / "suggestions.jsonl"
HORIZONS_DAYS = (1, 3, 7)
MIN_SCORED = 20
KINDS = ("trade", "size", "reduce", "hold", "avoid", "info")   # info: analysis that is not a call; never scored


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(coin: str | None, side: str | None, text: str, kind: str = "trade", confidence: str = "medium", at: str | None = None,
        path: Path = LOG) -> dict:
    """Append one suggestion. ``side`` is long or short for a call on direction (the scored kinds); None for the rest."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    row = {"at": at or now_iso(), "coin": coin, "side": (side or "").lower() or None, "kind": kind, "confidence": confidence, "text": text}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def load(path: Path = LOG) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _ms(iso: str) -> int:
    return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _candles(coin: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    from desk.hl.trade_analytics import post_info as _post_info   # the desk HL client (retries on 429)

    rows = _post_info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": start_ms, "endTime": end_ms}})
    return [(int(row["t"]), float(row["c"])) for row in rows]


def price_moves(coin: str, at: str, now_ms: int | None = None) -> dict[int, float | None]:
    """The price move from the suggestion's hour to each horizon, None where the horizon is still in the future."""
    now_ms = now_ms or int(time.time() * 1000)
    start = _ms(at)
    end = min(now_ms, start + (max(HORIZONS_DAYS) + 1) * 86400000)
    series = _candles(coin, start - 3600000, end)
    if not series:
        return {days: None for days in HORIZONS_DAYS}
    base = min(series, key=lambda point: abs(point[0] - start))[1]
    out = {}
    for days in HORIZONS_DAYS:
        target = start + days * 86400000
        if target > now_ms:
            out[days] = None
            continue
        point = min(series, key=lambda p: abs(p[0] - target))
        out[days] = point[1] / base - 1
    return out


def followed(coin: str, side: str, at: str, fills: list[dict], window_days: int = 3) -> dict | None:
    """The first fill in that coin and direction within ``window_days`` after the suggestion, or None (not done)."""
    start, end = _ms(at), _ms(at) + window_days * 86400000
    want = "A" if side == "short" else "B"   # opening a short sells, a long buys
    for fill in sorted(fills, key=lambda f: f["time"]):
        if fill.get("coin") == coin and fill.get("side") == want and start <= int(fill["time"]) <= end and str(fill.get("dir", "")).startswith("Open"):
            return fill
    return None


def score(rows: list[dict] | None = None, fills: list[dict] | None = None, now_ms: int | None = None) -> list[dict]:
    """One result per directional suggestion: the move in the suggested direction at each horizon, and whether it was followed."""
    rows = load() if rows is None else rows
    results = []
    for row in rows:
        if row["kind"] not in ("trade", "avoid") or not row.get("coin") or row.get("side") not in ("long", "short"):
            continue
        try:
            moves = price_moves(row["coin"], row["at"], now_ms)
        except Exception as exc:   # an unreadable market is shown as unscored, never as a miss
            results.append({**row, "moves": {}, "error": f"{type(exc).__name__}"})
            continue
        sign = 1 if row["side"] == "long" else -1
        if row["kind"] == "avoid":
            sign = -sign   # "avoid a long" is right when the price falls
        directional = {days: (None if move is None else sign * move) for days, move in moves.items()}
        fill = followed(row["coin"], row["side"], row["at"], fills or []) if row["kind"] == "trade" else None
        results.append({**row, "moves": directional, "followed": bool(fill), "fill_at": fill and fill["time"]})
    return results


def summary(results: list[dict]) -> dict:
    """Hit rate per horizon (right direction), with the sample size, and how often it was followed. Below MIN_SCORED: too few."""
    out = {"n": len(results), "followed": sum(1 for r in results if r.get("followed")), "horizons": {}}
    for days in HORIZONS_DAYS:
        known = [r["moves"][days] for r in results if r.get("moves", {}).get(days) is not None]
        out["horizons"][days] = {"n": len(known), "hit": (sum(1 for m in known if m > 0) / len(known)) if known else None,
                                 "mean": (sum(known) / len(known)) if known else None}
    out["verdict"] = "too few suggestions scored to say anything" if len(results) < MIN_SCORED else "enough to read, with the usual caution"
    return out


def _fills(wallet_hint: str = "07f2") -> list[dict]:
    from desk.accounts import accounts_for, load_accounts
    from desk.hl.trade_analytics import post_info as _post_info

    out = []
    for account in accounts_for("hl", load_accounts()):
        try:
            out += _post_info({"type": "userFillsByTime", "user": account.id, "startTime": int((time.time() - 21 * 86400) * 1000)})
        except Exception:
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    adder = sub.add_parser("add")
    adder.add_argument("--coin")
    adder.add_argument("--side", choices=["long", "short"])
    adder.add_argument("--kind", default="trade", choices=KINDS)
    adder.add_argument("--confidence", default="medium", choices=["low", "medium", "high"])
    adder.add_argument("--text", required=True)
    adder.add_argument("--at", help="UTC time of the suggestion, YYYY-MM-DDTHH:MM:SSZ (default: now)")
    sub.add_parser("score")
    args = parser.parse_args(argv)
    if args.command == "add":
        print(json.dumps(add(args.coin, args.side, args.text, args.kind, args.confidence, args.at)))
        return 0
    results = score(fills=_fills())
    for r in results:
        moves = ", ".join(f"{d}d {'' if m is None else format(m, '+.1%')}" if m is not None else f"{d}d -" for d, m in r["moves"].items())
        print(f"{r['at'][:10]} {r['kind']:6} {r.get('side') or '':5} {r.get('coin') or '':10} {moves:36} {'followed' if r.get('followed') else 'not done':9} {r['text'][:60]}")
    print(json.dumps(summary(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
