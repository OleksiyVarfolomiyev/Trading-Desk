"""Factor risk model for stocks: each holding's exposures to the Fama-French factors (time-series regressions).

Without a fundamentals vendor there is no cross-section of book-to-market or profitability to regress on, so the stock
side uses what is free and point-in-time: Kenneth French's daily factors (market, size, value, profitability,
investment, momentum; the files in research/data/french). Each stock's daily excess return is regressed on them over
a trailing window, which gives its exposures and its specific variance; the factor covariance is the EWMA of the factor
returns (the same Newey-West corrected estimator as the crypto model, desk/factor_risk.py).

Risk of a portfolio w: ``b'Fb + sum(w_i^2 s_i^2)`` with ``b = sum(w_i * exposure_i)``.

Equity perps on Hyperliquid count as stocks, but their history is about a year and they trade around the clock, so
they are modelled by their listed underlyings' factor exposures (see research/stock_risk_study.py for the test of a
cross-section on the perps themselves).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from desk.factor_risk import factor_covariance

FRENCH_DIR = Path(__file__).resolve().parent.parent / "research" / "data" / "french"
FACTOR_FILES = {"us5": "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip", "momentum": "F-F_Momentum_Factor_daily_CSV.zip"}
FACTORS = ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom")
WINDOW, MIN_OBS = 250, 120


def parse_french(text: str) -> pd.DataFrame:
    """The daily rows (a date in YYYYMMDD then percent returns) of a Fama-French file, as decimals."""
    rows, header = [], None
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) > 1 and len(parts[0]) == 8 and parts[0].isdigit():
            rows.append([parts[0]] + [float(p) / 100 for p in parts[1:] if p != ""])
        elif rows:
            break
        elif len(parts) > 1 and parts[0] == "":
            header = [p for p in parts[1:] if p]
    frame = pd.DataFrame(rows, columns=["date"] + (header or []))
    frame["date"] = pd.to_datetime(frame["date"], format="%Y%m%d")
    frame.columns = [c.strip() for c in frame.columns]
    return frame.set_index("date")


def load_factors(directory: Path = FRENCH_DIR) -> pd.DataFrame:
    """Daily factor returns and the risk-free rate from the cached French files (no download)."""
    parts = []
    for name in FACTOR_FILES.values():
        with zipfile.ZipFile(directory / name) as archive:
            parts.append(parse_french(archive.read(archive.namelist()[0]).decode("latin-1")))
    frame = pd.concat(parts, axis=1, sort=True).dropna()
    frame = frame.rename(columns={c: "Mom" for c in frame.columns if c.strip().lower().startswith("mom")})
    return frame[list(FACTORS) + ["RF"]]


def exposures(excess: pd.DataFrame, factors: pd.DataFrame, factor_names: tuple[str, ...] = FACTORS) -> tuple[pd.DataFrame, pd.Series]:
    """OLS exposures (names x factors) and specific variance per name over the rows given (an intercept is fitted)."""
    x = np.column_stack([np.ones(len(factors)), factors[list(factor_names)].to_numpy()])
    betas, spec = {}, {}
    for name in excess:
        y = excess[name].to_numpy(dtype=float)
        ok = np.isfinite(y)
        if ok.sum() < MIN_OBS:
            continue
        coefficients, *_ = np.linalg.lstsq(x[ok], y[ok], rcond=None)
        resid = y[ok] - x[ok] @ coefficients
        betas[name] = coefficients[1:]
        spec[name] = float(resid.var(ddof=x.shape[1]))
    return pd.DataFrame(betas, index=list(factor_names)).T, pd.Series(spec)


def model_at(excess: pd.DataFrame, factors: pd.DataFrame, position: int, factor_names: tuple[str, ...] = FACTORS,
             window: int = WINDOW) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series] | None:
    """Exposures, factor covariance and specific variance using rows up to and including ``position`` only."""
    if position + 1 < MIN_OBS:
        return None
    lo = max(0, position + 1 - window)
    betas, spec = exposures(excess.iloc[lo: position + 1], factors.iloc[lo: position + 1], factor_names)
    cov = factor_covariance(factors[list(factor_names)].iloc[: position + 1], halflife=90)
    return betas, cov, spec


def portfolio_risk(weights: pd.Series, betas: pd.DataFrame, covariance: pd.DataFrame, spec_var: pd.Series) -> dict:
    """Daily vol of a portfolio and the share of its variance that is factor and specific."""
    names = [n for n in weights.index if n in betas.index and n in spec_var.index]
    w = weights[names]
    b = betas.loc[names].T @ w
    factor_var = float(b @ covariance.loc[b.index, b.index] @ b)
    specific = float((w**2 * spec_var[names]).sum())
    total = factor_var + specific
    return {"vol": float(np.sqrt(max(total, 0.0))), "factor_variance": factor_var, "specific_variance": specific,
            "factor_share": factor_var / total if total > 0 else float("nan"),
            "coverage": float(w.abs().sum() / weights.abs().sum()) if weights.abs().sum() else 0.0}
