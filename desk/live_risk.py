"""Risk of the book held now, and where the current regime began.

Realized statistics over the desk's whole history describe an average of every book it has
held. A live view needs two things instead:

* ex-ante risk: today's positions (every venue, plus HL spot tokens) run through recent daily
  price moves, giving the volatility, VaR, drawdown and BTC/SPY betas of the book as it stands;
* the start of the current regime, so realized statistics can be limited to the period in
  which the book looked like it does now (see ``desk.hl.book_exposure``).
"""

from __future__ import annotations

import math
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from desk.hl.trade_analytics import bootstrap_btc_regression, bootstrap_var

EX_ANTE_DAYS = 90
# Sharpe and alpha are not quoted from fewer days than this: a shorter stretch is an episode.
MIN_RATIO_DAYS = 40
REGIME_LOOKBACK_DAYS = 240


def position_values(snapshots: Iterable, hl_spot: dict[str, float] | None = None) -> pd.DataFrame:
    """One row per priced holding: key, venue, symbol, ref, value (signed USD)."""
    rows = []
    for snapshot in snapshots:
        for position in snapshot.positions:
            rows.append({"key": f"{position.venue}:{position.account}:{position.symbol}", "venue": position.venue,
                         "symbol": position.symbol, "ref": position.ref, "value": position.notional_usd})
    for token, value in (hl_spot or {}).items():
        rows.append({"key": f"hl-spot:{token}", "venue": "hl-spot", "symbol": token, "ref": "", "value": value})
    frame = pd.DataFrame(rows, columns=["key", "venue", "symbol", "ref", "value"])
    return frame[frame["value"].abs() > 0].reset_index(drop=True)


def hl_closes(symbol: str, dex: str, days: int) -> pd.Series:
    """Daily closes of an HL market (cached candles), dated by UTC day."""
    from desk.hl.trade_analytics import fetch_daily_candles

    candles = fetch_daily_candles(symbol, dex, lookback_days=days + 2)
    return pd.Series(candles["close"].astype(float).to_numpy(),
                     index=pd.to_datetime(candles["time"], utc=True).dt.normalize())


def hl_spot_values(wallets: Iterable[str]) -> dict[str, float]:
    """USD value of each non-stable spot token across HL wallets, at today's perp mark."""
    from desk.hl.account_analytics import post
    from desk.hl.book_exposure import STABLECOINS, WRAPPED

    units: dict[str, float] = {}
    for wallet in wallets:
        state = post({"type": "spotClearinghouseState", "user": wallet}) or {}
        for balance in state.get("balances", []):
            token, amount = str(balance.get("coin", "")), float(balance.get("total") or 0.0)
            if token and token not in STABLECOINS and amount:
                units[token] = units.get(token, 0.0) + amount
    if not units:
        return {}
    marks = post({"type": "allMids"}) or {}
    values = {}
    for token, amount in units.items():
        mark = marks.get(WRAPPED.get(token, token))
        if mark is not None:
            values[WRAPPED.get(token, token)] = values.get(WRAPPED.get(token, token), 0.0) + amount * float(mark)
    return values


def holding_returns(
    holdings: pd.DataFrame, days: int, ib_closes: Callable[[list[str]], dict[str, pd.Series]] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Daily returns per holding over the last ``days`` days, and the symbols that could not be priced.

    Days a market is closed (weekends for stocks) carry a zero return, so a stock and a
    perp line up on the same calendar.
    """
    end = pd.Timestamp.now(tz="UTC").normalize()
    calendar = pd.date_range(end - pd.Timedelta(days=days), end - pd.Timedelta(days=1), freq="D")
    closes, missing = {}, []
    ib_refs = holdings.loc[holdings["venue"] == "ib", "ref"].tolist()
    ib_series = {}
    if ib_refs and ib_closes is not None:
        try:
            ib_series = ib_closes(ib_refs)
        except Exception:
            ib_series = {}
    for row in holdings.itertuples(index=False):
        try:
            if row.venue == "hl":
                series = hl_closes(row.symbol, row.ref, days)
            elif row.venue == "hl-spot":
                series = hl_closes(row.symbol, "", days)
            elif row.venue == "ib":
                series = ib_series[row.ref]
            else:
                raise KeyError(row.venue)
        except Exception:
            missing.append(row.symbol)
            continue
        series = series[~series.index.duplicated(keep="last")].sort_index()
        closes[row.key] = series.reindex(series.index.union(calendar)).ffill().reindex(calendar)
    frame = pd.DataFrame(closes, index=calendar)
    return frame.pct_change(fill_method=None).iloc[1:], missing


def ex_ante_row(
    holdings: pd.DataFrame, returns: pd.DataFrame, equity: float, benchmarks: pd.DataFrame, missing: list[str],
) -> dict:
    """Risk-table row for the current book: today's values times each day's return."""
    covered = holdings[holdings["key"].isin(returns.columns)]
    gross = holdings["value"].abs().sum()
    row = {"Basis": "Current holdings (ex-ante)", "Periods": len(returns),
           "From": f"{returns.index.min():%Y-%m-%d}" if len(returns) else "",
           "To": f"{returns.index.max():%Y-%m-%d}" if len(returns) else "",
           "Coverage": covered["value"].abs().sum() / gross if gross else np.nan}
    if covered.empty or equity <= 0 or len(returns) < 20:
        return row
    weights = covered.set_index("key")["value"] / equity
    book = returns[weights.index].fillna(0.0) @ weights
    path = (1 + book).cumprod()
    row.update({"Volatility (/yr)": float(book.std() * math.sqrt(365)),
                "Max drawdown": float((path / path.cummax() - 1).min())})
    for confidence in (0.95, 0.99):
        row[f"VaR {confidence:.0%} ($)"] = bootstrap_var(book, confidence)["var"] * equity
    daily = benchmarks.pct_change(fill_method=None) if len(benchmarks) else pd.DataFrame()
    for name in ("BTC", "SPY"):
        if name in daily:
            pair = pd.concat([book.rename("book"), daily[name].rename(name)], axis=1).dropna()
            if name == "SPY":  # only days SPY traded, so weekend crypto moves are not read as zero beta
                pair = pair[pair[name] != 0]
            if len(pair) >= 20:
                stats = bootstrap_btc_regression(pair["book"], pair[name], periods_per_year=365, samples=2000)
                row[f"Beta {name}"] = stats["beta"]
                row[f"Beta {name} 90% range"] = (f"{stats['beta_low']:.2f} to {stats['beta_high']:.2f}"
                                                 if np.isfinite(stats.get("beta_low", np.nan)) else "n/a")
    if missing:
        row["Not priced"] = ", ".join(sorted(set(missing)))
    return row


def hl_regime_start(wallets: list[str], equity: pd.Series, lookback_days: int = REGIME_LOOKBACK_DAYS) -> tuple[pd.Timestamp | None, str]:
    """First day of the HL book's current regime and its name (None if it cannot be told)."""
    from desk.hl import book_exposure

    start = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=lookback_days)
    daily, _ = book_exposure.fetch_book_exposure(wallets, equity, start=start)
    weekly = book_exposure.weekly_exposure(daily)
    if weekly.empty:
        return None, ""
    regimes = book_exposure.label_regimes(weekly)
    current = regimes.iloc[-1]
    first_week = regimes[regimes == current].index.min()
    return first_week - pd.Timedelta(days=6), current.split(" (")[0]
