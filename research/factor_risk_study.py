"""Validate the crypto factor risk model (desk/factor_risk.py) on cached HL daily candles only.

    /Users/admin/anaconda3/envs/py314/bin/python research/factor_risk_study.py

Writes research/data/factor_risk_factors.csv (factor significance, whole sample and halves) and
research/data/factor_risk_forecast.csv (risk forecast quality against two baselines).

Rules (desk): a factor is kept only if its mean return has |t| >= 2 with the same sign in both halves. The risk
model is adopted only if it forecasts better than the trailing realised vol of the same portfolio (what the live
ex-ante row is) and has a bias statistic near 1 in both halves.
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
from research.factor_model import newey_west_mean, split_in_half  # noqa: E402

CACHE = ROOT / "data" / "api_cache"
OUT = ROOT / "research" / "data"
HORIZON, STEP, BURN = 20, 5, 250


def cached_returns() -> pd.DataFrame:
    closes = {}
    for path in glob.glob(str(CACHE / "candles_main_*.json")):
        rows = json.load(open(path))["rows"]
        closes[Path(path).stem.removeprefix("candles_main_")] = pd.Series({r["time"]: r["close"] for r in rows})
    frame = pd.DataFrame(closes).sort_index()
    frame.index = pd.to_datetime(frame.index, unit="ms")
    return frame.pct_change(fill_method=None).iloc[1:]


def panel_inputs() -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Returns and point-in-time size from the backfilled research panel (research/backfill_hl_history.py).

    Crypto perps only (the HIP-3 equity perps are stocks). Size and the weights are the trailing 30-day median dollar
    volume, known the day before: a liquidity proxy for market cap, which has no point-in-time history for most coins.
    """
    from desk.hl import signal_research as sr

    panel = {coin: frame for coin, frame in sr.load_panel().items() if ":" not in coin and len(frame) >= 60}
    if len(panel) < 30:
        return None
    frames = sr.frames(panel)
    close = frames["close"]
    close.index = close.index.tz_localize(None)
    volume = frames["dollar_volume"]
    volume.index = volume.index.tz_localize(None)
    returns = close.pct_change(fill_method=None).iloc[1:]
    size = volume.rolling(30, min_periods=20).median().shift(1).reindex(returns.index)
    return returns, size


def current_caps() -> pd.Series:
    table = pd.read_csv(ROOT / "data" / "market_caps.csv")
    table = table[table["date"] == table["date"].max()]
    return table.drop_duplicates("symbol").set_index("symbol")["market_cap"].astype(float)


def factor_table(factors: pd.DataFrame) -> pd.DataFrame:
    first, second = split_in_half(factors.index)
    rows = []
    for name in factors:
        row = {"Factor": name}
        for label, part in (("all", factors[name]), ("first half", factors.loc[first, name]), ("second half", factors.loc[second, name])):
            mean, t, n = newey_west_mean(part)
            row[f"Mean daily ({label})"], row[f"t ({label})"] = mean, t
        row["Kept"] = bool(abs(row["t (first half)"]) >= 2 and abs(row["t (second half)"]) >= 2
                           and np.sign(row["Mean daily (first half)"]) == np.sign(row["Mean daily (second half)"]))
        rows.append(row)
    return pd.DataFrame(rows).set_index("Factor")


def random_portfolios(names: list[str], rng: np.random.Generator, long_short: int, long_only: int) -> list[pd.Series]:
    out = []
    for _ in range(long_short):
        pick = rng.choice(names, 20, replace=False)
        out.append(pd.Series(np.r_[np.full(10, 0.05), np.full(10, -0.05)], index=pick))
    for _ in range(long_only):
        pick = rng.choice(names, 15, replace=False)
        out.append(pd.Series(1 / 15, index=pick))
    return out


def forecast_test(fit: dict, portfolios: list[pd.Series], keep: list[str]) -> pd.DataFrame:
    """For each origin, predicted daily vol (full model, market-only, trailing realised) vs the next 20 days."""
    dates = fit["factors"].index
    returns = fit["returns"]
    records = []
    for pos in range(BURN, len(dates) - HORIZON, STEP):
        origin = dates[pos]
        models = {"Full model": (fit["factors"].columns[1:].tolist(), fr.model_at(fit, pos)),
                  "Market only": ([], fr.model_at(fit, pos, styles=()))}
        nxt = dates[pos + 1]
        exposure = fit["designs"][nxt]
        future = returns.loc[dates[pos + 1: pos + 1 + HORIZON]]
        history = returns.loc[:origin].tail(60)
        for pid, w in enumerate(portfolios):
            w = w[[n for n in w.index if n in exposure.index and future[n].notna().all() and history[n].notna().sum() >= 40]]
            if len(w) < 8:
                continue
            realised = (future[w.index] * w).sum(axis=1).to_numpy()
            preds = {}
            for label, (styles, model) in models.items():
                if model is None:
                    continue
                cov, spec = model
                preds[label] = fr.portfolio_risk(w, exposure[["Market", *styles]], cov, spec)["vol"]
            preds["Trailing 60d vol"] = float((history[w.index] * w).sum(axis=1).std())
            for label, sigma in preds.items():
                if not sigma > 0:
                    continue
                z = realised / sigma
                records.append({"origin": origin, "portfolio": pid, "model": label, "z2": float(np.mean(z**2)),
                                "qlike": float(np.mean(np.log(sigma**2) + realised**2 / sigma**2)),
                                "kind": "long/short" if (w < 0).any() else "long only"})
    return pd.DataFrame(records)


def forecast_summary(records: pd.DataFrame) -> pd.DataFrame:
    origins = np.sort(records["origin"].unique())
    halves = {"first half": origins[: len(origins) // 2], "second half": origins[len(origins) // 2:]}
    rows = []
    for kind in ("long/short", "long only"):
        sub = records[records["kind"] == kind]
        per_origin = sub.groupby(["origin", "model"])[["z2", "qlike"]].mean().unstack("model")
        for label in per_origin["z2"].columns:
            row = {"Portfolios": kind, "Model": label}
            for half, o in halves.items():
                # bias statistic: sqrt(mean z^2); 1.0 = forecast vol matches realised
                row[f"Bias ({half})"] = float(np.sqrt(per_origin["z2"].loc[per_origin.index.isin(o), label].mean()))
            for rival in ("Trailing 60d vol",):
                if label == rival:
                    continue
                diff = (per_origin["qlike"][rival] - per_origin["qlike"][label]).dropna()   # > 0: model beats the rival
                for half, o in halves.items():
                    mean, t, _ = newey_west_mean(diff[diff.index.isin(o)], lags=4)
                    row[f"QLIKE gain vs trailing ({half})"], row[f"t ({half})"] = mean, t
            rows.append(row)
    return pd.DataFrame(rows).set_index(["Portfolios", "Model"])


def main() -> int:
    inputs = panel_inputs()
    if inputs is not None:
        returns, caps = inputs
        print("source: backfilled panel, size = trailing 30-day median dollar volume (point-in-time)")
    else:
        returns, caps = cached_returns(), current_caps()
        print("source: cached candles, size = today's market cap (static)")
    market = returns["BTC"]
    fit = fr.estimate(returns, caps, market)
    factors = fit["factors"]
    print(f"{len(factors)} days, {len(returns.columns)} coins in candles, {len(fit['caps'].index.intersection(returns.columns))} with a size")
    print("average names per day:", round(np.mean([len(d) for d in fit['designs'].values()]), 1))
    table = factor_table(factors)
    pd.set_option("display.width", 220)
    print(table.round(4).to_string())
    table.to_csv(OUT / "factor_risk_factors.csv")
    names = sorted(set(fit["caps"].index) & set(returns.columns))
    rng = np.random.default_rng(7)
    records = forecast_test(fit, random_portfolios(names, rng, 60, 30), names)
    summary = forecast_summary(records)
    print(summary.round(3).to_string())
    summary.to_csv(OUT / "factor_risk_forecast.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
