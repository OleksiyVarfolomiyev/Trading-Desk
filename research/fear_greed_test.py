"""Does extreme fear (Crypto Fear & Greed <= 20) predict what BTC and alts do next? The desk's question: after a
sell-off, is extreme fear the cue that shorts are crowded and a bounce is due (2026-11-15: index 10, alts +5-13%)?

Index from alternative.me (daily, published at 00:00 UTC for that day, so the trade enters at that day's open: no
look-ahead). Prices: Binance daily closes (BTC) and the top-40 alt basket (research/binance_rules_test.py). For each
horizon, events are thinned so their windows do not overlap, and each event's forward return is compared with the
average day of the same half (excess), t >= 2 in both halves to pass.

Run: PYTHONPATH=. python research/fear_greed_test.py
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import requests

from desk.venues import binance_archive as ba
from research.binance_rules_test import alt_basket

THRESHOLD = 20
HORIZONS = (1, 3, 7)


def index_history() -> pd.Series:
    data = requests.get("https://api.alternative.me/fng/?limit=0", timeout=60).json()["data"]
    series = pd.Series({pd.Timestamp(int(d["timestamp"]), unit="s", tz="UTC"): int(d["value"]) for d in data})
    return series.sort_index()


def thinned(days: pd.DatetimeIndex, gap: int) -> pd.DatetimeIndex:
    out, last = [], None
    for day in days:
        if last is None or (day - last).days >= gap:
            out.append(day)
            last = day
    return pd.DatetimeIndex(out)


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return x.mean() / x.std() * math.sqrt(len(x)) if len(x) > 2 and x.std() > 0 else float("nan")


def main() -> None:
    fng = index_history()
    btc = ba.klines("BTCUSDT", "1d", "2020-01-01")["open"]          # open of day D = close of D-1
    alts = alt_basket().resample("1D").first()                       # basket level at each day's open
    alts.index = alts.index.normalize()
    prices = pd.DataFrame({"BTC": btc, "Alts": alts}).dropna()
    prices["Alts vs BTC"] = prices["Alts"] / prices["BTC"]
    fng = fng.reindex(prices.index).dropna()
    rows = []
    split = prices.index[len(prices) // 2]
    for label, flags in (("fear <= 20 (any day)", fng <= THRESHOLD),
                         ("first day into fear <= 20", (fng <= THRESHOLD) & (fng.shift(1) > THRESHOLD)),
                         ("greed >= 80 (any day)", fng >= 80)):
        for h in HORIZONS:
            forward = prices.shift(-h) / prices - 1
            days = thinned(flags[flags].index, h)
            for asset in ("BTC", "Alts", "Alts vs BTC"):
                fwd = forward[asset]
                base = fwd.groupby(fwd.index < split).transform("mean")    # the average day in the same half
                excess = (fwd - base).reindex(days).dropna()
                first, second = excess[excess.index < split], excess[excess.index >= split]
                raw = fwd.reindex(days).dropna()
                rows.append({"signal": label, "h (days)": h, "asset": asset, "events": len(excess),
                             "mean": raw.mean(), "excess vs avg day": excess.mean(), "hit": (raw > 0).mean(),
                             "t": tstat(excess), "t first": tstat(first), "t second": tstat(second),
                             "passes": bool(abs(tstat(first)) >= 2 and abs(tstat(second)) >= 2
                                            and np.sign(tstat(first)) == np.sign(tstat(second)))})
    table = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    print(f"index {fng.index.min():%Y-%m-%d} to {fng.index.max():%Y-%m-%d}; days <= {THRESHOLD}: {(fng <= THRESHOLD).sum()} "
          f"of {len(fng)}; halves split {split:%Y-%m-%d}; now {int(fng.iloc[-1])}")
    print(table.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    table.to_csv("research/data/fear_greed_test.csv", index=False)


if __name__ == "__main__":
    main()
