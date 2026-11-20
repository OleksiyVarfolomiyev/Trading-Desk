"""Screener research: which window of relative weakness (and which entry timing) predicts the next days, net of costs.

(a) Short signal windows: each day, among the 40 most liquid main-DEX perps, the weakest coins by return vs BTC over a window
    are shorted for ``h`` days (side-adjusted return vs BTC, less a 9 bps round trip). Windows 1, 3, 7, 14, 30, 60 days, and the
    combination "weak over 30 days AND bouncing over 3 days". Non-overlapping samples (one entry per ``h`` days), t-statistic of the
    mean, and both halves of the history.
(b) Fade: coins stretched above their 30-day mean and up vs BTC over 3 days, shorted for ``h`` days.
(c) Timing of the desk's own short entries (data/closed_trades_crypto.csv): the entry day against placebo entries on other days of
    the same coin (+/-3 and +/-7 days), side-adjusted, over the next 1, 3 and 7 days.

    python research/screener_strategies.py      # writes data/screener_strategies.json and research/screener_strategies.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desk.hl import signal_research as research  # noqa: E402

COST = 0.0009            # round trip, as the desk's research uses
WINDOWS = (1, 3, 7, 14, 30, 60)
HOLDS = (1, 3, 7, 14)
TOP = 5                  # the candidates a day: the weakest five of the liquid forty
OUT_JSON = ROOT / "data" / "screener_strategies.json"
OUT_MD = ROOT / "research" / "screener_strategies.md"


def tstat(values) -> tuple[float, float, int]:
    values = pd.Series(values).dropna()
    n = len(values)
    if n < 8 or values.std(ddof=1) == 0:
        return float("nan"), float("nan"), n
    return float(values.mean()), float(values.mean() / (values.std(ddof=1) / np.sqrt(n))), n


def halves(series: pd.Series) -> tuple[tuple, tuple]:
    mid = len(series) // 2
    return tstat(series.iloc[:mid]), tstat(series.iloc[mid:])


def verdict(series: pd.Series) -> dict:
    """The short side's net return per trade (positive is a gain), t overall and in both halves."""
    whole, (first, second) = tstat(series), halves(series)
    ok = all(x[1] == x[1] and x[1] >= 2 and x[0] > 0 for x in (whole, first, second))
    return {"mean": whole[0], "t": whole[1], "n": whole[2], "t1": first[1], "t2": second[1], "ok": bool(ok)}


def panel_frames():
    panel = research.load_panel()
    data = research.frames({coin: frame for coin, frame in panel.items() if ":" not in coin})   # main-DEX perps only
    tradable = research.universe(data["close"], data["dollar_volume"])
    return data, tradable


def short_returns(close: pd.DataFrame, hold: int) -> pd.DataFrame:
    """Forward return of shorting the coin for ``hold`` days against BTC (a gain when the coin lags BTC), less the round trip."""
    forward = close.shift(-hold) / close - 1
    btc = forward["BTC"]
    return -(forward.sub(btc, axis=0)) - COST


def window_test(data, tradable) -> list[dict]:
    close = data["close"]
    relative = close.div(close["BTC"], axis=0)
    rows = []
    for hold in HOLDS:
        ret = short_returns(close, hold)
        for window in WINDOWS:
            weak = relative.pct_change(window, fill_method=None)
            rows.append(_run(f"weak {window}d", weak, tradable, ret, hold, ascending=True))
        weak30, bounce3 = relative.pct_change(30, fill_method=None), relative.pct_change(3, fill_method=None)
        rows.append(_run("weak 30d, bouncing 3d", weak30, tradable, ret, hold, ascending=True, gate=bounce3 > 0))
        rows.append(_run("weak 30d, bouncing 3d > 5%", weak30, tradable, ret, hold, ascending=True, gate=bounce3 > 0.05))
    return rows


def fade_test(data, tradable) -> list[dict]:
    close = data["close"]
    relative = close.div(close["BTC"], axis=0)
    stretch = close / close.rolling(30, min_periods=20).mean() - 1
    up3 = relative.pct_change(3, fill_method=None)
    rows = []
    for hold in HOLDS:
        ret = short_returns(close, hold)
        for threshold in (0.05, 0.10, 0.15, 0.20):
            rows.append(_run(f"fade: up >{threshold:.0%} vs BTC in 3d, stretched", stretch, tradable, ret, hold, ascending=False,
                             gate=(up3 > threshold) & (stretch > 0)))
    return rows


def _run(name, signal, tradable, ret, hold, ascending, gate=None) -> dict:
    """One entry every ``hold`` days (non-overlapping): the ``TOP`` weakest (or most stretched) of the tradable coins, equal weight."""
    dates = signal.index[::hold]
    values = []
    for day in dates:
        if day not in ret.index:
            continue
        eligible = tradable.loc[day] & signal.loc[day].notna() & ret.loc[day].notna()
        if gate is not None:
            eligible &= gate.loc[day].fillna(False)
        names = list(signal.loc[day][eligible[eligible].index].sort_values(ascending=ascending).head(TOP).index)
        if len(names) >= 2:
            values.append((day, float(ret.loc[day, names].mean())))
    series = pd.Series({d: v for d, v in values})
    return {"signal": name, "hold": hold, **verdict(series)} if len(series) >= 8 else {"signal": name, "hold": hold, "mean": float("nan"),
                                                                                       "t": float("nan"), "n": len(series), "t1": float("nan"), "t2": float("nan"), "ok": False}


def timing_test() -> list[dict]:
    """The desk's actual short entries against placebo entries on other days of the same coin."""
    trades = pd.read_csv(ROOT / "data" / "closed_trades_crypto.csv", parse_dates=["opened"])
    trades = trades[trades["side"] == "SHORT"].copy()
    panel = research.load_panel()
    entries = trades.assign(day=trades["opened"].dt.tz_convert("UTC").dt.normalize()).drop_duplicates(["coin", "day"])
    out = []
    for hold in (1, 3, 7):
        diffs = []
        for _, trade in entries.iterrows():
            frame = panel.get(trade["coin"])
            if frame is None or trade["day"] not in frame.index:
                continue
            close = frame["close"]
            forward = -(close.shift(-hold) / close - 1)        # a short's gain
            actual = forward.get(trade["day"])
            offsets = [-7, -3, 3, 7]
            placebo = [forward.get(trade["day"] + pd.Timedelta(days=o)) for o in offsets]
            placebo = [p for p in placebo if p is not None and p == p]
            if actual is not None and actual == actual and len(placebo) >= 2:
                diffs.append((trade["day"], actual - float(np.mean(placebo))))
        series = pd.Series({d: v for d, v in sorted(diffs)})
        out.append({"hold": hold, **(verdict(series) if len(series) >= 8 else {"mean": float("nan"), "t": float("nan"), "n": len(series),
                                                                                  "t1": float("nan"), "t2": float("nan"), "ok": False})})
    return out


NOTES = """## Reading

- **Windows:** no window of relative weakness (1, 3, 7, 14, 30 or 60 days) validates as a short signal on the liquid forty, net of costs: the best
  is the 3-day window held 14 days (t about 1.8), and the desk's 30-day rule is near zero (t 0.2 to 0.3). "Weak over 30 days and bouncing over 3"
  is not a better entry, it is worse (negative, t down to -1.7). So the screener's ranking is labelled as the desk's record, not a validated signal.
- **Fade:** stretched and up against BTC in 3 days, shorted for 1 to 14 days, does not validate at any threshold (5 to 20%), the same finding
  as the earlier pump-and-dump tests.
- **Timing:** the desk's own short entries beat placebo days of the same coin (3 and 7 days out, t 3.8 and 4.4, both halves above 2; at 1 day the
  first half is 1.6, not validated). It is a real finding about WHEN the desk enters, not WHAT: same coin, neighbouring days. Caveats: 27 distinct coin
  days (entries merged per coin and day, only coins in the panel), closed trades only, and the size (+12% to +16% of price) is far above what a
  rule would give, so read it as direction, not magnitude. It is not reproducible from simple price conditions here (weak plus bounce fails), so the
  screener shows no timing marker: a timing rule is not established.
- **The attribution's Timing row:** it is built from the deviation of the day's net weight from the window's average, times the benchmark return, plus
  fills marked to the close. That is partly net exposure against the benchmark, so it is not a clean measure of entry timing. The placebo test above
  holds the coin and the exposure fixed and moves only the entry day, so the two agree in direction (timing is a strength) but the placebo test is the
  cleaner evidence for it.
"""


def pct(x) -> str:
    return "" if x != x else f"{100 * x:+.2f}%"


def table(rows: list[dict], first: str) -> str:
    lines = [f"| {first} | hold | n | mean per trade | t | t 1st half | t 2nd half | validated |", "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row.get('signal', 'entry day vs placebo')} | {row['hold']}d | {row['n']} | {pct(row['mean'])} | "
                     f"{'' if row['t'] != row['t'] else format(row['t'], '.1f')} | {'' if row['t1'] != row['t1'] else format(row['t1'], '.1f')} | "
                     f"{'' if row['t2'] != row['t2'] else format(row['t2'], '.1f')} | {'yes' if row['ok'] else 'no'} |")
    return "\n".join(lines)


def main() -> int:
    data, tradable = panel_frames()
    windows, fades, timing = window_test(data, tradable), fade_test(data, tradable), timing_test()
    summary = {
        "windows": windows, "fade": fades, "timing": timing,
        "validated_windows": [r for r in windows if r["ok"]], "validated_fade": [r for r in fades if r["ok"]], "timing_ok": [r for r in timing if r["ok"]],
        "days": int(len(data["close"])), "from": str(data["close"].index[0].date()), "to": str(data["close"].index[-1].date()),
    }
    OUT_JSON.write_text(json.dumps(summary, indent=1, default=float))
    OUT_MD.write_text("\n".join([
        "# Screener strategies: windows, fade, timing", "",
        f"Panel: the 40 most liquid main-DEX perps, {summary['from']} to {summary['to']} ({summary['days']} days). Each entry shorts the "
        f"{TOP} weakest (or most stretched) names for the hold, return vs BTC, less a 9 bps round trip. Non-overlapping entries; "
        "validated means mean > 0 with t >= 2 overall and in both halves.", "",
        "## (a) Relative weakness windows", table(windows, "signal"), "",
        "## (b) Fade: stretched and up vs BTC", table(fades, "signal"), "",
        "## (c) Timing of the desk's own short entries (entry day minus placebo days of the same coin, a short's gain)", table(timing, "entry"), "",
        NOTES]))
    for name in ("validated_windows", "validated_fade", "timing_ok"):
        print(name, [(r.get("signal"), r["hold"], round(r["t"], 1)) for r in summary[name]])
    print("n windows tested", len(windows), "| best window t:", max(((r["t"], r["signal"], r["hold"]) for r in windows if r["t"] == r["t"]), default=None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
