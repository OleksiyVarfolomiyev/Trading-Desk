"""Beta-weighted variant of the excess-return attribution (desk/books.py decision_attribution), as an experiment.

Production attributes a book's excess return over its index with dollar weights: the market-exposure line is
(average net weight - 1) x the index's return, and picks carry whatever beta the names have. Here the exposure each day
is beta-weighted (each market's value x its beta to the book's index, estimated only from returns up to the day before),
so market exposure is the beta bet and picks are the names' idiosyncratic returns.

The variant reuses production's own telescoping: ``decision_attribution`` runs with its ``decision_marks`` swapped for
``beta_marks`` below. The parts then still add up to the excess return exactly (an identity, see the tests), and the
unattributed line is the same as in the dollar version, whatever betas are used.

    /Users/admin/anaconda3/envs/py314/bin/python research/attribution_beta_test.py [--shrink]

It reads the real books (as desk/web_sync.py does: HL and IB reads, cached candles) and prints each window dollar-weighted
against beta-weighted, how much of the picks moved into market exposure, and the two halves of the whole period.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk import books  # noqa: E402

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
    """Daily closes of one market: HL candles for crypto; Yahoo for stocks, with HL's equity perps as the fallback."""
    from desk.hl.trade_analytics import fetch_daily_candles
    from desk.live_risk import yahoo_stock_closes

    def hl(dex):
        frame = fetch_daily_candles(asset, dex, lookback_days=lookback)
        return pd.Series(frame["close"].astype(float).values, index=pd.to_datetime(frame["time"], utc=True))

    if book.kind == "Crypto":
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


def half(book, returns: pd.Series, which: int) -> SimpleNamespace:
    """The book restricted to the first (0) or second (1) half of its days; the full history still feeds the marks."""
    cut = len(book.days) // 2
    days = book.days[:cut] if which == 0 else book.days[cut:]
    return SimpleNamespace(coins=book.coins, days=days, equity=book.equity, benchmark=book.benchmark,
                           benchmark_closes=book.benchmark_closes), returns[returns.index.isin(days)]


def compare(book, returns: pd.Series, betas: pd.DataFrame) -> pd.DataFrame:
    """Rows: window, factor; columns: dollar, beta, shift (beta - dollar), in percentage points of equity."""
    dollar, beta = attribute(book, returns, None), attribute(book, returns, betas)
    rows = []
    for window in dollar:
        if window not in beta:
            continue
        for part in PARTS:
            d, b = dollar[window]["parts"].get(part, np.nan), beta[window]["parts"].get(part, np.nan)
            rows.append({"Window": window, "Factor": part, "Dollar pp": 100 * d, "Beta pp": 100 * b, "Shift pp": 100 * (b - d)})
        rows.append({"Window": window, "Factor": "Excess return", "Dollar pp": 100 * dollar[window]["excess"],
                     "Beta pp": 100 * beta[window]["excess"], "Shift pp": 0.0})
    return pd.DataFrame(rows)


def halves(book, returns: pd.Series, betas: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for which, name in ((0, "first half"), (1, "second half")):
        part, part_returns = half(book, returns, which)
        for label, weights in (("Dollar", None), ("Beta", betas)):
            result = attribute(part, part_returns, weights).get("All")
            if result:
                rows.append({"Period": name, "Weights": label, **{k: 100 * result["parts"].get(k, np.nan) for k in PARTS},
                             "Excess": 100 * result["excess"], "From": result["start"], "To": result["end"]})
    return pd.DataFrame(rows)


def weights_summary(book, betas: pd.DataFrame) -> dict:
    """Average net weight on trading equity over the whole period: dollars against beta-weighted."""
    names = book.coins.sort_values("day")
    before = names.groupby(["account", "kind", "asset"])["value"].shift(1).fillna(0.0)
    equity = book.equity.shift(1).reindex(book.days)
    stacked = betas.stack()
    stacked.index.names = ["day", "asset"]
    beta = names.join(stacked.rename("beta"), on=["day", "asset"])["beta"].fillna(1.0)
    dollar = before.groupby(names["day"]).sum().reindex(book.days) / equity.where(equity > 0)
    weighted = (before * beta).groupby(names["day"]).sum().reindex(book.days) / equity.where(equity > 0)
    held = names.loc[before.abs() > 0.01]
    return {"net dollar weight": float(dollar.mean()), "net beta weight": float(weighted.mean()),
            "mean beta of held names": float(beta.loc[held.index].mean()),
            "share at beta 1 (no history)": float(betas.isna().stack().mean()) if betas.size else float("nan")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shrink", action="store_true", help="shrink betas toward 1 (Blume: 0.67 x beta + 0.33)")
    args = parser.parse_args(argv)
    pd.set_option("display.width", 200)
    for kind, options in (("Crypto", {}), ("Stocks", {"since": "2025-01-13"})):
        book = books.Book(kind, **options).fetch_data()
        returns = books.chart_returns(book).dropna()
        betas, missing = beta_table(book, args.shrink)
        print(f"\n===== {kind} vs {book.benchmark} ({'shrunk' if args.shrink else 'raw'} {BETA_DAYS}d betas) =====")
        print("left at beta 1 (no price history):", ", ".join(missing) or "none")
        print({k: round(v, 3) for k, v in weights_summary(book, betas).items()})
        print(compare(book, returns, betas).round(2).to_string(index=False))
        print(halves(book, returns, betas).round(2).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
