"""Rank HL markets using the same definitions as the app's market selector.

The views mirror the Crypto, Tradfi, HIP-3, and Trending tabs. Formulas and filters
follow the app's public code as checked on 2026-11-03; HL can change them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from desk.hl.trade_analytics import post_info
from desk.numbers import usd as _usd, pct as _pct

TOP_N = 20
TRADFI_CATEGORIES = {"stocks", "stock", "indices", "commodities", "fx", "preipo", "rates"}
# Highlights and tables are split by kind of market, so crypto and stocks are never ranked together.
STOCK_CATEGORIES = {"stocks", "stock", "indices", "preipo"}
COMMODITY_FX_CATEGORIES = {"commodities", "fx", "rates"}
CRYPTO_HIP3_DEXES = {"hyna"}
CRYPTO_HIP3_MARKETS = {"cash:BTC", "cash:ETH", "flx:BTC", "flx:USDE", "flx:XMR", "io:PRL", "para:ANSEM",
                       "para:BTCD", "para:HYPE2", "para:OTHERS", "para:TOTAL2"}
MARKET_KINDS = ("Crypto", "Stocks", "Commodities & FX", "Other")
TRENDING_MIN_VOLUME = 30_000_000.0
CHANGE = "24h Change (%)"
FUNDING = "8h Funding (%)"
VOLUME = "24h Volume"
OPEN_INTEREST = "OI"
TABLE_COLUMNS = ["Ticker", CHANGE, FUNDING, VOLUME, OPEN_INTEREST]
VIEW_LABELS = {"crypto": "Crypto", "tradfi": "Tradfi", "hip3": "HIP-3", "trending": "Trending"}


def asset_class(dex: str | None, category: str | None) -> str:
    """Name a market's single class: Crypto, Tradfi, or HIP-3 for other HIP-3 markets."""
    if not dex:
        return "Crypto"
    return "Tradfi" if str(category or "").lower() in TRADFI_CATEGORIES else "HIP-3"


def market_kind(ticker: str, dex: str | None, category: str | None) -> str:
    """Crypto (main DEX, spot, crypto HIP-3), Stocks (stocks, indices, pre-IPO), Commodities & FX, or Other."""
    category = str(category or "").lower()
    if not dex or category == "crypto" or dex in CRYPTO_HIP3_DEXES or ticker in CRYPTO_HIP3_MARKETS:
        return "Crypto"
    if category in STOCK_CATEGORIES:
        return "Stocks"
    if category in COMMODITY_FX_CATEGORIES:
        return "Commodities & FX"
    return "Other"


def by_kind(markets: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """The markets split by kind, in ``MARKET_KINDS`` order; one unnamed group without Dex/Category columns."""
    if markets.empty or not {"Dex", "Category"}.issubset(markets.columns):
        return [("", markets)]
    kinds = pd.Series([market_kind(row["Ticker"], row["Dex"], row["Category"]) for _, row in markets.iterrows()],
                      index=markets.index)
    return [(kind, markets[kinds == kind]) for kind in MARKET_KINDS if (kinds == kind).any()]


def _number(value: object) -> float:
    """Convert an API value to a float, using ``NaN`` for missing or invalid values."""
    number = pd.to_numeric(value, errors="coerce")
    return float(number) if pd.notna(number) else float("nan")


def _day_change_pct(mid_price: float, previous_price: float) -> float:
    """Return mid versus prior-day price, or ``NaN`` for markets without both prices."""
    if not mid_price > 0 or not previous_price > 0:
        return float("nan")
    return (mid_price / previous_price - 1) * 100


def fetch_perp_markets() -> pd.DataFrame:
    """Fetch active perps from the main DEX and every HIP-3 DEX."""
    categories = {
        coin: str(category).lower() for coin, category in post_info({"type": "perpCategories"})
    }
    dexes = [""] + [dex["name"] for dex in post_info({"type": "perpDexs"}) if dex and dex.get("name")]
    rows = []
    for dex in dexes:
        meta, contexts = post_info({"type": "metaAndAssetCtxs", "dex": dex})
        for asset, context in zip(meta["universe"], contexts):
            if asset.get("isDelisted"):
                continue
            rows.append({
                "Ticker": asset["name"],
                "Dex": dex,
                "Category": categories.get(asset["name"], ""),
                CHANGE: _day_change_pct(_number(context.get("midPx")), _number(context.get("prevDayPx"))),
                # The API rate is hourly and fractional; the app shows it per 8h in percent.
                FUNDING: _number(context.get("funding")) * 8 * 100,
                VOLUME: _number(context.get("dayNtlVlm")),
                # The API reports open interest in coins.
                OPEN_INTEREST: _number(context.get("openInterest")) * _number(context.get("markPx")),
            })
    return pd.DataFrame(rows)


def fetch_canonical_spot_markets() -> pd.DataFrame:
    """Fetch canonical spot pairs, the only spot markets eligible for Trending."""
    meta, contexts = post_info({"type": "spotMetaAndAssetCtxs"})
    contexts_by_coin = {context.get("coin"): context for context in contexts}
    rows = []
    for pair in meta["universe"]:
        context = contexts_by_coin.get(pair["name"])
        if not pair.get("isCanonical") or context is None:
            continue
        rows.append({
            "Ticker": pair["name"],
            "Dex": None,
            "Category": "spot",
            CHANGE: _day_change_pct(_number(context.get("midPx")), _number(context.get("prevDayPx"))),
            FUNDING: np.nan,
            VOLUME: _number(context.get("dayNtlVlm")),
            OPEN_INTEREST: np.nan,
        })
    return pd.DataFrame(rows)


def select_view(markets: pd.DataFrame, view: str) -> pd.DataFrame:
    """Return one view's market universe; Trending returns its volume-eligible pool."""
    if view == "crypto":
        return markets.loc[markets["Dex"] == ""]
    if view == "hip3":
        return markets.loc[markets["Dex"].notna() & (markets["Dex"] != "")]
    if view == "tradfi":
        return markets.loc[
            markets["Dex"].notna() & (markets["Dex"] != "") & markets["Category"].isin(TRADFI_CATEGORIES)
        ]
    if view == "trending":
        return markets.loc[markets[VOLUME] > TRENDING_MIN_VOLUME]
    raise ValueError(f"Unknown market view: {view!r}")


def trending_markets(markets: pd.DataFrame) -> pd.DataFrame:
    """Return what the Trending tab lists: the biggest loser and three biggest gainers (of the markets given)."""
    ranked = select_view(markets, "trending").dropna(subset=[CHANGE]).sort_values(CHANGE)
    picked = pd.concat([ranked.iloc[:1], ranked.iloc[-3:]])
    return picked[~picked.index.duplicated()].sort_values(CHANGE, ascending=False)


def rank_markets(markets: pd.DataFrame, metric: str, top: bool, count: int = TOP_N) -> pd.DataFrame:
    """Return the highest or lowest markets by ``metric``, skipping missing values.

    The top and bottom lists never share a market: with fewer than ``2 * count`` markets the
    ranking is split in half (the top gets the extra one), so a small view is not shown twice
    in opposite orders.
    """
    ranked = markets.dropna(subset=[metric]).sort_values(metric, ascending=False)
    top_count = min(count, (len(ranked) + 1) // 2)
    if top:
        return ranked.head(top_count)
    bottom_count = min(count, len(ranked) - top_count)
    return ranked.tail(bottom_count).iloc[::-1] if bottom_count else ranked.iloc[0:0]


HIGH_FUNDING_8H = 0.05  # % per 8h; about 55% a year, which makes carrying the paying side expensive


def market_highlights(universe: pd.DataFrame, view: str) -> tuple[list, list]:
    """What stands out in one market view: biggest moves, funding extremes, and where the volume is.

    A view mixing kinds (Trending, HIP-3, Tradfi) gets each line per kind, "Biggest gainers (crypto)",
    "Biggest gainers (stocks)", so crypto and stocks are never ranked against each other.
    """
    if universe.empty:
        return [], [("Markets", "No markets returned for this view.")]
    groups = by_kind(universe)
    well, attention = [], []
    for kind, group in groups:
        part_well, part_attention = _kind_highlights(group, f" ({kind.lower()})" if len(groups) > 1 else "")
        well += part_well
        attention += part_attention
    if view == "trending":
        well.append(("Trending pool", f"{len(universe):,} markets above {_usd(TRENDING_MIN_VOLUME / 1e6)}M 24h volume."))
    return well, attention


def _kind_highlights(universe: pd.DataFrame, suffix: str) -> tuple[list, list]:
    """One kind's highlights; ``suffix`` (" (crypto)") names the kind in each label when a view mixes kinds."""
    well, attention = [], []
    scope = suffix.strip(" ()") if suffix else "the view's"

    def names(frame: pd.DataFrame, column: str, fmt: str) -> str:
        return ", ".join(f"{row['Ticker']} {format(row[column], fmt)}" for _, row in frame.iterrows())

    changes = universe.dropna(subset=[CHANGE])
    if len(changes):
        up = (changes[CHANGE] > 0).mean()
        well.append((f"Breadth{suffix}", f"{up:.0%} of {len(changes):,} markets are up over 24h; median change "
                                f"{_pct(changes[CHANGE].median(), scaled=True)}."))
        # Gainers must be up and losers down: in a falling market the "top 3" are only the smallest
        # declines, and a small view could otherwise list one market as both.
        gainers, losers = changes[changes[CHANGE] > 0], changes[changes[CHANGE] < 0]
        if len(gainers):
            well.append((f"Biggest gainers{suffix}", names(gainers.nlargest(3, CHANGE), CHANGE, ".2f") + " (%)."))
        else:
            attention.append((f"No gainers{suffix}", f"none of {len(changes):,} markets is up over 24h; the smallest "
                                            f"declines are {names(changes.nlargest(3, CHANGE), CHANGE, '.2f')} (%)."))
        if len(losers):
            attention.append((f"Biggest losers{suffix}", names(losers.nsmallest(3, CHANGE), CHANGE, ".2f") + " (%)."))
        else:
            well.append((f"No losers{suffix}", f"none of {len(changes):,} markets is down over 24h."))
    funding = universe.dropna(subset=[FUNDING])
    if len(funding):
        hot = funding[funding[FUNDING].abs() >= HIGH_FUNDING_8H].sort_values(FUNDING, key=abs, ascending=False)
        if len(hot):
            attention.append((f"Funding beyond ±{HIGH_FUNDING_8H}%/8h{suffix}",
                              f"{len(hot)} markets; " + names(hot.head(5), FUNDING, ".4f")
                              + " (%; positive means longs pay)."))
        top, bottom = funding.loc[funding[FUNDING].idxmax()], funding.loc[funding[FUNDING].idxmin()]
        well.append((f"Funding range{suffix}", f"highest {top['Ticker']} {top[FUNDING]:.4f}% "
                                      f"(~{top[FUNDING] * 3 * 365:.0f}%/yr), lowest {bottom['Ticker']} "
                                      f"{bottom[FUNDING]:.4f}% (~{bottom[FUNDING] * 3 * 365:.0f}%/yr)."))
    volume = universe.dropna(subset=[VOLUME])
    if len(volume) and volume[VOLUME].sum() > 0:
        leaders = volume.nlargest(3, VOLUME)
        share = leaders[VOLUME].sum() / volume[VOLUME].sum()
        well.append((f"Most traded{suffix}", ", ".join(f"{row['Ticker']} {_usd(row[VOLUME] / 1e6)}M" for _, row in leaders.iterrows())
                     + f" ({share:.0%} of {scope} 24h volume)."))
    return well, attention


def show_market_research(view: str) -> None:
    """Display the top and bottom tables for one market view."""
    from desk.hl.account_analytics import display_interactive_table

    markets = fetch_perp_markets()
    if view == "trending":
        markets = pd.concat([markets, fetch_canonical_spot_markets()], ignore_index=True)
    universe = select_view(markets, view)
    label = VIEW_LABELS[view]

    def signed_style(row_index, column, value, row):
        if column in {CHANGE, FUNDING} and pd.notna(value):
            return "color:#188038;" if value > 0 else "color:#c62828;" if value < 0 else ""
        return ""

    def show(title: str, frame: pd.DataFrame, key: dict | None = None) -> None:
        display_interactive_table(
            frame,
            title=title,
            columns=TABLE_COLUMNS,
            cell_style=signed_style,
            formatters={
                CHANGE: lambda v: f"{_pct(v, scaled=True)}" if pd.notna(v) else "n/a",
                FUNDING: lambda v: f"{v:.4f}%" if pd.notna(v) else "n/a",
                VOLUME: lambda v: f"{_usd(v)}" if pd.notna(v) else "n/a",
                OPEN_INTEREST: lambda v: f"{_usd(v)}" if pd.notna(v) else "n/a",
            },
            key_columns=key,
        )

    from desk.notebook_highlights import show as show_highlights

    show_highlights(*market_highlights(universe, view))
    print(f"{label}: {len(universe):,} markets. Change is mid vs prior-day price; funding is the "
          "current hourly rate x 8. Positive funding means longs pay shorts.")
    groups = by_kind(universe)
    if len(groups) > 1:
        print("Crypto and stocks are ranked separately: " + ", ".join(f"{kind} {len(group):,}" for kind, group in groups) + ".")
    if view == "trending":
        print(f"Trending lists the biggest loser and three biggest gainers with over "
              f"{_usd(TRENDING_MIN_VOLUME)} 24h volume, per kind. The ranked tables below use that same pool.")
        for kind, group in groups:
            show(f"HL TRENDING{' - ' + kind.upper() if kind else ''}", trending_markets(group), {CHANGE: "abs"})
    for kind, group in groups:
        tag = f" - {kind.upper()}" if len(groups) > 1 else ""
        for name, metric, top in (
            ("24H CHANGE", CHANGE, True), ("24H CHANGE", CHANGE, False),
            ("8H FUNDING", FUNDING, True), ("8H FUNDING", FUNDING, False),
        ):
            ranked = rank_markets(group, metric, top)
            available = int(group[metric].notna().sum())
            if not available:
                continue  # e.g. no funding on spot pairs
            # Real counts in the title: a small view is split between its top and bottom tables.
            scope = f" OF {available}" if available < 2 * TOP_N else ""
            # The ranking metric is the key column; its extreme (the top or bottom market) stands out.
            show(f"{'TOP' if top else 'BOTTOM'} {len(ranked)}{scope} BY {name}{tag}", ranked, {metric: "max" if top else "min"})
