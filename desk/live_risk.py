"""Risk of the book held now, and where the current regime began.

Realized statistics over the desk's whole history describe an average of every book it has
held. A live view needs two things instead:

* ex-ante risk: today's positions (every venue, HL spot tokens and on-chain wallet tokens) run
  through recent daily
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
from desk.numbers import since as _since

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
        venue = holding.get("venue", "hl-spot")
        rows.append({"key": holding.get("key", f"{venue}:{holding['token']}"), "venue": venue,
                     "symbol": holding["token"], "ref": holding["market"], "value": holding["value"]})
    frame = pd.DataFrame(rows, columns=["key", "venue", "symbol", "ref", "value"])
    return frame[frame["value"].abs() > 0].reset_index(drop=True)


def yahoo_stock_closes(symbol: str, days: int) -> pd.Series:
    """Daily closes of a US-listed stock from Yahoo Finance, dated by UTC day: the stand-in for IB's own
    history while TWS does not answer (a symbol Yahoo does not know raises, so it counts as not priced)."""
    from desk.venues.ib_statement import yahoo_closes

    end = pd.Timestamp.now(tz="UTC").normalize()
    series = yahoo_closes(symbol.replace(" ", "-"), end - pd.Timedelta(days=days + 10), end)
    if series.empty:
        raise KeyError(symbol)
    series.index = pd.DatetimeIndex(series.index).tz_localize("UTC")
    return series


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


# Wallet tokens priced as the coin they wrap or stake, and wallet stablecoins (cash, no exposure).
WALLET_ALIASES = {"WETH": "ETH", "WBTC": "BTC", "CBBTC": "BTC", "CBETH": "ETH", "WSTETH": "ETH",
                  "STETH": "ETH", "WEETH": "ETH", "RETH": "ETH"}
WALLET_STABLECOINS = {"USDC", "USDT", "USDT0", "DAI", "EURC", "USDS", "PYUSD", "USDBC", "USDE", "FDUSD", "GHO"}


def wallet_token_holdings(snapshots: Iterable, mids: dict[str, str], pairs: dict[str, str]) -> list[dict]:
    """On-chain wallet tokens as ex-ante holdings, each priced by the HL market of its underlying.

    Position symbols read ``sVVV (VVV staked on Venice, base)``, ``VVV (unclaimed staking rewards,
    base)`` or ``ETH (arbitrum)``. A staking receipt is priced as the token it stakes (sVVV as
    VVV), a wrapped token as the coin it wraps; stablecoins are cash and are left out. A token
    with no HL market keeps its value with an empty market, so it counts against coverage and is
    listed as not priced instead of disappearing.
    """
    from desk.hl.book_exposure import STABLECOINS, WRAPPED

    holdings = []
    for snapshot in snapshots:
        for position in snapshot.positions:
            text = position.symbol
            token, _, detail = text.partition(" (")
            detail = detail.rstrip(")")
            underlying = detail.split(" staked", 1)[0] if " staked " in detail else token
            symbol = WALLET_ALIASES.get(underlying.upper(), WRAPPED.get(underlying, underlying))
            if symbol.upper() in WALLET_STABLECOINS | STABLECOINS or not position.notional_usd:
                continue
            market = symbol if symbol in mids else pairs.get(symbol, "")
            holdings.append({"key": f"wallet:{position.account}:{text}", "venue": "wallet", "token": text,
                             "market": market if market in mids else "", "value": position.notional_usd})
    return holdings


def hl_markets() -> tuple[dict[str, str], dict[str, str]]:
    """HL mid prices and each spot token's USDC pair, for pricing holdings outside the perp book."""
    from desk.hl.account_analytics import post
    from desk.hl.book_exposure import spot_pairs

    return post({"type": "allMids"}) or {}, spot_pairs(post({"type": "spotMeta"}) or {})


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
            elif row.venue in ("hl-spot", "wallet"):
                series = hl_closes(row.ref, "", days)  # an empty market raises: counted as not priced
            elif row.venue == "ib":
                series = ib_series[row.ref] if row.ref in ib_series else yahoo_stock_closes(row.symbol, days)
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
           "From": _since(returns.index.min()) if len(returns) else "",
           "To": _since(returns.index.max()) if len(returns) else "",
           "Coverage": covered["value"].abs().sum() / gross if gross else np.nan}
    if covered.empty or equity <= 0 or len(returns) < 20:
        return row
    weights = covered.set_index("key")["value"] / equity
    book = returns[weights.index].fillna(0.0) @ weights
    path = (1 + book).cumprod()
    row.update({"Volatility (/yr)": float(book.std() * math.sqrt(365)),
                "Max drawdown": float((path / path.cummax() - 1).min())})
    for confidence in (0.95, 0.99):
        row[f"VaR {confidence:.0%}"] = bootstrap_var(book, confidence)["var"] * equity
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


BTC_BETA_WINDOWS = (60, 90)


def exposure_leg(row, market_group: Callable[[str], str]) -> str:
    """Which part of the book a holding belongs to, for the BTC exposure summary."""
    if row["venue"] == "hl" and row["symbol"] == "BTC":
        return "BTC hedge" if row["value"] < 0 else "BTC long"
    if row["venue"] == "hl":
        if market_group(row["symbol"]) != "Perp Crypto":
            return "TradFi perps"
        return "Alt shorts" if row["value"] < 0 else "Alt longs"
    if row["venue"] == "hl-spot":
        return "HL spot tokens"
    if row["venue"] == "wallet":
        return "Wallet tokens"
    return "Other venues"


def btc_exposure(holdings: pd.DataFrame, returns: pd.DataFrame, btc: pd.Series,
                 windows: tuple[int, ...] = BTC_BETA_WINDOWS,
                 market_group: Callable[[str], str] | None = None) -> pd.DataFrame:
    """Beta-weighted BTC exposure of each holding: value x its beta to BTC over each window.

    ``returns`` are daily returns per holding key (``holding_returns``) and ``btc`` BTC's daily
    returns. BTC itself has a beta of exactly 1, so a BTC short reads as its full notional. A
    holding without a price history keeps its value with no beta, so it shows as unmeasured.
    """
    if market_group is None:
        from desk.hl.account_analytics import perp_market_group as market_group
    rows = []
    for row in holdings.to_dict("records"):
        record = {"Leg": exposure_leg(row, market_group), "Holding": row["symbol"], "Value": row["value"]}
        for days in windows:
            if row["venue"] == "hl" and row["symbol"] == "BTC":
                beta = 1.0
            elif row["key"] in returns:
                pair = pd.concat([returns[row["key"]].rename("asset"), btc.rename("btc")], axis=1).dropna().tail(days)
                beta = float(pair["asset"].cov(pair["btc"]) / pair["btc"].var()) if len(pair) >= 20 and pair["btc"].var() > 0 else np.nan
            else:
                beta = np.nan
            record[f"Beta {days}d"] = beta
            record[f"BTC-eq {days}d"] = row["value"] * beta
        rows.append(record)
    return pd.DataFrame(rows)


def btc_exposure_by_leg(table: pd.DataFrame, windows: tuple[int, ...] = BTC_BETA_WINDOWS) -> pd.DataFrame:
    """Sum the per-holding BTC exposure by leg, with a net row last."""
    columns = ["Value"] + [f"BTC-eq {days}d" for days in windows]
    order = ["Alt shorts", "Alt longs", "HL spot tokens", "Wallet tokens", "TradFi perps", "Other venues",
             "BTC long", "BTC hedge"]
    legs = table.groupby("Leg")[columns].sum(min_count=1)
    legs = legs.reindex([leg for leg in order if leg in legs.index])
    legs.loc["Net"] = legs[columns].sum(min_count=1)
    return legs.reset_index()
