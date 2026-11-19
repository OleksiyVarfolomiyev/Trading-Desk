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

from desk.beta_attribution import (  # noqa: E402,F401  the method lives in desk/
    BETA_CLIP, BETA_DAYS, BETA_MIN_OBS, asset_closes_for, attribute, benchmark_returns, beta_marks_for, beta_table,
    rolling_beta)

PARTS = ["Picks", "Sizing", "Timing", "Market exposure", "Fees & funding", "Unattributed"]


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
