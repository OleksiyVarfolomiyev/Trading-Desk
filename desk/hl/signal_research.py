"""Which simple signals predict next week's alt returns on HL, tested honestly.

A signal is worth trading only if it survives what a backtest usually hides, so this harness:

* builds the universe as it was on each day: the 40 main-DEX perps with the highest trailing
  30-day median dollar volume on that day (at least $2M a day and 60 days of history), from
  daily candles of every perp HL lists, delisted ones included; BTC, ETH and stablecoins are
  left out (BTC is the benchmark);
* forms a portfolio every day from that day's close: long the top third by the signal, short the
  bottom third, equal weight, held 7 days;
* gives every position the executor's stop, 3 x ATR(14) from entry, checked on each day's high
  and low;
* charges a taker fee and 5 bp of slippage each way on every position every week (more than the
  desk pays with maker entries), and funding where its history is saved locally;
* reads significance from overlapping weekly cohorts as if only one in seven were independent;
* validates a signal only if it holds on the whole sample (t of 2 or more) and, with the
  direction its first half shows, on the second half too (t of 1.5 or more).

Three results per signal: the long-short spread (market-neutral), the short leg outright (the
trade the desk actually makes), and the short leg hedged with a BTC long.

    python -m desk.hl.signal_research               # refresh candles (slow the first time) and test
    python -m desk.hl.signal_research --no-refresh  # test on the saved candles
    python -m desk.hl.signal_research --backfill-funding   # save funding history, slowly (hours)

Results are saved to ``data/signal_research.json``; the screener (desk/screener.py) scores
candidates with the validated signals only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PANEL_FILE = ROOT / "data" / "api_cache" / "signal_panel.csv.gz"   # plain CSV: both Python environments read it
RESULTS_FILE = ROOT / "data" / "signal_research.json"
HISTORY_DAYS = 560
UNIVERSE_SIZE = 40
MIN_DOLLAR_VOLUME = 2_000_000.0
MIN_HISTORY_DAYS = 60
HOLD_DAYS = 7
STOP_ATR_MULTIPLE = 3.0
COST_PER_SIDE = 0.00045 + 0.0005      # taker fee + slippage
REQUEST_SPACING_SECONDS = 2.5         # candle requests share HL's rate limit with the trading daemon
EXCLUDED = {"BTC", "ETH", "USDC", "USDT", "USDE", "DAI", "PAXG", "XAUT"}
VALID_T_FULL, VALID_T_SECOND = 2.0, 1.5

SIGNALS = {
    "rel7": "7-day return vs BTC",
    "rel30": "30-day return vs BTC",
    "rel90": "90-day return vs BTC",
    "from_high60": "Distance from the 60-day high",
    "vol30": "30-day volatility",
    "volume_surge": "7-day volume vs 30-day volume",
    "funding7": "7-day funding paid by longs",
}


# ---------------------------------------------------------------------------
# Data: daily candles with volume for every main-DEX perp
# ---------------------------------------------------------------------------

def all_perps() -> list[str]:
    """Every perp the main DEX lists, delisted ones included (so dead coins stay in past universes)."""
    from desk.hl.trade_analytics import post_info

    return [asset["name"] for asset in post_info({"type": "meta"})["universe"]]


def _candles(coin: str, start_ms: int) -> pd.DataFrame:
    from desk.hl.trade_analytics import post_info

    rows = post_info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1d", "startTime": start_ms,
                                                        "endTime": int(time.time() * 1000)}}) or []
    frame = pd.DataFrame(rows)
    if frame.empty:  # a coin with no candles in the window (delisted long ago): an empty, date-indexed frame
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
    frame.index = pd.to_datetime(pd.to_numeric(frame["t"]), unit="ms", utc=True).dt.normalize()
    frame = frame.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return frame[["open", "high", "low", "close", "volume"]].astype(float)


def refresh_panel(days: int = HISTORY_DAYS, spacing: float = REQUEST_SPACING_SECONDS, log=print) -> dict[str, pd.DataFrame]:
    """Download (or extend) every perp's daily candles; saved after each coin so a stop loses nothing."""
    panel = load_panel()
    today = pd.Timestamp.now(tz="UTC").normalize()
    coins = all_perps()
    for index, coin in enumerate(coins):
        saved = panel.get(coin)
        if saved is not None and len(saved) and saved.index[-1] >= today - pd.Timedelta(days=1):
            continue  # already has yesterday's completed candle
        start = (saved.index[-1] if saved is not None and len(saved) else today - pd.Timedelta(days=days))
        try:
            fresh = _candles(coin, int(start.timestamp() * 1000))
        except Exception as exc:
            log(f"  {coin}: candles not read ({type(exc).__name__})")
            time.sleep(spacing)
            continue
        fresh = fresh[fresh.index < today]  # completed days only
        if fresh.empty and (saved is None or saved.empty):
            time.sleep(spacing)
            continue
        merged = fresh if saved is None or saved.empty else pd.concat([saved[saved.index < fresh.index[0]] if len(fresh) else saved, fresh])
        panel[coin] = merged[~merged.index.duplicated(keep="last")].sort_index()
        if index % 20 == 0:
            save_panel(panel)
            log(f"  candles: {index + 1} of {len(coins)}")
        time.sleep(spacing)
    save_panel(panel)
    return panel


COLUMNS = ["open", "high", "low", "close", "volume"]


def load_panel(path: Path = PANEL_FILE) -> dict[str, pd.DataFrame]:
    """Saved daily candles, one frame per coin (date-indexed, UTC)."""
    try:
        long = pd.read_csv(path)
    except (OSError, ValueError, EOFError):
        return {}
    long["date"] = pd.to_datetime(long["date"], utc=True)
    return {coin: rows.set_index("date")[COLUMNS].astype(float).sort_index() for coin, rows in long.groupby("coin")}


def save_panel(panel: dict[str, pd.DataFrame], path: Path = PANEL_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames_ = [frame[COLUMNS].assign(coin=coin).rename_axis("date").reset_index() for coin, frame in panel.items() if len(frame)]
    long = pd.concat(frames_, ignore_index=True) if frames_ else pd.DataFrame(columns=["date", "coin", *COLUMNS])
    long["date"] = pd.to_datetime(long["date"], utc=True).dt.strftime("%Y-%m-%d")
    temporary = path.with_name(path.name + ".tmp")
    long[["date", "coin", *COLUMNS]].to_csv(temporary, index=False, compression="gzip")
    temporary.replace(path)


def saved_funding(coins: list[str]) -> pd.DataFrame:
    """Daily funding (sum of the hourly rates, paid by longs) for coins whose history is saved locally."""
    from desk.hl import trade_analytics as ta

    columns = {}
    for coin in coins:
        try:
            cached = ta._read_market_cache(ta._cache_name("funding", coin, ""))
        except Exception:
            cached = None
        if cached is None or cached[0].empty:
            continue
        rows = cached[0]
        columns[coin] = rows.set_index(pd.DatetimeIndex(rows["time"]).normalize())["fundingRate"].groupby(level=0).sum()
    return pd.DataFrame(columns)


def backfill_funding(coins: list[str], days: int = HISTORY_DAYS, pause: float = 8.0, log=print) -> None:
    """Save each coin's hourly funding history (about 26 slow requests a coin; resumable)."""
    from desk.hl import trade_analytics as ta

    ta.FULL_PAGE_PAUSE_SECONDS = pause  # well under HL's rate limit, which the trading daemon shares
    for index, coin in enumerate(coins):
        try:
            ta.fetch_funding_history(coin, "", lookback_days=days, allow_empty=True)
            log(f"  funding: {coin} saved ({index + 1} of {len(coins)})")
        except Exception as exc:
            log(f"  funding: {coin} failed ({type(exc).__name__}: {exc})")
        time.sleep(pause)


# ---------------------------------------------------------------------------
# Frames, universe and signals (pure: frames in, frames out)
# ---------------------------------------------------------------------------

def frames(panel: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Aligned date x coin frames: close, high, low and dollar volume."""
    out = {name: pd.DataFrame({coin: frame[name] for coin, frame in panel.items() if len(frame)}).sort_index()
           for name in ("close", "high", "low")}
    out["dollar_volume"] = pd.DataFrame({coin: frame["volume"] * frame["close"] for coin, frame in panel.items() if len(frame)}).sort_index()
    return out


def universe(close: pd.DataFrame, dollar_volume: pd.DataFrame, size: int = UNIVERSE_SIZE,
             min_volume: float = MIN_DOLLAR_VOLUME) -> pd.DataFrame:
    """True where a coin is tradable on a day: among the ``size`` most liquid, with enough volume and history."""
    liquidity = dollar_volume.rolling(30, min_periods=20).median()
    history = close.notna().cumsum() >= MIN_HISTORY_DAYS
    eligible = liquidity.where(history & close.notna())
    eligible = eligible.drop(columns=[coin for coin in eligible.columns if coin in EXCLUDED])
    rank = eligible.rank(axis=1, ascending=False, method="first")  # ties (equal volume) still give exactly ``size`` names
    return (rank <= size) & (eligible >= min_volume)


def atr(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder's ATR per coin (as desk.hl.trade_analytics.wilder_atr, on frames)."""
    previous = close.shift(1)
    true_range = np.maximum(high - low, np.maximum((high - previous).abs(), (low - previous).abs()))
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def signal_frames(data: dict[str, pd.DataFrame], funding: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Each signal as a date x coin frame, known at that day's close."""
    close, volume = data["close"], data["dollar_volume"]
    btc = close["BTC"]
    relative = close.div(btc, axis=0)
    signals = {
        "rel7": relative.pct_change(7, fill_method=None),
        "rel30": relative.pct_change(30, fill_method=None),
        "rel90": relative.pct_change(90, fill_method=None),
        "from_high60": close / close.rolling(60, min_periods=30).max() - 1,
        "vol30": close.pct_change(fill_method=None).rolling(30, min_periods=20).std(),
        "volume_surge": volume.rolling(7).mean() / volume.rolling(30, min_periods=20).mean() - 1,
    }
    if funding is not None and not funding.empty:
        signals["funding7"] = funding.reindex(close.index).rolling(7, min_periods=5).sum()
    return signals


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def position_returns(data: dict[str, pd.DataFrame], hold: int = HOLD_DAYS, stop_multiple: float = STOP_ATR_MULTIPLE,
                     funding: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The return of a long and of a short opened at each day's close and held ``hold`` days, date x coin.

    Each has the executor's stop (``stop_multiple`` x ATR from entry, checked on daily highs and lows; a
    day that touches it exits at the stop), pays ``COST_PER_SIDE`` each way, and funding where known
    (longs pay it, shorts receive it, for the days held).
    """
    close, high, low = data["close"], data["high"], data["low"]
    stop = (stop_multiple * atr(high, low, close) / close).where(lambda frame: frame > 0)
    long_return = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    short_return = long_return.copy()
    long_open = pd.DataFrame(True, index=close.index, columns=close.columns)
    short_open = long_open.copy()
    held_funding_long = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    held_funding_short = held_funding_long.copy()
    paid = funding.reindex(index=close.index, columns=close.columns).fillna(0.0) if funding is not None and not funding.empty else None
    for day in range(1, hold + 1):
        up = high.shift(-day) / close - 1          # the day's best and worst against the entry
        down = low.shift(-day) / close - 1
        hit_long = long_open & (down <= -stop)
        hit_short = short_open & (up >= stop)
        long_return = long_return.mask(hit_long, -stop)
        short_return = short_return.mask(hit_short, -stop)
        if paid is not None:
            held_funding_long += paid.shift(-day).where(long_open, 0.0).fillna(0.0)
            held_funding_short += paid.shift(-day).where(short_open, 0.0).fillna(0.0)
        long_open &= ~hit_long
        short_open &= ~hit_short
    final = close.shift(-hold) / close - 1
    long_return = long_return.mask(long_open, final)
    short_return = short_return.mask(short_open, -final)
    known = final.notna() & stop.notna()
    long_return = (long_return - 2 * COST_PER_SIDE - held_funding_long).where(known)
    short_return = (short_return - 2 * COST_PER_SIDE + held_funding_short).where(known)
    return long_return, short_return


def _t(series: pd.Series, hold: int = HOLD_DAYS) -> float:
    series = series.dropna()
    if len(series) < 3 * hold or series.std() == 0:
        return float("nan")
    return float(series.mean() / series.std() * np.sqrt(len(series) / hold))  # overlapping cohorts: one in ``hold`` independent


def test_signal(signal: pd.DataFrame, tradable: pd.DataFrame, long_return: pd.DataFrame, short_return: pd.DataFrame,
                btc_forward: pd.Series, min_names: int = 12) -> dict:
    """Daily cohorts: long the top third by ``signal``, short the bottom third; the results the module describes."""
    spreads, shorts, hedged, dates = [], [], [], []
    for date in signal.index:
        row = signal.loc[date].where(tradable.loc[date]).dropna()
        row = row[long_return.loc[date].reindex(row.index).notna()]
        if len(row) < min_names:
            continue
        third = len(row) // 3
        ordered = row.sort_values().index
        low_names, high_names = ordered[:third], ordered[-third:]
        long_leg = long_return.loc[date, high_names].mean()
        short_leg = short_return.loc[date, low_names].mean()
        spreads.append(long_leg + short_leg)
        shorts.append(short_leg)
        hedged.append(short_leg + btc_forward.get(date, np.nan))
        dates.append(date)
    spread = pd.Series(spreads, index=dates, dtype=float)
    short = pd.Series(shorts, index=dates, dtype=float)
    short_hedged = pd.Series(hedged, index=dates, dtype=float)
    if len(spread) < 6 * HOLD_DAYS:
        return {"cohorts": len(spread), "validated": False, "reason": "too little history"}
    middle = len(spread) // 2
    first, second = spread.iloc[:middle], spread.iloc[middle:]
    direction = 1.0 if first.mean() >= 0 else -1.0     # chosen on the first half, tested on the second
    t_full, t_first, t_second = _t(spread), _t(first), _t(second)
    # With the direction reversed the legs swap (short the top third), so the short-leg figures apply only as shown.
    validated = bool(abs(t_full) >= VALID_T_FULL and np.sign(spread.mean()) == direction and direction * t_second >= VALID_T_SECOND)
    return {"cohorts": len(spread), "from": f"{spread.index[0]:%Y-%m-%d}", "to": f"{spread.index[-1]:%Y-%m-%d}",
            "spread_week": float(spread.mean()), "t_full": t_full, "t_first": t_first, "t_second": t_second,
            "direction": float(np.sign(spread.mean())), "validated": validated,
            "short_low_week": float(short.mean()), "short_low_t": _t(short),
            "short_low_hedged_week": float(short_hedged.mean()), "short_low_hedged_t": _t(short_hedged)}


def run(panel: dict[str, pd.DataFrame] | None = None, log=print) -> dict:
    """Test every signal on the saved candles and save the results."""
    panel = panel if panel is not None else load_panel()
    if "BTC" not in panel:
        raise RuntimeError("no saved candles: run python -m desk.hl.signal_research first")
    data = frames(panel)
    tradable = universe(data["close"], data["dollar_volume"])
    ever = [coin for coin in tradable.columns if tradable[coin].any()]
    funding = saved_funding(ever)
    coverage = len(funding.columns) / len(ever) if ever else 0.0
    use_funding = funding if coverage >= 0.8 else None  # partial funding would bias coins with it against those without
    long_return, short_return = position_returns(data, funding=use_funding)
    btc_forward = data["close"]["BTC"].shift(-HOLD_DAYS) / data["close"]["BTC"] - 1
    signals = signal_frames(data, use_funding)
    results = {name: {"label": SIGNALS[name], **test_signal(frame, tradable, long_return, short_return, btc_forward)}
               for name, frame in signals.items()}
    state = {"as_of": pd.Timestamp.now(tz="UTC").isoformat(), "universe": {"size": UNIVERSE_SIZE, "ever_tradable": len(ever),
             "days": int(tradable.any(axis=1).sum())}, "funding": {"coverage": coverage, "used": use_funding is not None},
             "assumptions": {"hold_days": HOLD_DAYS, "stop_atr": STOP_ATR_MULTIPLE, "cost_per_side": COST_PER_SIDE},
             "signals": results}
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(state, indent=1))
    return state


def load_results(path: Path = RESULTS_FILE) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def results_frame(state: dict) -> pd.DataFrame:
    rows = []
    for name, item in state["signals"].items():
        if "spread_week" not in item:
            rows.append({"Signal": item["label"], "Validated": "no (too little history)"})
            continue
        rows.append({"Signal": item["label"], "Long-short (/wk)": item["spread_week"], "t": item["t_full"],
                     "t first half": item["t_first"], "t second half": item["t_second"],
                     "Short the low third (/wk)": item["short_low_week"], "t short": item["short_low_t"],
                     "Short, BTC-hedged (/wk)": item["short_low_hedged_week"], "t hedged": item["short_low_hedged_t"],
                     "Validated": "YES" if item["validated"] else "no"})
    return pd.DataFrame(rows)


def show_signal_research() -> None:
    """The results table for the notebooks."""
    from desk.hl.account_analytics import display_interactive_table

    state = load_results()
    if not state:
        print("No signal research yet: run python -m desk.hl.signal_research")
        return
    frame = results_frame(state)
    percent = lambda value: "" if value != value else f"{value:.2%}"
    number = lambda value: "" if value != value else f"{value:.1f}"
    display_interactive_table(frame, title="SIGNAL RESEARCH: NEXT-WEEK RETURNS, AFTER COSTS AND STOPS",
                              formatters={"Long-short (/wk)": percent, "Short the low third (/wk)": percent,
                                          "Short, BTC-hedged (/wk)": percent, "t": number, "t first half": number,
                                          "t second half": number, "t short": number, "t hedged": number},
                              key_columns={"t": "abs"})
    funding = state["funding"]
    print(f"As of {state['as_of'][:10]}: {state['universe']['ever_tradable']} coins were in the top {state['universe']['size']} by "
          f"volume at some point over {state['universe']['days']} days. Funding {'included' if funding['used'] else 'not included'} "
          f"(saved for {funding['coverage']:.0%} of them). A signal is validated at t >= {VALID_T_FULL:g} on the whole sample and "
          f">= {VALID_T_SECOND:g} on the second half in the first half's direction.")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-refresh", action="store_true", help="test on the saved candles without downloading")
    parser.add_argument("--backfill-funding", action="store_true", help="save funding history for the tradable coins (slow)")
    args = parser.parse_args(argv)
    panel = load_panel() if args.no_refresh else refresh_panel()
    if args.backfill_funding:
        data = frames(panel)
        tradable = universe(data["close"], data["dollar_volume"])
        backfill_funding([coin for coin in tradable.columns if tradable[coin].any()])
    state = run(panel)
    pd.set_option("display.width", 220)
    print(results_frame(state).to_string(index=False, float_format=lambda value: f"{value:.4f}"))
    print(f"funding coverage {state['funding']['coverage']:.0%} (used: {state['funding']['used']}); "
          f"{state['universe']['ever_tradable']} coins ever tradable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
