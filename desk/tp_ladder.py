"""Take-profit ladder for crypto entries: where the desk's own winners were actually taken.

A crypto entry scales out with a few held take-profit rungs in the profit direction (no stop, no loss guard, and no rung
ever reduces the position at a price worse than its entry). The rung levels are not a hidden constant: they are fitted
from the desk's closed HL crypto trades, as the 50th, 75th and 90th percentile of the winning trades' realised move in
units of the coin's 14-day ATR at the time of entry, and written with that provenance to ``desk/tp_ladder.json``
(reviewable, refitted by ``python -m desk.tp_ladder fit``). The plan prints the levels and where they came from.

Equal sizes per rung. Without the file (or with too few winners) the ladder is the single take-profit the desk used
before (the reward/risk ratio times the sizing distance), and the plan says so.

    python -m desk.tp_ladder fit      # refit from data/closed_trades_crypto.csv and the cached daily candles
    python -m desk.tp_ladder show     # print the current levels and their basis
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LADDER_FILE = REPO / "desk" / "tp_ladder.json"
CLOSED_TRADES = REPO / "data" / "closed_trades_crypto.csv"
QUANTILES = (0.5, 0.75, 0.9)
MIN_WINNERS = 60   # fewer winners than this and the fit is not trusted: the single take-profit is kept


def load(path: Path = LADDER_FILE) -> dict | None:
    """The fitted ladder ({"levels_atr", "shares", "basis", ...}) or None when there is none to trust."""
    try:
        ladder = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    levels, shares = ladder.get("levels_atr") or [], ladder.get("shares") or []
    if not levels or len(levels) != len(shares) or any(level <= 0 for level in levels) or abs(sum(shares) - 1) > 1e-6:
        return None
    if list(levels) != sorted(levels):
        return None
    return ladder


def rungs(entry: float, direction: int, atr: float, quantity: float, size_decimals: int, tick: float,
          min_value: float, ladder: dict | None, fallback_distance: float) -> list[tuple[float, float]]:
    """[(price, quantity)] for the take-profit rungs: every price strictly on the profit side of the entry, sizes on the
    market's step, each rung at least ``min_value`` dollars (rungs that would be smaller are merged into the next).
    ``fallback_distance`` is the single take-profit's distance when there is no ladder."""
    if quantity <= 0 or entry <= 0:
        return []
    if ladder and atr > 0:
        targets = [entry + direction * level * atr for level in ladder["levels_atr"]]
        shares = list(ladder["shares"])
    else:
        targets, shares = [entry + direction * fallback_distance], [1.0]
    step = 10 ** -size_decimals
    units = int(round(quantity / step))
    out: list[tuple[float, float]] = []
    carried = 0
    for index, (price, share) in enumerate(zip(targets, shares)):
        # Never at or beyond the entry on the loss side: at least one tick into profit.
        price = max(price, entry + tick) if direction > 0 else min(price, entry - tick)
        if price <= 0:
            continue
        take = int(units * share) + carried if index < len(targets) - 1 else units - sum(round(q / step) for _, q in out)
        if take <= 0:
            continue
        size = round(take * step, size_decimals)
        if size * price < min_value and index < len(targets) - 1:
            carried = take   # too small on its own: goes with the next rung
            continue
        carried = 0
        out.append((price, size))
    if out and out[-1][1] * out[-1][0] < min_value and len(out) > 1:   # a small last rung joins the one before
        price, size = out.pop()
        prev_price, prev_size = out.pop()
        out.append((prev_price, round(prev_size + size, size_decimals)))
    return [(price, size) for price, size in out if size > 0]


def describe(ladder: dict | None) -> str:
    if not ladder:
        return "single take-profit at the reward/risk distance (no fitted ladder)"
    levels = ", ".join(f"{level:.2g}" for level in ladder["levels_atr"])
    return (f"take-profit ladder at {levels} x ATR, equal parts ({ladder.get('basis', 'fitted from closed trades')})")


def fit(trades_path: Path = CLOSED_TRADES, panel_path: Path | None = None) -> dict:
    """Fit the rung levels from the desk's winning closed HL crypto trades (see the module doc)."""
    import numpy as np
    import pandas as pd

    if panel_path is None:
        from desk.cache import cache_dir

        panel_path = cache_dir("api_cache") / "signal_panel.csv.gz"
    panel = pd.read_csv(panel_path, parse_dates=["date"])
    trades = pd.read_csv(trades_path, parse_dates=["opened"])
    trades = trades[(trades["venue"] == "HL") & ~trades["coin"].astype(str).str.contains(":")]
    atr_share = {}
    for coin, group in panel.groupby("coin"):
        group = group.sort_values("date").set_index("date")
        previous = group["close"].shift()
        true_range = pd.concat([group["high"] - group["low"], (group["high"] - previous).abs(),
                                (group["low"] - previous).abs()], axis=1).max(axis=1)
        atr_share[coin] = (true_range.ewm(alpha=1 / 14, adjust=False).mean() / group["close"]).dropna()
    moves = []
    for trade in trades.itertuples():
        series = atr_share.get(trade.coin)
        if series is None:
            continue
        day = pd.Timestamp(trade.opened).tz_localize(None).normalize() - pd.Timedelta(days=1)
        before = series.loc[:day]
        if not len(before) or not before.iloc[-1] > 0:
            continue
        move = (trade.exit / trade.entry - 1) * (1 if trade.side == "LONG" else -1)
        if move > 0:
            moves.append(move / before.iloc[-1])
    moves = np.array(moves)
    if len(moves) < MIN_WINNERS:
        raise ValueError(f"only {len(moves)} winning trades with an ATR at entry; need {MIN_WINNERS}")
    half = len(moves) // 2
    levels = [round(float(np.quantile(moves, q)), 2) for q in QUANTILES]
    halves = [[round(float(np.quantile(part, q)), 2) for q in QUANTILES] for part in (moves[:half], moves[half:])]
    return {"levels_atr": levels, "shares": [round(1 / len(levels), 6)] * (len(levels) - 1) + [round(1 - (len(levels) - 1) * round(1 / len(levels), 6), 6)],
            "basis": f"median, 75th and 90th percentile of {len(moves)} winning HL crypto trades' move in ATR units",
            "halves": halves, "winners": int(len(moves)), "quantiles": list(QUANTILES),
            "as_of": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")}


def main(argv: list[str] | None = None) -> int:
    command = (argv if argv is not None else sys.argv[1:] or ["show"])[0]
    if command == "fit":
        ladder = fit()
        LADDER_FILE.write_text(json.dumps(ladder, indent=1) + "\n")
        print(f"{describe(ladder)}; halves {ladder['halves']} -> {LADDER_FILE.relative_to(REPO)}")
    else:
        print(describe(load()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
