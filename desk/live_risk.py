"""Risk of the book held now, and where the current regime began.

Realized statistics over the desk's whole history describe an average of every book it has
held. A live view needs two things instead:

* ex-ante risk: today's positions (every venue, plus HL spot tokens) run through recent daily
  price moves, giving the volatility, VaR, drawdown and BTC/SPY betas of the book as it stands;
* realized statistics limited to the current regime, the period in which the book looked
  like it does now (detected by ``desk.hl.book_exposure.book_regimes``).
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


def position_values(snapshots: Iterable, hl_spot: Iterable[dict] | None = None) -> pd.DataFrame:
    """One row per holding: key, venue, symbol, ref, value (signed USD).

    ``hl_spot`` rows come from ``hl_spot_values``; their ``ref`` is the HL market that prices
    them (the perp where one exists, otherwise the token's spot pair).
    """
    rows = []
    for snapshot in snapshots:
        for position in snapshot.positions:
            rows.append({"key": f"{position.venue}:{position.account}:{position.symbol}", "venue": position.venue,
                         "symbol": position.symbol, "ref": position.ref, "value": position.notional_usd})
    for holding in hl_spot or ():
        rows.append({"key": f"hl-spot:{holding['token']}", "venue": "hl-spot", "symbol": holding["token"],
                     "ref": holding["market"], "value": holding["value"]})
    frame = pd.DataFrame(rows, columns=["key", "venue", "symbol", "ref", "value"])
    return frame[frame["value"].abs() > 0].reset_index(drop=True)


def hl_closes(symbol: str, dex: str, days: int) -> pd.Series:
    """Daily closes of an HL market (cached candles), dated by UTC day."""
    from desk.hl.trade_analytics import fetch_daily_candles

    candles = fetch_daily_candles(symbol, dex, lookback_days=days + 2)
    return pd.Series(candles["close"].astype(float).to_numpy(),
                     index=pd.to_datetime(candles["time"], utc=True).dt.normalize())


def price_spot_holdings(units: dict[str, float], mids: dict[str, str], pairs: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Value spot balances: at the token's perp mid where a perp exists, else its spot pair's mid.

    Returns (holdings, unpriced): each holding is ``{"token", "market", "value"}``, where
    ``market`` is the HL market that also supplies its price history; ``unpriced`` lists
    tokens with neither price.
    """
    from desk.hl.book_exposure import WRAPPED

    holdings, unpriced = [], []
    for token, amount in sorted(units.items()):
        perp = WRAPPED.get(token, token)
        market = perp if perp in mids else pairs.get(token)
        if market is None or market not in mids:
            unpriced.append(token)
            continue
        holdings.append({"token": perp, "market": market, "value": amount * float(mids[market])})
    return holdings, unpriced


def hl_spot_values(wallets: Iterable[str]) -> tuple[list[dict], list[str]]:
    """Non-stable spot tokens across HL wallets, valued now; and the tokens that have no HL price."""
    from desk.hl.account_analytics import post
    from desk.hl.book_exposure import STABLECOINS

    units: dict[str, float] = {}
    for wallet in wallets:
        state = post({"type": "spotClearinghouseState", "user": wallet}) or {}
        for balance in state.get("balances", []):
            token, amount = str(balance.get("coin", "")), float(balance.get("total") or 0.0)
            if token and token not in STABLECOINS and amount:
                units[token] = units.get(token, 0.0) + amount
    if not units:
        return [], []
    from desk.hl.book_exposure import spot_pairs

    return price_spot_holdings(units, post({"type": "allMids"}) or {}, spot_pairs(post({"type": "spotMeta"}) or {}))


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
                series = hl_closes(row.ref, "", days)
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


def paired_returns(book: pd.Series, closes: pd.Series) -> pd.DataFrame:
    """A daily book return series and a benchmark's returns over the same intervals.

    The benchmark's return runs between its own consecutive closes (Friday to Monday for SPY),
    and the book's daily returns are compounded over exactly those days. Computing returns on a
    frame that joins SPY to every-day BTC would divide Monday by Sunday's missing close and drop
    every Monday. An interval with any book day missing is left out rather than half-compounded.
    """
    closes = closes.dropna().sort_index()
    closes = closes[~closes.index.duplicated(keep="last")]
    bench = closes.pct_change(fill_method=None).iloc[1:]
    book = book.dropna().sort_index()
    if bench.empty or book.empty:
        return pd.DataFrame(columns=["book", "bench"])
    # Each book day belongs to the first benchmark date on or after it.
    position = bench.index.searchsorted(book.index, side="left")
    inside = position < len(bench)
    labels = bench.index[position[inside]]
    days = book[inside]
    compounded = (1 + days).groupby(labels).prod() - 1
    counts = days.groupby(labels).size()
    previous = closes.index[:-1]
    span = pd.Series((bench.index - previous).days, index=bench.index)
    complete = counts.reindex(bench.index).eq(span)
    frame = pd.DataFrame({"book": compounded.reindex(bench.index), "bench": bench})
    return frame[complete].dropna()


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
    for name in ("BTC", "SPY"):
        if name in benchmarks:
            pair = paired_returns(book, benchmarks[name])
            if len(pair) >= 20:
                stats = bootstrap_btc_regression(pair["book"], pair["bench"], periods_per_year=365, samples=2000)
                row[f"Beta {name}"] = stats["beta"]
                row[f"Beta {name} 90% range"] = (f"{stats['beta_low']:.2f} to {stats['beta_high']:.2f}"
                                                 if np.isfinite(stats.get("beta_low", np.nan)) else "n/a")
    if missing:
        row["Not priced"] = ", ".join(sorted(set(missing)))
    return row
