"""Replay a neutral two-sided market maker on HL's 1-minute candles (about 3.5 days): an upper bound for what
quoting both sides around the mid, with no base position, would have made on ETH, MELANIA and LIT.

Each minute the maker quotes one bid and one ask of LEVEL_USD around a reservation price: the mid shifted against
its inventory (SKEW spacings per level held), so it leans back toward flat; inventory stops at MAX_LEVELS either
way. Spacing is the ladder's (ladder.json spacing_atr x daily ATR(14), at least min_spacing_pct of the mid). Fills:

* touch: a quote fills when the candle reaches its price (the upper bound: queue position and fill order unknown);
* through: the candle must trade a tenth of a spacing past it (closer to what a resting post-only quote gets).

Maker fee 1.5 bp (the desk's fills). PnL is cash plus inventory at the last price; spread captured is the FIFO
round trips, inventory drift the rest.

Run: PYTHONPATH=. python research/neutral_mm_replay.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from desk.hl.account_analytics import post
from desk.hl.trade_analytics import fetch_daily_candles
from desk.market_making import round_trip_pnl

COINS = ("ETH", "MELANIA", "LIT")
LEVEL_USD = 11.0
MAX_LEVELS = 5
SKEW = 0.5
MAKER_FEE = 0.00015
CONFIG = Path(__import__("os").environ.get("LADDER_CONFIG", Path.home() / ".trading-desk" / "ladder.json"))


def spacing_for(coin: str, mid: float) -> float:
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    coin_config = {**{k: v for k, v in config.items() if k != "coins"}, **config.get("coins", {}).get(coin, {})}
    daily = fetch_daily_candles(coin, lookback_days=30)
    high, low, close = (daily[c].astype(float) for c in ("high", "low", "close"))
    true_range = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = float(true_range.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    return max(coin_config.get("spacing_atr", 0.05) * atr, mid * coin_config.get("min_spacing_pct", 0.001))


def minutes(coin: str) -> pd.DataFrame:
    end = int(time.time() * 1000)
    rows = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1m", "startTime": end - 5000 * 60_000,
                                                   "endTime": end}}) or []
    frame = pd.DataFrame(rows)[["t", "o", "h", "l", "c"]].astype(float)
    frame["t"] = pd.to_datetime(frame["t"], unit="ms", utc=True)
    return frame


def replay(candles: pd.DataFrame, spacing: float, through: float) -> dict:
    inventory, cash, fills = 0.0, 0.0, []
    for row in candles.itertuples(index=False):
        mid = row.o
        size = LEVEL_USD / mid
        levels = inventory / size
        reservation = mid - levels * SKEW * spacing
        bid, ask = reservation - spacing, reservation + spacing
        bid, ask = min(bid, mid - spacing * 0.5), max(ask, mid + spacing * 0.5)   # never at or through the mid
        if levels < MAX_LEVELS and row.l <= bid - through * spacing:
            inventory += size
            cash -= size * bid * (1 + MAKER_FEE)
            fills.append({"time": row.t, "side": "B", "px": bid, "sz": size})
        if levels > -MAX_LEVELS and row.h >= ask + through * spacing:
            inventory -= size
            cash += size * ask * (1 - MAKER_FEE)
            fills.append({"time": row.t, "side": "A", "px": ask, "sz": size})
    last = float(candles["c"].iloc[-1])
    pnl = cash + inventory * last
    volume = sum(f["px"] * f["sz"] for f in fills)
    spread = round_trip_pnl([{"time": int(f["time"].timestamp() * 1000), "side": f["side"], "px": f["px"],
                              "sz": f["sz"]} for f in fills]) if fills else 0.0
    return {"fills": len(fills), "volume": volume, "pnl": pnl, "spread": spread, "fees": -volume * MAKER_FEE,
            "per_1k": 1000 * pnl / volume if volume else np.nan, "end_inventory": inventory * last}


def main() -> None:
    rows = []
    for coin in COINS:
        candles = minutes(coin)
        mid = float(candles["c"].iloc[-1])
        spacing = spacing_for(coin, mid)
        hours = (candles["t"].iloc[-1] - candles["t"].iloc[0]).total_seconds() / 3600
        move = candles["c"].iloc[-1] / candles["o"].iloc[0] - 1
        for label, through in (("touch", 0.0), ("through", 0.1)):
            result = replay(candles, spacing, through)
            rows.append({"coin": coin, "fill rule": label, "hours": round(hours), "move": move,
                         "spacing (bp)": 1e4 * spacing / mid, **result})
        time.sleep(1.5)
    table = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print(table.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    table.to_csv("research/data/neutral_mm_replay.csv", index=False)


if __name__ == "__main__":
    main()
