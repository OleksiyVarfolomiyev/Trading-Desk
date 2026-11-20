"""Book and desk outcomes on matched BTC and SPY intervals."""

from collections import defaultdict

import numpy as np
import pandas as pd

from desk.books import decision_marks, invested_class_capital, invested_class_returns

WINDOWS = {"W": 7, "M": 30, "Q": 90, "Y": 365, "All": None}
REGIMES = ("both_up", "btc_up", "spy_up", "both_down")


def _levels(series):
    if series.empty:
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    levels = series.copy()
    levels.index = pd.to_datetime(levels.index, utc=True).normalize()
    return levels.groupby(level=0).last().where(lambda values: values > 0).dropna().sort_index()


def _linked_parts(parts, actual, benchmark):
    """Link daily contributions exactly to the compounded book-minus-index return."""
    prefix = (1 + actual).cumprod().shift(fill_value=1)
    suffix = (1 + benchmark).iloc[::-1].cumprod().shift(fill_value=1).iloc[::-1]
    return parts.mul(prefix * suffix, axis=0).sum().to_dict()


def _parts(weights, benchmark, calendar):
    dates = weights.loc[calendar]
    reference = dates["before"].mean()
    return pd.DataFrame({
        "Picks": dates["equal_price"] - dates["before"] * benchmark,
        "Sizing": dates["price"] - dates["equal_price"],
        "Timing": (dates["before"] - reference) * benchmark + dates["trading"],
        "Market exposure": reference * benchmark,
        "Fees & funding": dates["fees"] + dates["funding"],
    }, index=calendar)


def benchmark_regimes(books):
    """Compare the four market directions on the same SPY close-to-close dates.

    BTC is sampled on the corresponding UTC dates. Both books and the desk share
    the matched sample. Book contributions use prior gross exposure. Desk
    contributions combine both books on prior trading equity, and reconcile to
    desk TWR with the difference in Unattributed.
    """
    crypto, stocks = books["crypto"], books["stocks"]
    end = min(crypto.days[-1], stocks.days[-1])
    market_levels = {"btc": _levels(crypto.benchmark_closes).loc[:end],
                     "spy": _levels(stocks.benchmark_closes).loc[:end]}
    desk = _levels(stocks.desk.index).loc[:end]
    book_returns = {name: invested_class_returns(book).clip(lower=-1) for name, book in books.items()}
    invested = {name: (1 + daily).cumprod() for name, daily in book_returns.items()}
    btc, spy = market_levels["btc"], market_levels["spy"]
    intervals = []
    for start, stop in zip(spy.index[:-1], spy.index[1:]):
        if not all(start in levels.index and stop in levels.index for levels in (btc, desk, *invested.values())):
            continue
        if any(levels.loc[start] <= 0 for levels in invested.values()):
            continue
        btc_move = btc.loc[stop] / btc.loc[start] - 1
        spy_move = spy.loc[stop] / spy.loc[start] - 1
        if btc_move == 0 or spy_move == 0:
            continue
        regime = ("both_up" if spy_move > 0 else "btc_up") if btc_move > 0 else ("spy_up" if spy_move > 0 else "both_down")
        intervals.append((start, stop, regime))
    if not intervals:
        return {}, {}

    calendar = pd.date_range(intervals[0][0], end, freq="D")
    market_daily = {name: levels.pct_change(fill_method=None).reindex(calendar).fillna(0.0)
                    for name, levels in market_levels.items()}
    desk_daily = desk.reindex(calendar).ffill().pct_change(fill_method=None).fillna(0.0)
    actual = {"desk": desk_daily, **{name: daily.reindex(calendar).fillna(0.0) for name, daily in book_returns.items()}}
    labels = [("crypto", "crypto", "btc"), ("stocks", "stocks", "spy"),
              ("desk_btc", "desk", "btc"), ("desk_spy", "desk", "spy")]
    levels = {"desk": desk, **invested}
    marks = {name: decision_marks(book).reindex(calendar).fillna(0.0) for name, book in books.items()}
    equity = stocks.equity.shift(1).reindex(calendar)
    desk_weights = {name: frame.div(equity.where(equity > 0), axis=0).fillna(0.0)
                    for name, frame in marks.items()}
    book_weights = {name: marks[name].div(invested_class_capital(book).reindex(calendar), axis=0).fillna(0.0)
                    for name, book in books.items()}
    indices = {"crypto": "btc", "stocks": "spy"}

    result, dates_by_regime = {}, {}
    from desk.drawdown import with_active_window   # + "Active": since active management began

    for window, days in with_active_window(WINDOWS, end).items():
        matched = [(start, stop, regime) for start, stop, regime in intervals
                   if days is None or stop >= end - pd.Timedelta(days=days - 1)]
        if not matched:
            continue
        span = calendar[(calendar > matched[0][0]) & (calendar <= matched[-1][1])]
        underlying = {name: market_daily[index].loc[span] for name, index in indices.items()}
        basis = {name: _parts(weights, underlying[name], span) for name, weights in book_weights.items()}
        basis["desk"] = sum(_parts(weights, underlying[name], span) for name, weights in desk_weights.items())
        groups = defaultdict(list)
        for start, stop, regime in matched:
            groups[regime].append((start, stop))
        result[window], dates_by_regime[window] = {}, {}
        for regime in REGIMES:
            sessions = groups[regime]
            if not sessions:
                continue
            dates = pd.DatetimeIndex([day for start, stop in sessions for day in pd.date_range(start, stop)[1:]])
            dates_by_regime[window][regime] = set(dates)
            comparisons = {}
            for key, portfolio, market in labels:
                r, b = actual[portfolio].loc[dates], market_daily[market].loc[dates]
                parts = basis[portfolio].loc[dates].copy()
                parts["Market exposure"] -= b
                parts["Unattributed"] = r - b - parts.sum(axis=1)
                gaps = np.array([
                    levels[portfolio].loc[stop] / levels[portfolio].loc[start]
                    - market_levels[market].loc[stop] / market_levels[market].loc[start]
                    for start, stop in sessions
                ])
                comparisons[key] = {
                    "beat": int((gaps > 1e-8).sum()), "lag": int((gaps < -1e-8).sum()),
                    "portfolio": float((1 + r).prod()), "index": float((1 + b).prod()),
                    "parts": _linked_parts(parts, r, b),
                }
            result[window][regime] = {"sessions": len(sessions), "comparisons": comparisons}
    return result, dates_by_regime
