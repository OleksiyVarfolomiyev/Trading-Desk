"""What alts do when BTC moves 10% (calibrates the stress tile's alt-rally shock, desk/stress.py).

For each window length, every day where BTC's return over the window is at least +10% (or at most -10%) is an episode
(overlapping days within a window are collapsed to the first); the alt basket is the median return of the most traded alts.
Reported per direction: episodes, the basket's median return, the 75th-percentile alt (the one a short would hurt on), the
alts-to-BTC ratio, a t statistic across episodes and the two halves of the sample. Hypothesis rule: t >= 2 in both halves.

    PYTHONPATH=. python research/alt_beta_test.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from desk import market_regime as mr

THRESHOLD = 0.10
WINDOWS = (1, 3, 7)


def episodes(btc: pd.Series, window: int, sign: int) -> list[pd.Timestamp]:
    move = btc.pct_change(window)
    hits = move[(move * sign) >= THRESHOLD].index
    chosen, last = [], None
    for day in hits:
        if last is None or (day - last).days >= window:
            chosen.append(day)
            last = day
    return chosen


def summarize(closes: pd.DataFrame, btc: pd.Series, window: int, sign: int) -> dict:
    days = episodes(btc, window, sign)
    rows = []
    for day in days:
        window_ret = closes.pct_change(window).loc[day].dropna()
        if len(window_ret) < 10:
            continue
        rows.append({"day": day, "btc": btc.pct_change(window).loc[day], "alt": window_ret.median(), "p75": window_ret.quantile(0.75) if sign > 0 else window_ret.quantile(0.25)})
    frame = pd.DataFrame(rows)
    if frame.empty:
        return {}
    ratio = frame["alt"] / frame["btc"]
    half = len(frame) // 2

    def t(series):
        return float(series.mean() / (series.std(ddof=1) / np.sqrt(len(series)))) if len(series) > 2 and series.std(ddof=1) > 0 else float("nan")

    return {"n": len(frame), "btc": frame["btc"].median(), "alt": frame["alt"].median(), "bad_alt": frame["p75"].median(),
            "ratio": ratio.median(), "t_ratio": t(ratio), "t_first": t(ratio.iloc[:half]), "t_second": t(ratio.iloc[half:])}


def main() -> None:
    coins = mr.alt_universe(40)
    closes = mr.hl_closes(coins + ["BTC"], days=900)
    btc = closes.pop("BTC")
    print(f"{closes.shape[1]} alts, {len(closes)} days ({closes.index[0]:%Y-%m-%d} to {closes.index[-1]:%Y-%m-%d})")
    for window in WINDOWS:
        for sign, name in ((1, "BTC up 10%+"), (-1, "BTC down 10%+")):
            result = summarize(closes, btc, window, sign)
            if not result:
                print(f"{window}d {name}: no episodes")
                continue
            print(f"{window}d {name}: {result['n']} episodes, BTC {result['btc']:+.1%}, alt basket {result['alt']:+.1%}, "
                  f"bad-side alt {result['bad_alt']:+.1%}, alts per BTC {result['ratio']:.2f}x "
                  f"(t {result['t_ratio']:.1f}; halves {result['t_first']:.1f} / {result['t_second']:.1f})")


if __name__ == "__main__":
    main()
