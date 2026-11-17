"""Pumps: does the move continue after it is visible, or reverse? Hourly candles of the liquid HL perps, a causal trigger
(a big 4-hour gain on a volume spike), and what the next hours paid long (ride the pump) and short (fade it), net of costs.

    python research/pump_study.py fetch      # saves hourly candles (a few minutes: HL rate limits)
    python research/pump_study.py            # the study
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from desk.cache import cache_dir  # noqa: E402

FILE = cache_dir("api_cache") / "pump_hourly.csv.gz"
DAYS = 200
COST = 0.0025          # round trip: taker fees both ways (0.09%) and slippage on a thin, fast coin (0.16%)
HORIZONS = (1, 4, 12, 24)


def universe(min_dollar_volume: float = 3e6) -> list[str]:
    from desk.hl import signal_research as r

    data = r.frames(r.load_panel())
    liquid = data["dollar_volume"].tail(30).mean()
    return [c for c, v in liquid.sort_values(ascending=False).items() if v >= min_dollar_volume and ":" not in c]


def fetch(coins: list[str]) -> None:
    from desk.hl.trade_analytics import post_info

    have = pd.read_csv(FILE, parse_dates=["time"]) if FILE.exists() else pd.DataFrame()
    done = set(have["coin"]) if len(have) else set()
    frames = [have] if len(have) else []
    end = int(time.time() * 1000)
    for index, coin in enumerate(coins):
        if coin in done:
            continue
        try:
            rows = post_info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": end - DAYS * 86_400_000, "endTime": end}})
        except Exception as exc:
            print(coin, "failed", exc)
            time.sleep(10)
            continue
        frame = pd.DataFrame([{"time": pd.to_datetime(r["t"], unit="ms", utc=True), "coin": coin, "open": float(r["o"]), "high": float(r["h"]),
                               "low": float(r["l"]), "close": float(r["c"]), "volume": float(r["v"]) * float(r["c"])} for r in rows])
        frames.append(frame)
        print(index + 1, len(coins), coin, len(frame), flush=True)
        time.sleep(1.5)
        if (index + 1) % 10 == 0:
            pd.concat(frames).to_csv(FILE, index=False)
    pd.concat(frames).to_csv(FILE, index=False)


def events(frame: pd.DataFrame, gain: float = 0.08, spike: float = 3.0) -> pd.DataFrame:
    """Trigger hours: the last 4 hours gained at least ``gain`` on at least ``spike`` x the usual 4-hour volume; at most one a
    day per coin. Entry at the next hour's open; outcomes are close-to-close from that entry."""
    out = []
    for coin, part in frame.groupby("coin"):
        part = part.sort_values("time").reset_index(drop=True)
        close, volume = part["close"], part["volume"]
        gain4 = close / close.shift(4) - 1
        vol4 = volume.rolling(4).sum()
        usual = vol4.rolling(24 * 7, min_periods=24 * 3).median().shift(4)
        hit = (gain4 >= gain) & (vol4 >= spike * usual)
        last = -999
        for t in np.flatnonzero(hit.to_numpy()):
            if t - last < 24 or t + 1 + max(HORIZONS) >= len(part):
                continue
            last = t
            entry = part["open"].iloc[t + 1]
            row = {"coin": coin, "time": part["time"].iloc[t], "gain4": gain4.iloc[t], "spike": vol4.iloc[t] / usual.iloc[t]}
            for h in HORIZONS:
                row[f"r{h}"] = part["close"].iloc[t + h] / entry - 1
            out.append(row)
    return pd.DataFrame(out)


def summarise(table: pd.DataFrame, label: str) -> None:
    if table.empty:
        print(label, "no events")
        return
    day = table["time"].dt.normalize()
    half = table["time"] >= table["time"].median()
    print(f"\n{label}: {len(table)} events on {day.nunique()} days")
    for h in HORIZONS:
        net = table[f"r{h}"] - COST            # ride the pump: long
        fade = -table[f"r{h}"] - COST          # fade it: short
        def stat(x):
            by_day = x.groupby(day).mean()        # days, not events, are the independent draws
            t = by_day.mean() / (by_day.std(ddof=1) / np.sqrt(len(by_day))) if len(by_day) > 2 and by_day.std() > 0 else np.nan
            return f"{x.mean():+.2%} (hit {(x > 0).mean():.0%}, t {t:+.1f}; halves {x[~half].mean():+.2%} / {x[half].mean():+.2%})"
        print(f"  +{h:>2}h  ride {stat(net)}   fade {stat(fade)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        fetch(universe())
        sys.exit(0)
    frame = pd.read_csv(FILE, parse_dates=["time"])
    print(f"{frame['coin'].nunique()} coins, {frame['time'].min():%Y-%m-%d} to {frame['time'].max():%Y-%m-%d}")
    table = events(frame)
    summarise(table, "all pumps (4h gain >= 8%, volume >= 3x)")
    for low, high in ((0.08, 0.15), (0.15, 0.30), (0.30, 9)):
        summarise(table[(table["gain4"] >= low) & (table["gain4"] < high)], f"4h gain {low:.0%} to {high:.0%}" if high < 9 else "4h gain 30%+")
