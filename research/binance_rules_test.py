"""Replay the desk's auto-trading rules on Binance's archive (2020 onward), the test HL's 17 days could not give.

Rules, as the daemon runs them (execution/hl_daemon.py ``watch_momentum`` / ``watch_extraordinary``, desk/alt_rally.py):

* flip: BTC 3%+ off its 2-hour low/high, volume 3x+ usual, not fading, funding quiet -> ride it with a 1.5% trail.
* flatten: an up-burst (against the short-alt book) with volume 2x+, not fading -> close the book.
* bank: an up-burst after a 5%+ fall in the prior day, funding quiet -> bank half the shorts.
* trim: BTC's 24h move 2+ daily standard deviations down -> trim a third of the shorts (up: of the longs).

Each rule fires once per 12-hour episode. The short book is proxied by the alt basket: the 40 alts with the most
quote volume over the previous 30 days (BTC, ETH, stablecoins and gold out), equal weight, rebalanced daily, delisted
coins included. A rule earns when what it avoids or takes is positive: flatten and bank earn the basket's next move
(an alt rise the shorts no longer suffer), trim earns the bounce after the down day. Significance: t >= 2 in both
halves of the events (desk standard), net of costs.

Run: PYTHONPATH=. python research/binance_rules_test.py
"""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from desk import alt_rally
from desk.venues import binance_archive as ba

START = "2020-01-01"
EPISODE = pd.Timedelta(hours=12)
COST = 0.0010            # taker in and out plus slippage, per unit of notional traded
STABLE = {"USDC", "BUSD", "TUSD", "FDUSD", "USDP", "DAI", "USDE", "PAXG", "XAUT", "BTCDOM", "DEFI", "USD1"}
HORIZONS = {"4h": 48, "24h": 288, "72h": 864, "7d": 2016}   # in 5-minute bars


def alt_symbols() -> list[str]:
    out = []
    for symbol in ba.symbols():
        if not symbol.endswith("USDT") or "_" in symbol:
            continue
        base = symbol[:-4]
        if base in {"BTC", "ETH"} | STABLE or base.endswith("UP") or base.endswith("DOWN"):
            continue
        out.append(symbol)
    return out


def parallel(fn, items, workers=16):
    with ThreadPoolExecutor(workers) as pool:
        return dict(zip(items, pool.map(fn, items)))


def alt_basket() -> pd.Series:
    """Hourly equal-weight index of the top-40 alts by trailing 30-day quote volume (ranked on yesterday's data)."""
    symbols = alt_symbols()
    daily = parallel(lambda s: ba.klines(s, "1d", START)["quote_volume"], symbols)
    volume = pd.DataFrame({s: v for s, v in daily.items() if len(v)}).sort_index()
    rank_volume = volume.rolling(30, min_periods=20).mean().shift(1)
    members = rank_volume.apply(lambda row: set(row.nlargest(alt_rally.ALT_COUNT).index), axis=1)
    ever = sorted(set().union(*members.dropna()))
    print(f"alt basket: {len(ever)} coins ever in the top {alt_rally.ALT_COUNT}")
    hourly = parallel(lambda s: ba.klines(s, "1h", START)["close"], ever)
    closes = pd.DataFrame({s: c for s, c in hourly.items() if len(c)}).sort_index()
    returns = closes.pct_change(fill_method=None).clip(-0.5, 1.0)
    day_members = members.reindex(returns.index.floor("D"))
    mask = pd.DataFrame([[s in m if isinstance(m, set) else False for s in returns.columns] for m in day_members],
                        index=returns.index, columns=returns.columns)
    basket = returns.where(mask).mean(axis=1).fillna(0.0)
    return (1 + basket).cumprod()


def tstat(values: pd.Series) -> float:
    values = values.dropna()
    if len(values) < 3 or values.std() == 0:
        return float("nan")
    return values.mean() / values.std() * math.sqrt(len(values))


def summarise(name: str, values: pd.Series) -> dict:
    """Mean, t, and t in each half of the events (by time), and whether it passes the desk's bar."""
    values = values.dropna().sort_index()
    half = len(values) // 2
    first, second = values.iloc[:half], values.iloc[half:]
    row = {"test": name, "events": len(values), "mean": values.mean(), "hit": (values > 0).mean(),
           "t": tstat(values), "t_first": tstat(first), "t_second": tstat(second)}
    row["passes"] = bool(row["t_first"] >= 2 and row["t_second"] >= 2)
    return row


def episodes(flags: pd.Series) -> pd.DatetimeIndex:
    """The first flagged bar of each 12-hour episode."""
    out, last = [], None
    for when in flags.index[flags.values]:
        if last is None or when - last >= EPISODE:
            out.append(when)
            last = when
    return pd.DatetimeIndex(out)


def burst_frame(bars: pd.DataFrame, rates: pd.Series) -> pd.DataFrame:
    """Per 5-minute bar: the burst (move off the 2h low/high), its volume ratio, fading, and funding quiet."""
    window = alt_rally.MOMENTUM_MINUTES // 5
    close, high, low, vol = bars["close"], bars["high"], bars["low"], bars["volume"]
    lo, hi = low.rolling(window).min(), high.rolling(window).max()
    up, down = close / lo - 1, close / hi - 1
    move = np.where(up >= -down, up, down)
    frame = pd.DataFrame({"close": close, "move": move}, index=bars.index)
    # bars since the extreme (the move's start), for volume and fading
    lo_age = low.rolling(window).apply(lambda x: window - 1 - int(np.argmin(x)), raw=True)
    hi_age = high.rolling(window).apply(lambda x: window - 1 - int(np.argmax(x)), raw=True)
    frame["age"] = np.where(up >= -down, lo_age, hi_age)
    usual = vol.rolling(288).median().shift(window)
    csum = vol.cumsum()
    frame["vol_ratio"] = np.nan
    age = frame["age"].fillna(0).astype(int).to_numpy()
    cs = csum.to_numpy()
    idx = np.arange(len(cs))
    start = np.maximum(idx - age - 1, 0)
    moved = (cs - cs[start]) / (age + 1)
    frame["vol_ratio"] = moved / usual.to_numpy()
    then = close.shift(6)
    best_up, best_down = hi, lo
    fade_up = (close < then) | ((best_up - close) >= (best_up - lo) / 3)
    fade_down = (close > then) | ((close - best_down) >= (hi - best_down) / 3)
    frame["fading"] = np.where(frame["move"] > 0, fade_up, fade_down)
    hours = 8.0
    apr = rates.reindex(frame.index, method="ffill") * (365 * 24 / hours)
    against = np.where(frame["move"] > 0, -apr, apr)
    frame["quiet"] = against < alt_rally.CROWDED_FUNDING
    # the day before the bounce's low: the low against the day's high up to it
    day_high = high.rolling(288).max()
    frame["selloff"] = (lo / day_high.shift(window)) - 1
    return frame


def trail_trade(bars: pd.DataFrame, entry_at: pd.Timestamp, side: int, trail: float = 0.015,
                max_bars: int = 864) -> float:
    """Return of a trade entered at the next bar's open, exited on a ``trail`` trailing stop (or after 72h)."""
    i = bars.index.searchsorted(entry_at, side="right")
    if i >= len(bars):
        return float("nan")
    seg = bars.iloc[i:i + max_bars]
    entry = seg["open"].iloc[0]
    best = entry
    for o, h, l, c in seg[["open", "high", "low", "close"]].itertuples(index=False):
        if side > 0:
            stop = best * (1 - trail)
            if o <= stop:
                return o / entry - 1 - COST
            if l <= stop:
                return stop / entry - 1 - COST
            best = max(best, h)
        else:
            stop = best * (1 + trail)
            if o >= stop:
                return 1 - o / entry - COST
            if h >= stop:
                return 1 - stop / entry - COST
            best = min(best, l)
    last = seg["close"].iloc[-1]
    return (last / entry - 1 if side > 0 else 1 - last / entry) - COST


def forward(series: pd.Series, when: pd.DatetimeIndex, bars: int, step: str) -> pd.Series:
    """Forward return of ``series`` from each time over ``bars`` of ``step``."""
    s = series.sort_index()
    pos = s.index.searchsorted(when, side="right") - 1
    out = []
    for p in pos:
        q = p + bars
        out.append(s.iloc[q] / s.iloc[p] - 1 if 0 <= p and q < len(s) else np.nan)
    return pd.Series(out, index=when)


def main() -> None:
    btc = ba.klines("BTCUSDT", "5m", START)
    rates = ba.funding("BTCUSDT", START)
    basket = alt_basket()
    basket_5m = basket.reindex(btc.index, method="ffill")
    frame = burst_frame(btc, rates)
    big = frame["move"].abs() >= alt_rally.MOMENTUM_MOVE
    live = big & ~frame["fading"].astype(bool)
    rows, events = [], []

    # flip: ride natural, heavy bursts
    flip_when = episodes(live & (frame["vol_ratio"] >= 3) & frame["quiet"])
    flips = pd.Series([trail_trade(btc, t, int(np.sign(frame.at[t, "move"]))) for t in flip_when], index=flip_when)
    rows.append(summarise("flip: BTC trail 1.5% (net)", flips))
    for label, n in HORIZONS.items():
        side = np.sign(frame.loc[flip_when, "move"])
        rows.append(summarise(f"flip: BTC hold {label} (net)", forward(btc["close"], flip_when, n, "5m") * side - COST))
    events += [("flip", t, frame.at[t, "move"], v) for t, v in flips.items()]

    # flatten: up-burst against the short book, volume 2x+
    flat_when = episodes(live & (frame["move"] > 0) & (frame["vol_ratio"] >= 2))
    for label, n in HORIZONS.items():
        avoided = forward(basket_5m, flat_when, n, "5m") - 2 * COST      # out and back in
        rows.append(summarise(f"flatten: alts next {label} (net)", avoided))
    events += [("flatten", t, frame.at[t, "move"], np.nan) for t in flat_when]

    # bank: up-burst after a 5%+ sell-off, funding quiet
    bank_when = episodes(big & (frame["move"] > 0) & (frame["selloff"] <= alt_rally.SELLOFF_MOVE) & frame["quiet"])
    for label, n in HORIZONS.items():
        rows.append(summarise(f"bank: alts next {label} (net)", forward(basket_5m, bank_when, n, "5m") - 2 * COST))
    events += [("bank", t, frame.at[t, "move"], np.nan) for t in bank_when]

    # trim: 2-sigma BTC day (24h move vs 60 daily returns), checked hourly
    daily = btc["close"].resample("1D").last()
    sd = daily.pct_change().rolling(60).std().shift(1)
    hourly = btc["close"].resample("1h").last()
    move24 = hourly / hourly.shift(24) - 1
    score = move24 / sd.reindex(hourly.index, method="ffill")
    for direction, name in ((-1, "down: trim shorts"), (1, "up: trim longs")):
        when = episodes((score * direction >= alt_rally.EXTRAORDINARY_SIGMA).fillna(False))
        for label, bars in (("24h", 24), ("72h", 72), ("7d", 168)):
            # trimming a short earns the bounce (basket up after a down day); trimming a long earns the drop
            fwd_basket = forward(basket, when, bars, "1h") * (-direction) - 2 * COST
            fwd_btc = forward(hourly, when, bars, "1h") * (-direction) - 2 * COST
            rows.append(summarise(f"trim {name}: alts next {label} (net)", fwd_basket))
            rows.append(summarise(f"trim {name}: BTC next {label} (net)", fwd_btc))
        events += [(f"trim {direction:+d}", t, move24.at[t], np.nan) for t in when]

    # base rate: the basket's unconditional move, for scale
    base = basket.pct_change(24).dropna()
    print(f"basket base rate 24h: mean {base.mean():.2%}, sd {base.std():.2%}")
    table = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(table.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    table.to_csv("research/data/binance_rules_test.csv", index=False)
    pd.DataFrame(events, columns=["rule", "time", "move", "trade_return"]).to_csv(
        "research/data/binance_rules_events.csv", index=False)


if __name__ == "__main__":
    main()
