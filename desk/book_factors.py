"""Factor model of each book's actual daily returns, with split-sample validation.

A single-benchmark regression puts everything the benchmark misses into alpha. The crypto book shorts alts, so
its "alpha against BTC" is mostly the alt market falling against BTC; the stock book holds semiconductors and
growth, so its beta to SPY hides those tilts. Here each book is regressed on the factors it actually trades:

* crypto, every day: BTC, the alt basket against BTC (the 40 most traded HL alts, equal weight), BTC momentum
  (long or short BTC by the sign of its lagged 30-day return);
* stocks, between SPY closes: SPY, growth (QQQ - SPY), small caps (IWM - SPY), semiconductors (SMH - SPY).

Alpha is the intercept, annualised, with Newey-West t-stats (research/factor_model.py). Validation: the model is fit
on the first half of the days and its betas carried into the second half; the out-of-sample alpha is the second
half's return less what those betas predict. An alpha that is not positive with t >= 2 there is not established.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from research.factor_model import fit_factor_regression, newey_west_mean, rolling_betas, split_in_half

STOCK_TILTS = {"Growth (QQQ - SPY)": "QQQ", "Small caps (IWM - SPY)": "IWM", "Semis (SMH - SPY)": "SMH"}
MIN_DAYS = 40          # fewer days than this: an episode, not a model
ROLLING_DAYS = 30


def crypto_factors(btc: pd.Series, days: int = 400) -> pd.DataFrame:
    """Daily crypto factor returns: BTC, the alt basket against BTC, BTC momentum."""
    from desk import market_regime
    from research.factor_model import btc_momentum_factor

    btc = btc.dropna().sort_index()
    btc.index = pd.to_datetime(btc.index, utc=True).normalize()
    btc_returns = btc.pct_change(fill_method=None)
    closes = market_regime.hl_closes(market_regime.alt_universe(), days)
    closes.index = pd.to_datetime(closes.index, utc=True).normalize()
    basket = closes.pct_change(fill_method=None).clip(-0.5, 1.0).mean(axis=1, skipna=True)
    frame = pd.DataFrame({"BTC": btc_returns})
    frame["Alts - BTC"] = basket.reindex(frame.index) - frame["BTC"]
    frame["BTC momentum"] = btc_momentum_factor(frame["BTC"], 0.0)
    return frame.dropna()


def stock_factors(days: int = 400) -> tuple[pd.DataFrame, pd.Series]:
    """Factor returns on SPY's trading days, and SPY's closes (the calendar the book is compounded onto)."""
    from desk import market_regime

    spy = market_regime.yahoo_series("SPY", days)
    spy.index = pd.to_datetime(spy.index).tz_localize("UTC") if spy.index.tz is None else spy.index
    frame = pd.DataFrame({"SPY": spy.pct_change(fill_method=None)})
    for name, ticker in STOCK_TILTS.items():
        series = market_regime.yahoo_series(ticker, days)
        series.index = pd.to_datetime(series.index).tz_localize("UTC") if series.index.tz is None else series.index
        frame[name] = series.reindex(spy.index).pct_change(fill_method=None) - frame["SPY"]
    return frame.dropna(), spy


def on_calendar(book: pd.Series, closes: pd.Series) -> pd.Series:
    """The book's daily returns compounded onto a benchmark's close-to-close intervals (weekends into Monday)."""
    from desk.live_risk import paired_returns

    return paired_returns(book, closes)["book"]


def fit(dependent: pd.Series, factors: pd.DataFrame, periods: int) -> dict:
    """One regression: annualised alpha and its t, betas and t-stats, R-squared, days."""
    model = fit_factor_regression(dependent, factors)
    return {"alpha": model.alpha * periods, "alpha_t": model.alpha_t_stat, "betas": model.betas,
            "t": model.t_stats, "r2": model.r_squared, "days": model.observations}


def validate(dependent: pd.Series, factors: pd.DataFrame, periods: int) -> dict:
    """Fit on the first half, carry the betas into the second: in-sample alphas of each half and the out-of-sample
    alpha (second half less the first half's prediction) with its Newey-West t."""
    data = pd.concat([dependent.rename("y"), factors], axis=1).dropna()
    first, second = split_in_half(data.index)
    early, late = fit(data.loc[first, "y"], data.loc[first, factors.columns], periods), \
        fit(data.loc[second, "y"], data.loc[second, factors.columns], periods)
    predicted = data.loc[second, factors.columns] @ early["betas"]
    mean, t, n = newey_west_mean(data.loc[second, "y"] - predicted)
    return {"first": early, "second": late, "oos_alpha": mean * periods, "oos_t": t, "oos_days": n,
            "split": second[0]}


def verdict(alpha: float, t: float) -> str:
    if not math.isfinite(alpha) or alpha <= 0:
        return "no alpha"
    return "alpha established" if t >= 2 else "not established"


def model_tables(dependent: pd.Series, factors: pd.DataFrame, periods: int) -> tuple[pd.DataFrame, pd.DataFrame,
                                                                                     pd.DataFrame]:
    """(betas table, validation table, rolling betas) for the notebooks."""
    whole = fit(dependent, factors, periods)
    betas = pd.DataFrame({"Beta": whole["betas"], "t": whole["t"]})
    betas.loc["Alpha (/yr)"] = [whole["alpha"], whole["alpha_t"]]
    betas.loc["R²"] = [whole["r2"], np.nan]
    betas.loc["Days"] = [whole["days"], np.nan]
    check = validate(dependent, factors, periods)
    rows = [{"Sample": f"First half (to {check['split'] - pd.Timedelta(days=1):%b %d})", "Days": check["first"]["days"],
             "Alpha (/yr)": check["first"]["alpha"], "t": check["first"]["alpha_t"], "R²": check["first"]["r2"]},
            {"Sample": "Second half", "Days": check["second"]["days"], "Alpha (/yr)": check["second"]["alpha"],
             "t": check["second"]["alpha_t"], "R²": check["second"]["r2"]},
            {"Sample": "Out of sample (first-half betas)", "Days": check["oos_days"], "Alpha (/yr)": check["oos_alpha"],
             "t": check["oos_t"], "R²": np.nan, "Verdict": verdict(check["oos_alpha"], check["oos_t"])}]
    rolling = rolling_betas(dependent, factors, ROLLING_DAYS, ROLLING_DAYS - 5)
    return betas, pd.DataFrame(rows), rolling
