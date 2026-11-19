"""Validate the stock factor risk model (desk/stock_risk.py) and test a cross-section on the HIP-3 equity perps.

    /Users/admin/anaconda3/envs/py314/bin/python research/stock_risk_study.py

Part 1: Fama-French exposures of the cached Yahoo closes (listed stocks). Risk forecast on random portfolios, 20 days
ahead, scored by QLIKE against the portfolio's trailing 60-day vol (what the live ex-ante row is). Part 2: the crypto
model's cross-sectional regression on the equity perps' own history. Rules (desk): a factor premium is shown only if
t >= 2 with the same sign in both halves; the model is adopted only if it beats trailing vol in both halves.
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from desk import factor_risk as fr  # noqa: E402
from desk import stock_risk as sk  # noqa: E402
from research.factor_model import newey_west_mean, split_in_half  # noqa: E402
from research.factor_risk_study import factor_table, forecast_summary  # noqa: E402

CACHE = ROOT / "data" / "api_cache"
OUT = ROOT / "research" / "data"
HORIZON, STEP, BURN = 20, 5, 130
NOT_SINGLE_STOCKS = {"SPY", "QQQM", "SCHD", "GLD", "REMX", "KYIV", "SWMR"}   # funds, and the symbolic holdings
NOT_STOCKS = {"CL", "COPPER", "EUR", "GOLD", "JPY", "SILVER", "XYZ100", "BRENTOIL", "NATGAS", "SP500", "JP225"}


def stock_returns() -> pd.DataFrame:
    closes = {}
    for path in glob.glob(str(CACHE / "yahoo_*.json")):
        symbol = Path(path).stem.removeprefix("yahoo_")
        if symbol in NOT_SINGLE_STOCKS:
            continue
        closes[symbol] = pd.Series(json.load(open(path))["closes"])
    frame = pd.DataFrame(closes).sort_index()
    frame.index = pd.to_datetime(frame.index)
    return frame.pct_change(fill_method=None).iloc[1:]


def random_portfolios(names: list[str], rng: np.random.Generator, long_short: int, long_only: int) -> list[pd.Series]:
    out = []
    for kind, count in (("ls", long_short), ("lo", long_only)):
        for _ in range(count):
            size = int(rng.integers(8, min(25, len(names)) + 1))
            picked = list(rng.choice(names, size=size, replace=False))
            w = rng.dirichlet(np.ones(size))
            if kind == "ls":
                signs = np.where(np.arange(size) < size // 2, 1.0, -1.0)
                rng.shuffle(signs)
                w = w * signs
                w = w / np.abs(w).sum()
            out.append(pd.Series(w, index=picked))
    return out


def forecast_test(excess: pd.DataFrame, raw: pd.DataFrame, factors: pd.DataFrame, portfolios: list[pd.Series],
                  models: dict[str, tuple[str, ...]]) -> pd.DataFrame:
    dates = excess.index
    records = []
    for pos in range(BURN, len(dates) - HORIZON, STEP):
        origin = dates[pos]
        fitted = {label: sk.model_at(excess, factors, pos, names) for label, names in models.items()}
        future = raw.iloc[pos + 1: pos + 1 + HORIZON]
        history = raw.iloc[max(0, pos + 1 - 60): pos + 1]
        for pid, w in enumerate(portfolios):
            w = w[[n for n in w.index if future[n].notna().all() and history[n].notna().sum() >= 40]]
            if len(w) < 8:
                continue
            realised = (future[w.index] * w).sum(axis=1).to_numpy()
            preds = {}
            for label, model in fitted.items():
                if model is not None:
                    preds[label] = sk.portfolio_risk(w, *model)["vol"]
            preds["Trailing 60d vol"] = float((history[w.index] * w).sum(axis=1).std())
            for label, sigma in preds.items():
                if not sigma > 0:
                    continue
                records.append({"origin": origin, "portfolio": pid, "model": label, "z2": float(np.mean((realised / sigma) ** 2)),
                                "qlike": float(np.mean(np.log(sigma**2) + realised**2 / sigma**2)),
                                "kind": "long/short" if (w < 0).any() else "long only"})
    return pd.DataFrame(records)


def perp_cross_section() -> None:
    """The crypto model's cross-sectional regression on the equity perps (stocks), if there are enough of them."""
    from desk.hl import signal_research as sr

    panel = {c: f for c, f in sr.load_panel().items() if ":" in c and c.split(":")[1] not in NOT_STOCKS and len(f) >= 120}
    print(f"\nequity perps with >= 120 days: {len(panel)}")
    if len(panel) < 12:
        print("too few names for a cross-section")
        return
    frames = sr.frames(panel)
    close, volume = frames["close"], frames["dollar_volume"]
    for frame in (close, volume):
        frame.index = frame.index.tz_localize(None)
    returns = close.pct_change(fill_method=None).iloc[1:]
    size = volume.rolling(30, min_periods=20).median().shift(1).reindex(returns.index)
    market = returns.mean(axis=1)   # equal-weighted perp basket as the market
    fit = fr.estimate(returns, size, market, styles=("Momentum", "Volatility", "Beta", "Size"), min_names=10)
    print(f"{len(fit['factors'])} days, average names per day {np.mean([len(d) for d in fit['designs'].values()]):.1f}")
    print(factor_table(fit["factors"]).round(4).to_string())


def main() -> int:
    factors = sk.load_factors()
    raw = stock_returns()
    common = raw.index.intersection(factors.index)
    raw, factors = raw.loc[common], factors.loc[common]
    excess = raw.sub(factors["RF"], axis=0)
    print(f"{len(common)} days ({common[0].date()} to {common[-1].date()}), {raw.shape[1]} stocks")
    pd.set_option("display.width", 220)
    table = factor_table(factors[list(sk.FACTORS)])
    print(table.round(4).to_string())
    table.to_csv(OUT / "stock_risk_factors.csv")
    rng = np.random.default_rng(11)
    names = [c for c in raw if raw[c].notna().sum() > len(raw) * 0.8]
    models = {"Fama-French 6": sk.FACTORS, "Market only": ("Mkt-RF",), "FF 3 (market, size, value)": ("Mkt-RF", "SMB", "HML")}
    records = forecast_test(excess, raw, factors, random_portfolios(names, rng, 40, 40), models)
    summary = forecast_summary(records)
    print(summary.round(3).to_string())
    summary.to_csv(OUT / "stock_risk_forecast.csv")
    perp_cross_section()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
