"""Beta-weighted picks for the excess-return attribution (desk/books.py decision_attribution).

Production attributes a book's excess return over its index with dollar weights, so picks carry whatever beta the names
have. Here each day's exposure is beta-weighted (each market's value x its trailing 90-day beta to the book's index,
from returns up to the day before, beta 1 under 20 paired returns), so picks are the names' idiosyncratic returns.

The variant reuses production's own telescoping: ``decision_attribution`` runs with its ``decision_marks`` swapped for
``beta_marks_for``. The parts still add up to the excess return exactly and the unattributed line is unchanged.
research/attribution_beta_test.py compares the two weightings over each window and half.
"""

from __future__ import annotations

from unittest import mock

import numpy as np
import pandas as pd

from desk import books, listings

BETA_DAYS = 90        # trailing window of benchmark-date returns
BETA_MIN_OBS = 20     # fewer than this and the market counts as beta 1 (the dollar-weighted treatment)
BETA_CLIP = (-2.0, 4.0)
PARTS = ["Picks", "Sizing", "Timing", "Market exposure", "Fees & funding", "Unattributed"]


def benchmark_returns(book) -> pd.Series:
    """The index's daily return on the book's days, prepared exactly as decision_attribution does."""
    closes = book.benchmark_closes.copy()
    closes.index = pd.to_datetime(closes.index, utc=True).normalize()
    closes = closes.dropna().sort_index()
    closes = closes[~closes.index.duplicated(keep="last")]
    closes = closes[closes > 0]
    return closes.pct_change(fill_method=None).reindex(book.days).fillna(0.0)


def rolling_beta(asset_closes: pd.Series, bench_closes: pd.Series, days: pd.DatetimeIndex,
                 shrink: bool = False) -> pd.Series:
    """The asset's beta to the index on each of ``days`` using only returns through the day before (the exposure a day
    starts with is the previous close's). NaN where fewer than BETA_MIN_OBS paired returns exist."""
    bench = bench_closes.copy()
    bench.index = pd.to_datetime(bench.index, utc=True).normalize()
    asset = asset_closes.copy()
    asset.index = pd.to_datetime(asset.index, utc=True).normalize()
    pair = pd.DataFrame({"a": asset[~asset.index.duplicated(keep="last")],
                         "b": bench[~bench.index.duplicated(keep="last")]}).dropna()
    r = pair.pct_change().dropna()
    cov = r["a"].rolling(BETA_DAYS, min_periods=BETA_MIN_OBS).cov(r["b"])
    var = r["b"].rolling(BETA_DAYS, min_periods=BETA_MIN_OBS).var()
    beta = (cov / var.where(var > 0)).clip(*BETA_CLIP)
    if shrink:
        beta = 0.67 * beta + 0.33   # Blume: betas drift toward 1
    full = pd.date_range(min(days.min(), beta.index.min() if len(beta) else days.min()), days.max(), freq="D", tz="UTC")
    return beta.reindex(full).ffill().shift(1).reindex(days)


def asset_closes_for(book, asset: str, venue: str, lookback: int) -> pd.Series:
    """Daily closes of one market: HL candles for crypto; for stocks Yahoo, and HL's own candles for a market with a
    dex prefix (``xyz:AAPL``, ``io:IONQ``, ``para:SOFI``), whose prefix names the dex."""
    from desk.hl.trade_analytics import fetch_daily_candles
    from desk.live_risk import yahoo_stock_closes

    def hl(dex):
        frame = fetch_daily_candles(asset, dex, lookback_days=lookback)
        own = pd.Series(frame["close"].astype(float).values, index=pd.to_datetime(frame["time"], utc=True))
        return listings.spliced(asset, own, lookback) if len(own) < lookback else own   # a new market borrows its underlying's history

    if book.kind == "Crypto":
        return hl("")
    dex = asset.split(":")[0] if ":" in asset else ""
    if dex:
        return hl(dex)
    if listings.listing_for(asset):   # a bare name that is not a US share (PAXG is a token; Yahoo's "PAXG" is another company)
        return hl("")
    try:
        return yahoo_stock_closes(asset, lookback)
    except Exception:
        return hl("xyz")


def beta_table(book, shrink: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """Day x asset betas for every market the book held, and the markets it had to leave at beta 1."""
    held = book.coins[book.coins["value"].abs() > 0.01]
    lookback = int((book.days[-1] - book.days[0]).days) + BETA_DAYS + 40
    out, missing = {}, []
    for asset, venue in held.groupby("asset")["venue"].first().items():
        try:
            out[asset] = rolling_beta(asset_closes_for(book, asset, venue, lookback), book.benchmark_closes, book.days, shrink)
        except Exception:
            missing.append(asset)
    return pd.DataFrame(out, index=book.days), missing


def beta_marks_for(betas: pd.DataFrame):
    """A replacement for books.decision_marks that weights by beta. Per day: ``before`` is the beta-weighted exposure;
    ``equal_price`` is the equal-dollar idiosyncratic return of each side's names plus the benchmark leg
    (before x index return), so that decision_attribution's picks (equal_price - before x index) are the equal-dollar
    idiosyncratic returns and its sizing (price - equal_price) is what dollar weights added to them."""

    def beta_marks(book) -> pd.DataFrame:
        bench = benchmark_returns(book)
        names = book.coins.sort_values("day").copy()
        keys = ["account", "kind", "asset"]
        names["before"] = names.groupby(keys)["value"].shift(1).fillna(0.0)
        stacked = betas.stack()
        stacked.index.names = ["day", "asset"]
        names = names.join(stacked.rename("beta"), on=["day", "asset"])
        names["beta"] = names["beta"].fillna(1.0)
        names["before_beta"] = names["before"] * names["beta"]
        held = names.loc[names["before"].abs() > 0.01].copy()
        held["side"] = np.sign(held["before"])
        # Each distinct market once per side, as the dollar version does; the beta of a market is the same in every wallet.
        holdings = held.groupby(["day", "side", "kind", "asset"]).agg(
            before=("before", "sum"), price=("price", "sum"), beta=("beta", "first"))
        holdings["gross"] = holdings["before"].abs()
        side_sign = holdings.index.get_level_values("side").to_numpy()
        day_bench = bench.reindex(holdings.index.get_level_values("day")).to_numpy()
        holdings["idio"] = holdings["price"] / holdings["gross"] - side_sign * holdings["beta"] * day_bench
        side = holdings.groupby(level=["day", "side"]).agg(gross=("gross", "sum"), mean_idio=("idio", "mean"))
        equal_idio = (side["gross"] * side["mean_idio"]).groupby(level="day").sum()

        marks = names.groupby("day")[["before_beta", "price", "trading", "fees", "funding"]].sum()
        marks = marks.rename(columns={"before_beta": "before"})
        marks["equal_price"] = equal_idio
        marks = marks.reindex(book.days).fillna(0.0)
        marks["equal_price"] = marks["equal_price"] + marks["before"] * bench
        return marks

    return beta_marks


def attribute(book, returns: pd.Series, betas: pd.DataFrame | None) -> dict:
    """decision_attribution as production does it (betas None) or beta-weighted."""
    if betas is None:
        return books.decision_attribution(book, returns)
    with mock.patch.object(books, "decision_marks", beta_marks_for(betas)):
        return books.decision_attribution(book, returns)


def beta_picks(book, returns: pd.Series, shrink: bool = False) -> dict:
    """Each window's picks with beta-weighted exposure, in return on equity (a fraction): {"W": 0.012, ...}. Markets that
    cannot be given a beta stay at beta 1 (the dollar treatment)."""
    betas, _missing = beta_table(book, shrink)
    result = attribute(book, returns, betas)
    return {window: float(item["parts"]["Picks"]) for window, item in result.items() if "Picks" in item.get("parts", {})}
