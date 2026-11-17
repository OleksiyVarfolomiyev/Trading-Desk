"""Hypothesis (2026-11-17): large exchange tokens behave differently from other alts, since an exchange can use its token's
price as marketing for investors and users. Test on Binance's archive (daily, 2021 on): BNB against a basket of other large
alts, as beta to BTC, and as how it did on days BTC fell 3% or more (the decoupling the hypothesis predicts: BNB falls less than
the basket) and over the following 5 days. Verdict by the desk's standard: t >= 2 in both halves of the sample, else not
established. One exchange token with a long history (BNB) is a thin sample, and it is said so.

Run: PYTHONPATH=. python research/exchange_token_test.py
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from desk.venues import binance_archive as archive

BASKET = ["ETHUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "ATOMUSDT",
          "NEARUSDT", "UNIUSDT", "AAVEUSDT", "FILUSDT", "ALGOUSDT"]
START = "2021-01-01"
DOWN = -0.03


def closes(symbol: str) -> pd.Series:
    frame = archive.klines(symbol, "1d", START)
    return frame["close"].rename(symbol)


def t_of(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 4 and x.std(ddof=1) > 0 else float("nan")


def main() -> None:
    names = ["BTCUSDT", "BNBUSDT"] + BASKET
    with ThreadPoolExecutor(max_workers=6) as pool:
        prices = pd.concat(list(pool.map(closes, names)), axis=1)
    returns = prices.pct_change(fill_method=None)
    basket = returns[BASKET].mean(axis=1)
    btc, bnb = returns["BTCUSDT"], returns["BNBUSDT"]
    data = pd.DataFrame({"btc": btc, "bnb": bnb, "basket": basket}).dropna()
    half = len(data) // 2
    print(f"{len(data)} days from {data.index[0]:%b %d %Y}; first half to {data.index[half - 1]:%b %Y}")
    for label, part in (("all", data), ("first half", data.iloc[:half]), ("second half", data.iloc[half:])):
        beta = lambda col: float(np.cov(part[col], part["btc"])[0, 1] / part["btc"].var())
        down = part[part["btc"] <= DOWN]
        gap = (down["bnb"] - down["basket"])
        forward = pd.concat([data["bnb"].rolling(5).sum().shift(-5) - data["basket"].rolling(5).sum().shift(-5)], axis=1).iloc[:, 0]
        after = forward.reindex(down.index).dropna()
        print(f"\n{label}: BNB beta {beta('bnb'):.2f} vs basket {beta('basket'):.2f}; vol {part['bnb'].std() * 100:.1f}% vs {part['basket'].std() * 100:.1f}% a day")
        print(f"  BTC down days (<= {DOWN:.0%}), n={len(down)}: BNB {down['bnb'].mean():.2%}, basket {down['basket'].mean():.2%}, "
              f"gap {gap.mean():.2%} (t {t_of(gap):.1f}); next 5 days BNB less basket {after.mean():.2%} (t {t_of(after):.1f})")


if __name__ == "__main__":
    main()
