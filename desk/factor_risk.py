"""Cross-sectional factor risk model for the HL perp universe (Barra-style, crypto).

Each day the cross-section of coin returns is regressed on that day's exposures: a market column (all ones) and
style exposures known the day before (momentum, reversal, volatility, beta to BTC, size). The regression is
weighted by sqrt(market cap), exposures are z-scored with a cap-weighted mean of 0 and an equal-weighted
sd of 1. The slopes are the day's factor returns and the residuals are the specific returns.

Risk of a portfolio w: ``x'Fx + sum(w_i^2 s_i^2)`` with ``x = X'w``, F the factor covariance (EWMA, Newey-West lag
correction, eigenvalues floored) and s_i^2 the specific variance (EWMA of squared residuals, shrunk toward the
median of the coin's size bucket).

Sector blocks (optional, ``sectors`` from desk/crypto_sectors.csv): a 0/1 column per sector with at least
MIN_SECTOR names that day, with the usual Barra constraint that the cap-weighted sum of the sector factor returns is
zero, so they sit beside the market column without being collinear with it.

Limits, stated where the numbers are used: the cached candles carry no volume, and market cap history exists for
35 coins only, so size and the regression weights use today's cap (a static ranking; stable but not point-in-time),
unless a point-in-time size frame (for example trailing dollar volume) is given. See research/factor_risk_study.py
for the validation (a factor is shown only if t >= 2 in both halves).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SECTOR_FILE = Path(__file__).resolve().parent / "crypto_sectors.csv"
SECTOR_PREFIX = "Sector: "
MIN_SECTOR = 4
STYLES = ("Momentum", "Reversal", "Volatility", "Beta", "Size")
MIN_NAMES = 20


def winsorize_rows(returns: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """Clip each day's cross-section at its 1st and 99th percentile."""
    low = returns.quantile(lower, axis=1)
    high = returns.quantile(upper, axis=1)
    return returns.clip(lower=low, upper=high, axis=0)


def raw_exposures(returns: pd.DataFrame, caps: pd.Series | pd.DataFrame, market: pd.Series) -> dict[str, pd.DataFrame]:
    """Style characteristics on each date, using returns up to the day before only.

    Momentum: 30-day log return ending 4 days back (skips the last 3 days). Reversal: last 5 days, negated so a
    positive exposure means a recent loser. Volatility: 30-day sd of log returns. Beta: 60-day beta to ``market``.
    Size: log of today's market cap (static; see the module note).
    """
    log = np.log1p(returns)
    momentum = log.rolling(30, min_periods=25).sum().shift(4)
    reversal = -log.rolling(5, min_periods=5).sum().shift(1)
    volatility = log.rolling(30, min_periods=25).std().shift(1)
    mkt = np.log1p(market)
    covariance = log.rolling(60, min_periods=40).cov(mkt)
    beta = covariance.div(mkt.rolling(60, min_periods=40).var(), axis=0).shift(1)
    if isinstance(caps, pd.DataFrame):   # point-in-time size (a date x coin frame)
        size = np.log(caps.reindex(index=returns.index, columns=returns.columns).astype(float))
    else:
        size = pd.DataFrame(np.tile(np.log(caps.reindex(returns.columns).to_numpy(dtype=float)), (len(returns), 1)),
                            index=returns.index, columns=returns.columns)
    return {"Momentum": momentum, "Reversal": reversal, "Volatility": volatility, "Beta": beta, "Size": size}


def load_sectors(path: Path = SECTOR_FILE) -> pd.Series:
    """Coin -> sector, from the reviewable file desk/crypto_sectors.csv."""
    table = pd.read_csv(path)
    return table.set_index("coin")["sector"]


def sector_columns(sectors: pd.Series) -> list[str]:
    return [SECTOR_PREFIX + name for name in sorted(sectors.dropna().unique())]


def sector_dummies(names: list[str], sectors: pd.Series, columns: list[str]) -> pd.DataFrame:
    """0/1 sector columns for ``names``; a sector with fewer than MIN_SECTOR of them today is all zeros."""
    mapped = sectors.reindex(names)
    out = pd.DataFrame({c: (mapped == c.removeprefix(SECTOR_PREFIX)).astype(float) for c in columns}, index=names)
    return out.loc[:, out.sum() >= MIN_SECTOR].reindex(columns=columns, fill_value=0.0)


def standardize(values: pd.Series, weights: pd.Series) -> pd.Series:
    """Cap-weighted mean 0 and equal-weighted sd 1 across the names that have a value; missing become 0."""
    ok = values.notna() & weights.notna()
    if ok.sum() < 3:
        return pd.Series(0.0, index=values.index)
    w = weights[ok] / weights[ok].sum()
    mean = float((values[ok] * w).sum())
    sd = float(values[ok].std(ddof=0))
    if not sd > 0:
        return pd.Series(0.0, index=values.index)
    return ((values - mean) / sd).where(values.notna(), 0.0)


def design(exposures: dict[str, pd.DataFrame], date, weights: pd.Series, names: list[str], styles: tuple[str, ...]) -> pd.DataFrame:
    """Exposure matrix (names x [Market, styles]) for one date."""
    frame = {"Market": pd.Series(1.0, index=names)}
    for style in styles:
        frame[style] = standardize(exposures[style].loc[date, names], weights[names])
    return pd.DataFrame(frame)


def constrained_wls(x: pd.DataFrame, y: np.ndarray, root: np.ndarray, caps: np.ndarray, sector_cols: list[str]) -> tuple[np.ndarray, int, int] | None:
    """WLS with the cap-weighted sum of the present sector slopes forced to zero. Returns (slopes, rank, free columns)."""
    present = [c for c in sector_cols if x[c].sum() > 0]
    values = x.to_numpy(dtype=float)
    if len(present) < 2:
        keep = [c for c in x.columns if c not in sector_cols]
        design_free = x[keep].to_numpy(dtype=float)
        coefficients, _, rank, _ = np.linalg.lstsq(design_free * root[:, None], y * root, rcond=None)
        full = pd.Series(0.0, index=x.columns)
        full[keep] = coefficients
        return full.to_numpy(), rank, design_free.shape[1]
    sector_weight = {c: float((x[c].to_numpy() * caps).sum()) for c in present}
    last = present[-1]
    free = x.drop(columns=[last]).copy()
    for c in present[:-1]:
        free[c] = x[c] - sector_weight[c] / sector_weight[last] * x[last]
    free = free.drop(columns=[c for c in sector_cols if c not in present])
    coefficients, _, rank, _ = np.linalg.lstsq(free.to_numpy(dtype=float) * root[:, None], y * root, rcond=None)
    slopes = pd.Series(coefficients, index=free.columns)
    full = pd.Series(0.0, index=x.columns)
    full[slopes.index] = slopes
    full[last] = -sum(sector_weight[c] * slopes[c] for c in present[:-1]) / sector_weight[last]
    return full.to_numpy(), rank, free.shape[1]


def estimate(returns: pd.DataFrame, caps: pd.Series | pd.DataFrame, market: pd.Series, styles: tuple[str, ...] = STYLES,
             min_names: int = MIN_NAMES, sectors: pd.Series | None = None) -> dict:
    """Daily cross-sectional WLS. Returns factor returns, specific returns and the per-date exposure matrices.

    ``caps`` is a Series (today's cap, a static ranking) or a date x coin frame (point-in-time size, known the day before,
    for example trailing dollar volume); a frame's last row stands in for the size buckets of the specific risk.
    """
    point_in_time = isinstance(caps, pd.DataFrame)
    caps_by_date = caps.reindex(index=returns.index).where(lambda f: f > 0) if point_in_time else None
    caps = caps_by_date.ffill().iloc[-1].dropna() if point_in_time else caps.dropna()
    returns = returns[[c for c in returns.columns if c in caps.index]]
    clean = winsorize_rows(returns)
    exposures = raw_exposures(returns, caps_by_date if point_in_time else caps, market)
    sqrt_cap = np.sqrt(caps)
    factors, specific, designs = {}, {}, {}
    sector_cols = sector_columns(sectors) if sectors is not None else []
    for date in clean.index:
        y = clean.loc[date]
        usable = [c for c in clean.columns if pd.notna(y[c]) and all(pd.notna(exposures[s].loc[date, c]) for s in styles if s != "Size")]
        if point_in_time:   # a name with no size known that day cannot be weighted
            usable = [c for c in usable if pd.notna(caps_by_date.loc[date, c])]
        if len(usable) < min_names:
            continue
        day_caps = caps_by_date.loc[date] if point_in_time else sqrt_cap**2
        x = design(exposures, date, day_caps, usable, styles)
        root = np.sqrt(np.sqrt(day_caps[usable].to_numpy(dtype=float)))
        if sector_cols:
            x = pd.concat([x[["Market"]], sector_dummies(usable, sectors, sector_cols), x[list(styles)]], axis=1)
            solved = constrained_wls(x, y[usable].to_numpy(dtype=float), root, day_caps[usable].to_numpy(dtype=float), sector_cols)
            coefficients, rank, needed = solved
        else:
            coefficients, _, rank, _ = np.linalg.lstsq(x.to_numpy() * root[:, None], y[usable].to_numpy(dtype=float) * root, rcond=None)
            needed = x.shape[1]
        if rank < needed:
            continue
        factors[date] = pd.Series(coefficients, index=x.columns)
        specific[date] = y[usable] - x.to_numpy() @ coefficients
        designs[date] = x
    return {"factors": pd.DataFrame(factors).T, "specific": pd.DataFrame(specific).T.reindex(columns=returns.columns),
            "designs": designs, "caps": caps, "exposures": exposures, "returns": clean}


def ewma_weights(n: int, halflife: float) -> np.ndarray:
    weights = 0.5 ** (np.arange(n)[::-1] / halflife)
    return weights / weights.sum()


def factor_covariance(factor_returns: pd.DataFrame, halflife: float = 90, lags: int = 2, floor: float = 1e-3) -> pd.DataFrame:
    """EWMA covariance of factor returns with Newey-West lag terms, eigenvalues floored to stay positive."""
    values = factor_returns.to_numpy(dtype=float)
    n = len(values)
    w = ewma_weights(n, halflife)
    centered = values - (w[:, None] * values).sum(axis=0)
    cov = (centered * w[:, None]).T @ centered
    for lag in range(1, lags + 1):
        gamma = (centered[lag:] * w[lag:, None]).T @ centered[:-lag]
        cov += (1 - lag / (lags + 1)) * (gamma + gamma.T)
    eigenvalues, eigenvectors = np.linalg.eigh((cov + cov.T) / 2)
    eigenvalues = np.maximum(eigenvalues, floor * max(eigenvalues.max(), 1e-12))
    return pd.DataFrame(eigenvectors @ np.diag(eigenvalues) @ eigenvectors.T, index=factor_returns.columns, columns=factor_returns.columns)


def specific_variance(specific: pd.DataFrame, caps: pd.Series, halflife: float = 60, shrink: float = 0.5, min_obs: int = 30) -> pd.Series:
    """EWMA of squared specific returns, shrunk toward the median of the coin's size bucket (terciles); floored."""
    squared = specific.pow(2)
    w = ewma_weights(len(squared), halflife)
    own = {}
    for name in squared:
        s = squared[name]
        ok = s.notna().to_numpy()
        if ok.sum() >= min_obs:
            ww = w[ok]
            own[name] = float((s.to_numpy()[ok] * ww).sum() / ww.sum())
    own = pd.Series(own)
    if own.empty:
        return own
    bucket = pd.qcut(caps.reindex(own.index).rank(method="first"), 3, labels=False)
    target = own.groupby(bucket).transform("median")
    blended = (1 - shrink) * own + shrink * target
    return blended.clip(lower=0.25 * float(own.median()))


def portfolio_risk(weights: pd.Series, exposure: pd.DataFrame, covariance: pd.DataFrame, spec_var: pd.Series) -> dict:
    """Daily vol of a portfolio and its split: factor contributions (share of variance) and specific."""
    names = [n for n in weights.index if n in exposure.index and n in spec_var.index]
    w = weights[names]
    x = exposure.loc[names].T @ w
    contribution = x * (covariance @ x)
    specific = float((w**2 * spec_var[names]).sum())
    variance = float(contribution.sum()) + specific
    return {"vol": float(np.sqrt(max(variance, 0.0))), "factor_variance": contribution, "specific_variance": specific,
            "variance": variance, "coverage": float(w.abs().sum() / weights.abs().sum()) if weights.abs().sum() else 0.0}


def model_at(fit: dict, position: int, halflife_cov: float = 90, styles: tuple[str, ...] | None = None) -> tuple[pd.DataFrame, pd.Series] | None:
    """Covariance and specific variance using factor and specific returns up to and including row ``position``."""
    factors = fit["factors"].iloc[: position + 1].fillna(0.0)   # a sector too thin on a day has no slope that day
    if styles is not None:
        factors = factors[["Market", *styles]]
    if len(factors) < 120:
        return None
    specific = fit["specific"].loc[factors.index]
    return factor_covariance(factors, halflife_cov), specific_variance(specific, fit["caps"])
