"""Order-book wall and spoof signals from Hyperliquid's public ``l2Book``, logged for forward testing.

Rebuilt from the exchange's own public data (never from deepalpha.pro, whose robots.txt forbids scraping).
Every snapshot of a coin's book becomes one row: depth and imbalance near the mid, the biggest resting wall on
each side, and how many walls from the previous snapshot vanished without price coming near them (a spoof proxy).
Rows go to ``research/data/orderbook_signals/<UTC date>.jsonl`` (data, never committed). ``evaluate`` then asks,
with the desk's hypothesis rule, whether a signal predicts the next return net of taker costs: Newey-West t >= 2
in both halves of the log, or it is dropped.

    python -m desk.book_signals log --once                 # one pass over the default coins
    python -m desk.book_signals log --every 300            # a pass every five minutes, until stopped
    python -m desk.book_signals test --horizon 60          # forward test of what has been logged
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk.hl.trade_analytics import post_info  # noqa: E402  (shares the desk's HL client: 429 backoff, retries)
from research.factor_model import SIGNIFICANT_T_STAT, newey_west_mean, split_in_half  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent.parent / "research" / "data" / "orderbook_signals"
DEFAULT_COINS = ["BTC", "ETH", "SOL", "HYPE", "XRP", "DOGE", "SUI", "BNB"]
BAND_BPS = (10.0, 50.0, 100.0)    # depth is measured within these distances of the mid
WALL_MULTIPLE = 5.0               # a level this many times the median level (within 1%) is a wall
SPOOF_DROP = 0.7                  # a wall that lost this share of its size...
SPOOF_NEAR_BPS = 15.0             # ...while the mid stayed farther than this from it was pulled, not hit
PAUSE_S = 0.4                     # between coins in a pass: ~8 coins every few minutes is far under HL's limits
ROUND_TRIP_COST_BPS = 9.0         # taker in and out (4.5 bps each side)


def parse_book(raw: dict[str, Any]) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """(bids, asks) as (price, notional in USD) lists, best first, from an ``l2Book`` answer."""
    sides = raw.get("levels") or [[], []]

    def side(levels):
        return [(float(level["px"]), float(level["px"]) * float(level["sz"])) for level in levels]
    return side(sides[0]), side(sides[1])


def _within(levels: list[tuple[float, float]], mid: float, bps: float) -> list[tuple[float, float]]:
    return [(px, usd) for px, usd in levels if abs(px - mid) / mid * 1e4 <= bps]


def find_walls(levels: list[tuple[float, float]], mid: float) -> list[tuple[float, float]]:
    """Levels within 1% of the mid that are at least WALL_MULTIPLE times the median level there."""
    near = _within(levels, mid, 100.0)
    if len(near) < 5:
        return []
    median = float(np.median([usd for _, usd in near]))
    return [(px, usd) for px, usd in near if median > 0 and usd >= WALL_MULTIPLE * median]


def spoofed(previous_walls: list[tuple[float, float]], levels: list[tuple[float, float]], mid: float) -> int:
    """Walls of the previous snapshot that shrank by SPOOF_DROP or more while the mid stayed away from them."""
    now = dict(levels)
    return sum(1 for px, usd in previous_walls
               if now.get(px, 0.0) <= (1 - SPOOF_DROP) * usd and abs(px - mid) / mid * 1e4 > SPOOF_NEAR_BPS)


def signals(bids, asks, previous: dict | None = None) -> dict[str, float] | None:
    """The signal values of one book snapshot; ``previous`` is this function's ``_walls`` from the snapshot before."""
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    out: dict[str, float] = {"mid": mid, "spread_bps": (asks[0][0] - bids[0][0]) / mid * 1e4}
    for bps in BAND_BPS:
        bid_usd = sum(usd for _, usd in _within(bids, mid, bps))
        ask_usd = sum(usd for _, usd in _within(asks, mid, bps))
        total = bid_usd + ask_usd
        out[f"depth_{int(bps)}bp_usd"] = total
        out[f"imbalance_{int(bps)}bp"] = (bid_usd - ask_usd) / total if total > 0 else 0.0   # >0: bids heavier
    bid_walls, ask_walls = find_walls(bids, mid), find_walls(asks, mid)
    biggest_bid = max(bid_walls, key=lambda w: w[1], default=None)
    biggest_ask = max(ask_walls, key=lambda w: w[1], default=None)
    out["bid_wall_usd"] = biggest_bid[1] if biggest_bid else 0.0
    out["ask_wall_usd"] = biggest_ask[1] if biggest_ask else 0.0
    out["bid_wall_bps"] = abs(biggest_bid[0] - mid) / mid * 1e4 if biggest_bid else float("nan")
    out["ask_wall_bps"] = abs(biggest_ask[0] - mid) / mid * 1e4 if biggest_ask else float("nan")
    scale = out["depth_100bp_usd"] or 1.0
    out["wall_skew"] = (out["bid_wall_usd"] - out["ask_wall_usd"]) / scale        # >0: the bigger wall is a bid
    if previous:
        pulled_bids = spoofed(previous.get("bid", []), bids, mid)
        pulled_asks = spoofed(previous.get("ask", []), asks, mid)
        out["bid_walls_pulled"], out["ask_walls_pulled"] = pulled_bids, pulled_asks
        out["spoof_skew"] = pulled_asks - pulled_bids   # asks pulled = fake selling pressure gone: reads bullish
    out["_walls"] = {"bid": bid_walls, "ask": ask_walls}
    return out


def log_path(when: datetime) -> Path:
    return LOG_DIR / f"{when:%Y-%m-%d}.jsonl"


def snapshot_pass(coins: list[str], state: dict, fetch=post_info, now=None, sleep=time.sleep) -> list[dict]:
    """One pass over the coins: fetch each book, compute signals, return the rows (``state`` carries the walls)."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    rows = []
    for i, coin in enumerate(coins):
        if i:
            sleep(PAUSE_S)
        try:
            raw = fetch({"type": "l2Book", "coin": coin})
        except RuntimeError as error:
            print(f"{stamp} {coin}: skipped ({error})", file=sys.stderr)
            continue
        bids, asks = parse_book(raw)
        values = signals(bids, asks, state.get(coin))
        if values is None:
            continue
        state[coin] = values.pop("_walls")
        rows.append({"time": stamp, "coin": coin, **{k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in values.items()}})
    return rows


def append_rows(rows: list[dict]) -> None:
    if not rows:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    when = datetime.fromisoformat(rows[0]["time"])
    with log_path(when).open("a") as handle:
        handle.writelines(json.dumps(row) + "\n" for row in rows)


def load_log(directory: Path = LOG_DIR) -> pd.DataFrame:
    frames = [pd.read_json(path, lines=True) for path in sorted(directory.glob("*.jsonl"))] if directory.exists() else []
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True)
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    return frame.sort_values(["coin", "time"]).reset_index(drop=True)


def forward_returns(frame: pd.DataFrame, horizon_min: int) -> pd.DataFrame:
    """Add ``fwd``: the mid's return ``horizon_min`` minutes after each snapshot, from later snapshots only."""
    out = []
    for _, part in frame.groupby("coin"):
        part = part.sort_values("time")
        later = part[["time", "mid"]].rename(columns={"time": "later", "mid": "mid_later"})
        target = part.assign(target=part["time"] + pd.Timedelta(minutes=horizon_min))
        merged = pd.merge_asof(target.sort_values("target"), later, left_on="target", right_on="later", direction="forward",
                               tolerance=pd.Timedelta(minutes=max(2, horizon_min // 4)))
        merged["fwd"] = merged["mid_later"] / merged["mid"] - 1
        out.append(merged.drop(columns=["target", "later", "mid_later"]))
    return pd.concat(out, ignore_index=True) if out else frame.assign(fwd=np.nan)


def signal_series(frame: pd.DataFrame, signal: str, cost_bps: float = ROUND_TRIP_COST_BPS) -> pd.Series:
    """Net return per snapshot time of trading the signal's sign: mean over coins of sign * fwd, less the cost."""
    usable = frame.dropna(subset=[signal, "fwd"])
    usable = usable[usable[signal] != 0]
    pnl = np.sign(usable[signal]) * usable["fwd"] - cost_bps / 1e4
    return pnl.groupby(usable["time"]).mean().sort_index()


def evaluate(frame: pd.DataFrame, signals_to_test: list[str], horizon_min: int, min_times: int = 30) -> pd.DataFrame:
    """Per signal: net mean return per trade with a Newey-West t over the whole log and over each half.

    A signal ``survives`` only if the mean is positive with t >= 2 in both halves (the desk's hypothesis rule).
    """
    if frame.empty:
        return pd.DataFrame(columns=["Signal", "Times", "Net bps", "t", "t first half", "t second half", "Verdict"])
    frame = forward_returns(frame, horizon_min)
    step = frame.groupby("coin")["time"].diff().dt.total_seconds().median()   # NaN while each coin has one snapshot
    lags = max(1, horizon_min // max(1, int(step // 60))) if pd.notna(step) else 1
    rows = []
    for name in signals_to_test:
        if name not in frame:
            continue
        series = signal_series(frame, name)
        if len(series) < min_times:
            rows.append({"Signal": name, "Times": len(series), "Net bps": np.nan, "t": np.nan, "t first half": np.nan,
                         "t second half": np.nan, "Verdict": "not enough snapshots yet"})
            continue
        first, second = split_in_half(series.index)
        mean, t, n = newey_west_mean(series, lags)
        m1, t1, _ = newey_west_mean(series.loc[first], lags)
        m2, t2, _ = newey_west_mean(series.loc[second], lags)
        ok = mean > 0 and m1 > 0 and m2 > 0 and min(t1, t2) >= SIGNIFICANT_T_STAT
        rows.append({"Signal": name, "Times": n, "Net bps": mean * 1e4, "t": t, "t first half": t1, "t second half": t2,
                     "Verdict": "survives" if ok else "dropped: not t>=2 in both halves"})
    return pd.DataFrame(rows)


TESTED_SIGNALS = ["imbalance_10bp", "imbalance_50bp", "imbalance_100bp", "wall_skew", "spoof_skew"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    logger = sub.add_parser("log", help="snapshot the books and append signal rows")
    logger.add_argument("--coins", nargs="+", default=DEFAULT_COINS)
    logger.add_argument("--every", type=int, default=300, help="seconds between passes (at least 60)")
    logger.add_argument("--once", action="store_true")
    tester = sub.add_parser("test", help="forward test of the logged signals")
    tester.add_argument("--horizon", type=int, default=60, help="minutes ahead")
    args = parser.parse_args(argv)
    if args.command == "log":
        state: dict = {}
        while True:
            append_rows(snapshot_pass(args.coins, state))
            if args.once:
                return 0
            time.sleep(max(60, args.every))
    result = evaluate(load_log(), TESTED_SIGNALS, args.horizon)
    print(result.to_string(index=False) if not result.empty else "No snapshots logged yet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
