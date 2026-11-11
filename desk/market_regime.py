"""Market regime watch: is an alt season starting, or an equity sell-off?

The book is built for one regime: short alts (against BTC strength) and long TradFi. The two
moves that break it are an alt season (alts outrunning BTC across the board) and an equity
sell-off. Each is read from daily closes with four signals, each judged against its own past
year so a volatile market does not trip it on noise:

Alt season (a basket of the most traded HL alts, equal weight, against BTC):
  * A1 alts beat BTC by a top-decile 7-day margin (and by at least 5%);
  * A2 at least 70% of the alts beat BTC over 7 days (breadth, not a few names);
  * A3 alts beat BTC by at least 10% over 30 days (it has lasted);
  * A4 the alt/BTC ratio is above its 20-day average, and that average is rising.

Equity sell-off (SPY and VIX from Yahoo Finance):
  * E1 SPY is 5% or more below its 60-day high;
  * E2 SPY is below its 50-day average, with the 20-day below the 50-day;
  * E3 VIX is 25 or more, or up 40% in five sessions;
  * E4 SPY's 10-day realized volatility is in its top decile for the year.

Level: "notice" when two signals hold today; "high" when three hold on at least three of the
last five days (alts) or two of the last three sessions (equities), so one wild day is not a
regime. The trading daemon trims the exposed positions only on "high" (execution/hl_daemon.py).

    python -m desk.market_regime              # today's reading
    python -m desk.market_regime --history    # the days each level held over the past year
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "market_regime.json"
STATE_MAX_AGE_SECONDS = 6 * 3600
ALT_COUNT = 30
HISTORY_DAYS = 500
EXCLUDED = {"BTC", "ETH", "USDC", "USDT", "USDE", "DAI", "PAXG", "XAUT"}  # the benchmark, majors, stables, gold
LEVELS = ("none", "notice", "high")


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def alt_universe(count: int = ALT_COUNT) -> list[str]:
    """The most traded HL perps on the main DEX (24h notional), majors and stables left out."""
    from desk.hl.account_analytics import post

    meta, contexts = post({"type": "metaAndAssetCtxs"})
    rows = []
    for asset, context in zip(meta["universe"], contexts):
        name = asset["name"]
        if asset.get("isDelisted") or name in EXCLUDED or name.startswith("k"):
            continue
        rows.append((float(context.get("dayNtlVlm") or 0), name))
    return [name for _, name in sorted(rows, reverse=True)[:count]]


def hl_closes(coins: list[str], days: int = HISTORY_DAYS) -> pd.DataFrame:
    """Daily closes (UTC days) of HL perps, one column per coin; coins without history are left out."""
    from desk.hl.trade_analytics import fetch_daily_candles

    columns = {}
    for coin in coins:
        try:
            candles = fetch_daily_candles(coin, "", lookback_days=days)
        except Exception:
            continue
        if len(candles):
            columns[coin] = pd.Series(candles["close"].to_numpy(), index=pd.DatetimeIndex(candles["time"]).normalize())
    return pd.DataFrame(columns).sort_index()


def yahoo_series(ticker: str, days: int = HISTORY_DAYS) -> pd.Series:
    from desk.venues.ib_statement import yahoo_closes

    end = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    return yahoo_closes(ticker, end - pd.Timedelta(days=days), end)


# ---------------------------------------------------------------------------
# Signals (pure: closes in, daily signal table out)
# ---------------------------------------------------------------------------

def _trailing_rank(series: pd.Series, window: int = 365) -> pd.Series:
    """Each day's value as a percentile of the previous ``window`` days (0 to 1)."""
    return series.rolling(window, min_periods=120).apply(lambda values: (values[:-1] < values[-1]).mean(), raw=True)


def alt_signals(alts: pd.DataFrame, btc: pd.Series) -> pd.DataFrame:
    """Daily alt-season signals A1 to A4 and the measures behind them."""
    returns = alts.pct_change(fill_method=None).clip(-0.5, 1.0)
    basket = (1 + returns.mean(axis=1, skipna=True).fillna(0)).cumprod()
    btc = btc.reindex(basket.index).ffill()
    ratio = basket / btc
    rel7, rel30 = ratio.pct_change(7, fill_method=None), ratio.pct_change(30, fill_method=None)
    alt7 = alts.pct_change(7, fill_method=None)
    btc7 = btc.pct_change(7, fill_method=None)
    beating = alt7.gt(btc7, axis=0).where(alt7.notna())
    breadth = beating.sum(axis=1) / alt7.notna().sum(axis=1).replace(0, np.nan)
    sma20 = ratio.rolling(20).mean()
    frame = pd.DataFrame({"rel7": rel7, "rel30": rel30, "breadth7": breadth, "rel7_rank": _trailing_rank(rel7)})
    frame["A1"] = (frame["rel7_rank"] >= 0.90) & (frame["rel7"] >= 0.05)
    frame["A2"] = frame["breadth7"] >= 0.70
    frame["A3"] = frame["rel30"] >= 0.10
    frame["A4"] = (ratio > sma20) & (sma20 > sma20.shift(5))
    return frame


def equity_signals(spy: pd.Series, vix: pd.Series) -> pd.DataFrame:
    """Daily equity sell-off signals E1 to E4 (trading days) and the measures behind them."""
    spy = spy.sort_index()
    vix = vix.sort_index().reindex(spy.index).ffill()
    drawdown = spy / spy.rolling(60, min_periods=20).max() - 1
    sma20, sma50 = spy.rolling(20).mean(), spy.rolling(50).mean()
    vol10 = spy.pct_change(fill_method=None).rolling(10).std() * np.sqrt(252)
    frame = pd.DataFrame({"drawdown60": drawdown, "vix": vix, "vix5": vix.pct_change(5, fill_method=None),
                          "vol10": vol10, "vol10_rank": _trailing_rank(vol10, 252)})
    frame["E1"] = frame["drawdown60"] <= -0.05
    frame["E2"] = (spy < sma50) & (sma20 < sma50)
    frame["E3"] = (frame["vix"] >= 25) | (frame["vix5"] >= 0.40)
    frame["E4"] = frame["vol10_rank"] >= 0.90
    return frame


def levels(signals: pd.DataFrame, names: list[str], confirm: int, window: int) -> pd.Series:
    """"none", "notice" (two signals today) or "high" (three on ``confirm`` of the last ``window`` days)."""
    count = signals[names].fillna(False).astype(int).sum(axis=1)
    strong = (count >= 3).astype(int).rolling(window, min_periods=1).sum() >= confirm
    return pd.Series(np.where(strong & (count >= 3), "high", np.where(count >= 2, "notice", "none")), index=signals.index)


# ---------------------------------------------------------------------------
# Reading and saved state
# ---------------------------------------------------------------------------

def _describe_alts(row: pd.Series) -> str:
    return (f"alts {row['rel7']:+.1%} vs BTC over 7 days (top {1 - row['rel7_rank']:.0%} of the year), "
            f"{row['breadth7']:.0%} of alts beat BTC, {row['rel30']:+.1%} over 30 days")


def _describe_equities(row: pd.Series) -> str:
    return (f"SPY {row['drawdown60']:+.1%} from its 60-day high, VIX {row['vix']:.1f} ({row['vix5']:+.0%} in 5 sessions), "
            f"10-day volatility {row['vol10']:.0%}")


def evaluate() -> dict:
    """Today's reading of both regimes, with the signals and measures behind it."""
    alts = hl_closes(alt_universe())
    btc = hl_closes(["BTC"])["BTC"]
    alt = alt_signals(alts, btc).dropna(subset=["rel7"])
    alt_level = levels(alt, ["A1", "A2", "A3", "A4"], confirm=3, window=5)
    equity = equity_signals(yahoo_series("SPY"), yahoo_series("^VIX")).dropna(subset=["drawdown60"])
    equity_level = levels(equity, ["E1", "E2", "E3", "E4"], confirm=2, window=3)

    def part(frame, level, names, describe):
        row = frame.iloc[-1]
        return {"level": str(level.iloc[-1]), "as_of": frame.index[-1].strftime("%Y-%m-%d"),
                "signals": {name: bool(row[name]) for name in names}, "detail": describe(row),
                "days_high_last_30": int((level.iloc[-30:] == "high").sum())}

    return {"as_of": pd.Timestamp.now(tz="UTC").isoformat(), "alts": len(alts.columns),
            "alt_season": part(alt, alt_level, ["A1", "A2", "A3", "A4"], _describe_alts),
            "equity_selloff": part(equity, equity_level, ["E1", "E2", "E3", "E4"], _describe_equities)}


def current(max_age: float = STATE_MAX_AGE_SECONDS, path: Path = STATE_FILE) -> dict:
    """The saved reading if fresh enough, else a new one (saved). Raises only if nothing can be read."""
    try:
        saved = json.loads(path.read_text())
        if time.time() - pd.Timestamp(saved["as_of"]).timestamp() < max_age:
            return saved
    except (OSError, ValueError, KeyError):
        pass
    state = evaluate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1))
    return state


def notices(state: dict | None) -> list[tuple[str, str]]:
    """Highlight rows for the dashboards: (title, text) for each regime at "notice" or "high"."""
    rows = []
    if not state:
        return rows
    for key, name, meaning in (("alt_season", "Alt season", "the alt shorts are exposed"),
                               ("equity_selloff", "Equity sell-off", "the TradFi longs are exposed")):
        part = state.get(key) or {}
        level = part.get("level", "none")
        if level == "none":
            continue
        held = sum(bool(value) for value in part.get("signals", {}).values())
        verdict = ("high confidence: the trading daemon's regime guard trims the exposed HL positions when it is on"
                   if level == "high" else "watching; no trimming yet")
        rows.append((f"Regime change: {name.lower()} ({level})",
                     f"{held} of 4 signals: {part.get('detail', '')}. {meaning.capitalize()}; {verdict}."))
    return rows


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", action="store_true", help="show the days each level held over the past year")
    args = parser.parse_args(argv)
    if args.history:
        alts = hl_closes(alt_universe())
        btc = hl_closes(["BTC"])["BTC"]
        alt = alt_signals(alts, btc).dropna(subset=["rel7"])
        equity = equity_signals(yahoo_series("SPY"), yahoo_series("^VIX")).dropna(subset=["drawdown60"])
        for name, frame, level in (("ALT SEASON", alt, levels(alt, ["A1", "A2", "A3", "A4"], 3, 5)),
                                   ("EQUITY SELL-OFF", equity, levels(equity, ["E1", "E2", "E3", "E4"], 2, 3))):
            level = level[level.index >= level.index[-1] - pd.Timedelta(days=365)]
            print(f"{name}: {(level == 'high').sum()} high and {(level == 'notice').sum()} notice days of {len(level)}")
            runs = (level != level.shift()).cumsum()
            for _, run in level[level == "high"].groupby(runs[level == "high"]):
                print(f"  high {run.index[0]:%Y-%m-%d} to {run.index[-1]:%Y-%m-%d} ({len(run)} days)")
        return 0
    state = current(max_age=0)
    for key in ("alt_season", "equity_selloff"):
        part = state[key]
        print(f"{key}: {part['level']} (as of {part['as_of']}) {part['signals']} — {part['detail']}")
    for title, text in notices(state):
        print(f"NOTICE {title}: {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
