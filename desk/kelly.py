"""The constrained long/short full-Kelly portfolio, and how it would have done on the coins the desk traded.

``_optimize_full_kelly`` is the optimizer behind research/portfolio_analytics.ipynb. ``compare_with_book`` asks the
question the desk cares about: had the same money held the traded coins as the Kelly portfolio, instead of what was
done, how would it have gone? Two Kelly lines answer it:

* walk-forward: each month's weights are fit on the days before it only (up to 180), then held for the month, so the
  line is what Kelly would really have produced;
* in-sample: weights fit on the whole window, the line the notebook shows. It looks better than anything tradable,
  because the weights saw the days they are judged on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

KELLY_STATIONARITY_TOL = 1e-9
BOUND = 0.15                 # the notebook's per-asset limit, long or short
MAX_GROSS = 2.5              # ...and with many coins the total stays near the desk's own leverage: each within +/- 2.5 / n
RISK_FREE_ANNUAL = 0.05
MIN_FIT_DAYS = 90
MAX_FIT_DAYS = 180
STEP_DAYS = 30


def _project_weights(weights, lower, upper, budget=1.0, *, fully_invested=False):
    projected=np.clip(weights,lower,upper)
    if not fully_invested and projected.sum()<=budget:
        return projected
    left=np.min(weights-upper)-1.0
    right=np.max(weights-lower)+1.0
    for _ in range(100):
        shift=(left+right)/2.0
        projected=np.clip(weights-shift,lower,upper)
        if projected.sum()>budget:
            left=shift
        else:
            right=shift
    return np.clip(weights-right,lower,upper)


def _optimize_full_kelly(returns, lower, upper, cash_daily_return, max_iterations=20000, *, fully_invested=False):
    """Constrained full-Kelly weights by projected gradient ascent on mean log growth.

    Converged when a unit gradient step, projected onto the constraints, moves no weight by more
    than ``KELLY_STATIONARITY_TOL``. That test does not depend on the step size, which grows large
    along flat directions (near-collinear assets), where step-based tests never settle.
    """
    def objective(weights):
        portfolio_returns=returns@weights+(1.0-weights.sum())*cash_daily_return
        if np.any(portfolio_returns<=-1.0):
            return float('inf')
        return -np.log1p(portfolio_returns).mean()
    def gradient(weights):
        portfolio_returns=returns@weights+(1.0-weights.sum())*cash_daily_return
        marginal_returns=returns-cash_daily_return
        return -(marginal_returns/(1.0+portfolio_returns[:,None])).mean(axis=0)
    def project(weights):
        return _project_weights(weights, lower, upper, fully_invested=fully_invested)

    weights=project(np.full(returns.shape[1],1.0/returns.shape[1]))
    step_size=1.0
    for _ in range(max_iterations):
        current=objective(weights)
        grad=gradient(weights)
        if np.max(np.abs(project(weights-grad)-weights))<KELLY_STATIONARITY_TOL:
            return weights
        trial=step_size
        for _ in range(100):
            candidate=project(weights-trial*grad)
            direction=candidate-weights
            candidate_value=objective(candidate)
            bound=current+grad@direction+(direction@direction)/(2.0*trial)
            if candidate_value<=bound+1e-14:
                break
            trial*=0.5
        else:
            raise RuntimeError('Kelly optimizer could not find a stable step.')
        if np.max(np.abs(direction))<1e-10:
            return candidate
        weights=candidate
        step_size=min(trial*1.2,1e6)
    raise RuntimeError('Kelly optimizer did not converge within the iteration limit.')



def daily_cash() -> float:
    return (1 + RISK_FREE_ANNUAL) ** (1 / 365) - 1


def half_kelly_risk_shares(history: pd.DataFrame, unit_risk: np.ndarray, budget_fraction: float) -> np.ndarray:
    """Blend equal modeled risk with log-optimal allocations to fixed-direction signals."""
    history = history.tail(MAX_FIT_DAYS).dropna()
    if len(history) < MIN_FIT_DAYS:
        raise ValueError(f"Half-Kelly sizing requires {MIN_FIT_DAYS} common completed return observations.")
    scenarios = history.to_numpy(float) * (budget_fraction / unit_risk)
    if not np.isfinite(scenarios).all() or (scenarios <= -1).any():
        raise ValueError("Half-Kelly scenarios contain invalid returns or a loss of all desk equity.")
    full = _optimize_full_kelly(scenarios, 0.0, 1.0, 0.0, fully_invested=True)
    return (full + 1 / history.shape[1]) / 2


def fit_weights(returns: pd.DataFrame) -> pd.Series:
    """Constrained full-Kelly weights (each within +/- BOUND) for the columns of ``returns``."""
    bound = min(BOUND, MAX_GROSS / max(returns.shape[1], 1))
    values = _optimize_full_kelly(returns.fillna(0.0).to_numpy(float), -bound, bound, daily_cash())
    return pd.Series(values, index=returns.columns)


def portfolio_returns(returns: pd.DataFrame, weights: pd.Series) -> pd.Series:
    """Daily return of holding ``weights`` (the rest in cash)."""
    weights = weights.reindex(returns.columns).fillna(0.0)
    return returns.fillna(0.0).to_numpy(float) @ weights.to_numpy(float) + (1.0 - weights.sum()) * daily_cash()


def walk_forward(returns: pd.DataFrame) -> pd.Series:
    """Daily returns of the Kelly portfolio refit every ``STEP_DAYS`` on the days before, held for the days after."""
    pieces = []
    for start in range(MIN_FIT_DAYS, len(returns), STEP_DAYS):
        fit = returns.iloc[max(0, start - MAX_FIT_DAYS):start]
        fit = fit.loc[:, fit.notna().mean() >= 0.95]   # a coin without the days is left out of that fit
        if fit.shape[1] < 3:
            continue
        hold = returns.iloc[start:start + STEP_DAYS][fit.columns]
        weights = fit_weights(fit)
        pieces.append(pd.Series(portfolio_returns(hold, weights), index=hold.index))
    return pd.concat(pieces) if pieces else pd.Series(dtype=float)


def traded_coins(book) -> list[str]:
    """The HL crypto perps the desk has traded: the closed trades' coins and today's positions (no dex stock perps)."""
    from pathlib import Path

    coins = set(book.coins.loc[book.coins["venue"] == "hl", "asset"].astype(str)) if len(book.coins) else set()
    path = Path(__file__).resolve().parents[1] / "data" / "closed_trades_crypto.csv"
    try:
        coins |= set(pd.read_csv(path)["coin"].astype(str))
    except (OSError, ValueError, KeyError):
        pass
    return sorted(coin for coin in coins if ":" not in coin and not coin.startswith("@"))


def summary(returns: pd.Series) -> dict:
    growth = (1 + returns).cumprod()
    return {"Return": float(growth.iloc[-1] - 1), "Volatility (/yr)": float(returns.std(ddof=1) * np.sqrt(365)),
            "Max drawdown": float((growth / growth.cummax() - 1).min()),
            "Sharpe": float((returns.mean() - daily_cash()) / returns.std(ddof=1) * np.sqrt(365)) if returns.std() > 0 else float("nan")}


def compare_with_book(actual: pd.Series, closes: pd.DataFrame, coins: list[str]) -> dict:
    """The desk's daily returns against the Kelly portfolio of ``coins`` (walk-forward and in-sample), on the days all
    three exist. Returns {"lines": DataFrame of daily returns, "table": DataFrame, "coins": [...]} (empty lines when
    there is too little overlap)."""
    prices = closes[[c for c in coins if c in closes.columns]]
    returns = prices.pct_change(fill_method=None).iloc[1:]
    returns = returns.loc[:, returns.notna().sum() >= MIN_FIT_DAYS]
    oos = walk_forward(returns)
    actual = actual.copy()
    actual.index = pd.to_datetime(actual.index, utc=True).normalize()
    days = oos.index.intersection(actual.index)
    if len(days) < 20:
        return {"lines": pd.DataFrame(), "table": pd.DataFrame(), "coins": list(returns.columns)}
    window = returns.loc[days]
    in_sample = pd.Series(portfolio_returns(window, fit_weights(window)), index=days)
    lines = pd.DataFrame({"Desk (actual)": actual.loc[days], "Kelly (walk-forward)": oos.loc[days], "Kelly (in-sample)": in_sample})
    if "BTC" in closes:
        lines["BTC"] = closes["BTC"].pct_change(fill_method=None).reindex(days)
    table = pd.DataFrame({name: summary(lines[name].dropna()) for name in lines}).T
    return {"lines": lines, "table": table.reset_index(names="Line"), "coins": list(returns.columns)}
