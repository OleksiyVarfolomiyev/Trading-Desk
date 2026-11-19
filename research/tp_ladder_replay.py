"""Does the take-profit ladder beat the single held take-profit? A replay on ALL the desk's closed HL crypto trades.

Every trade (winners and losers: no selection on outcome) is replayed on daily candles over the full days it was held
(the entry day and the exit day are left out, since a daily high or low there may come before the entry or after the exit):

* single: one take-profit at the desk's single target, 3 x ATR (HL_STOP_ATR_MULTIPLE x REWARD_RISK_RATIO): if a full day's
  high (long) or low (short) reached it, the trade exits there; otherwise it exits as it actually did;
* ladder: equal thirds at the ladder's levels in ATR units; each third exits at its level if reached, else as the trade did.

ATR is the coin's 14-day Wilder ATR as a share of price on the day before entry. Returns are per $ of notional, signed by
side, net of fees (a take-profit fills as maker, the trade's own exit is charged as taker). The ladder levels are fitted
out of sample: on the first half of the trades (by entry time) as desk/tp_ladder.fit does (50th/75th/90th percentile of
that half's winners' move in ATR units) and evaluated on the second half, and the other way round. The desk's rule
applies: the ladder is better only if ladder - single is positive with t >= 2 in both evaluations.

    /Users/admin/anaconda3/envs/py314/bin/python research/tp_ladder_replay.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from desk.risk import HL_STOP_ATR_MULTIPLE, REWARD_RISK_RATIO  # noqa: E402

MAKER_FEE, TAKER_FEE = 0.00015, 0.00045
QUANTILES = (0.5, 0.75, 0.9)
SINGLE_ATR = HL_STOP_ATR_MULTIPLE * REWARD_RISK_RATIO


def atr_share(panel: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    for coin, group in panel.groupby("coin"):
        group = group.sort_values("date").set_index("date")
        previous = group["close"].shift()
        true_range = pd.concat([group["high"] - group["low"], (group["high"] - previous).abs(),
                                (group["low"] - previous).abs()], axis=1).max(axis=1)
        group = group.assign(atr=(true_range.ewm(alpha=1 / 14, adjust=False).mean() / group["close"]))
        out[coin] = group
    return out


def prepare(trades: pd.DataFrame, panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per trade: direction, actual move, ATR at entry and the best favourable excursion over its full days."""
    rows = []
    for trade in trades.itertuples():
        frame = panels.get(trade.coin)
        if frame is None:
            continue
        opened = pd.Timestamp(trade.opened).tz_convert(None).normalize()
        closed = pd.Timestamp(trade.time).tz_convert(None).normalize()
        before = frame.loc[:opened - pd.Timedelta(days=1), "atr"].dropna()
        if not len(before) or not before.iloc[-1] > 0:
            continue
        direction = 1 if trade.side == "LONG" else -1
        held = frame.loc[opened + pd.Timedelta(days=1):closed - pd.Timedelta(days=1)]
        best = ((held["high"].max() / trade.entry - 1) if direction > 0 else (1 - held["low"].min() / trade.entry)) if len(held) else 0.0
        rows.append({"opened": opened, "coin": trade.coin, "move": direction * (trade.exit / trade.entry - 1),
                     "atr": float(before.iloc[-1]), "best": max(0.0, float(best) if best == best else 0.0)})
    return pd.DataFrame(rows).sort_values("opened").reset_index(drop=True)


def exit_return(row, level_atr: float) -> float:
    target = level_atr * row.atr
    return target - MAKER_FEE if row.best >= target else row.move - TAKER_FEE


def evaluate(part: pd.DataFrame, levels: list[float]) -> pd.Series:
    """Per trade: ladder return minus single return."""
    single = part.apply(lambda row: exit_return(row, SINGLE_ATR), axis=1)
    ladder = part.apply(lambda row: sum(exit_return(row, level) for level in levels) / len(levels), axis=1)
    return ladder - single


def fit_levels(part: pd.DataFrame) -> list[float]:
    winners = (part["move"] / part["atr"])[part["move"] > 0]
    return [round(float(np.quantile(winners, q)), 2) for q in QUANTILES]


def t_stat(values: pd.Series) -> tuple[float, float, int]:
    values = values.dropna()
    n = len(values)
    sd = values.std(ddof=1) if n > 1 else float("nan")
    return float(values.mean()), (float(values.mean() / (sd / math.sqrt(n))) if sd and sd == sd else float("nan")), n


def run(trades_path: Path | None = None, panel_path: Path | None = None) -> dict:
    if panel_path is None:
        from desk.cache import cache_dir

        panel_path = cache_dir("api_cache") / "signal_panel.csv.gz"
    trades = pd.read_csv(trades_path or REPO / "data" / "closed_trades_crypto.csv", parse_dates=["opened", "time"])
    trades = trades[(trades["venue"] == "HL") & ~trades["coin"].astype(str).str.contains(":")]
    data = prepare(trades, atr_share(pd.read_csv(panel_path, parse_dates=["date"])))
    half = len(data) // 2
    first, second = data.iloc[:half], data.iloc[half:]
    out = {"trades": len(data), "single_atr": SINGLE_ATR, "evaluations": []}
    for fit_on, test_on, label in ((first, second, "fit 1st half, test 2nd"), (second, first, "fit 2nd half, test 1st")):
        levels = fit_levels(fit_on)
        diff = evaluate(test_on, levels)
        mean, t, n = t_stat(diff)
        reached = float((test_on["best"] >= SINGLE_ATR * test_on["atr"]).mean())
        out["evaluations"].append({"label": label, "levels_atr": levels, "n": n, "mean_diff": mean, "t": t,
                                   "single_target_reached": reached})
    out["ladder_better"] = all(e["mean_diff"] > 0 and e["t"] >= 2 for e in out["evaluations"])
    return out


def main() -> int:
    result = run()
    print(f"{result['trades']} HL crypto trades (winners and losers); single target {result['single_atr']:g} x ATR")
    for e in result["evaluations"]:
        print(f"  {e['label']}: ladder at {e['levels_atr']} x ATR, ladder - single {100 * e['mean_diff']:+.3f}% per trade "
              f"(t {e['t']:.2f}, n {e['n']}); single target reached in {e['single_target_reached']:.0%} of test trades")
    print("verdict: " + ("ladder better (t >= 2 in both)" if result["ladder_better"] else "not established: keep the single target"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
