"""Does a market-making ladder earn its spread? Per coin, from the ladder's own fills on HL (cloid-tagged).

Two measures, each tested at t >= 2 in both halves (the desk's hypothesis standard):
* round trips: FIFO-paired buys and sells, each trip's gain in basis points of its notional, less both fees;
* markout: the move 1 and 15 minutes after each fill, signed for the fill's side, less the fill's fee (HL keeps
  1-minute candles for only the last few days, so the markout covers the recent fills).

Run: PYTHONPATH=. python research/mm_fill_test.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from desk.accounts import accounts_for, load_accounts
from desk.market_making import ladder_fill_rows, minute_closes

COINS = ("MELANIA", "LIT", "ETH", "BTC", "VVV", "xyz:SP500")
SINCE_MS = 1_794_200_000_000   # Nov 10 2026, before the first ladder


def trips(fills: list[dict]) -> list[dict]:
    """FIFO round trips: time of the closing fill, gain in bp of notional after both fees."""
    lots, out = [], []
    for fill in sorted(fills, key=lambda f: f["time"]):
        price, size, fee = float(fill["px"]), float(fill["sz"]), float(fill.get("fee") or 0)
        left = size if fill["side"] == "B" else -size
        unit_fee = fee / size if size else 0.0
        while left and lots and (lots[0][0] > 0) != (left > 0):
            lot = lots[0]
            part = min(abs(lot[0]), abs(left))
            gain = part * (price - lot[1]) if lot[0] > 0 else part * (lot[1] - price)
            cost = part * (unit_fee + lot[2])
            out.append({"time": fill["time"], "bp": (gain - cost) / (part * price) * 1e4})
            lot[0] += -part if lot[0] > 0 else part
            left += -part if left > 0 else part
            if abs(lot[0]) < 1e-12:
                lots.pop(0)
        if abs(left) > 1e-12:
            lots.append([left, price, unit_fee])
    return out


def t_stat(values) -> tuple[float, float, int]:
    x = np.asarray(values, float)
    x = x[np.isfinite(x)]
    if len(x) < 5 or x.std(ddof=1) == 0:
        return float("nan"), float("nan"), len(x)
    return x.mean(), x.mean() / (x.std(ddof=1) / np.sqrt(len(x))), len(x)


def halves(rows: pd.DataFrame, column: str) -> dict:
    rows = rows.sort_values("time")
    middle = len(rows) // 2
    return {name: t_stat(part[column]) for name, part in (("all", rows), ("first", rows.iloc[:middle]),
                                                           ("second", rows.iloc[middle:]))}


def main() -> None:
    wallets = [account.id for account in accounts_for("hl", load_accounts())]
    result = []
    for coin in COINS:
        fills = [dict(f, wallet=w) for w in wallets for f in ladder_fill_rows(w, SINCE_MS)
                 if f["coin"] == coin]
        if len(fills) < 10:
            continue
        try:
            closes = minute_closes(coin, min(f["time"] for f in fills))
        except Exception as exc:   # HL has no 1-minute candles for some markets: the round trips still run
            print(f"{coin}: no minute candles ({type(exc).__name__}); markouts skipped")
            closes = pd.Series(dtype=float)
        times = closes.index.to_numpy() if len(closes) else np.array([])
        marks = []
        for f in fills:
            side = 1.0 if f["side"] == "B" else -1.0
            fee_bp = float(f.get("fee") or 0) / (float(f["px"]) * float(f["sz"])) * 1e4
            row = {"time": f["time"]}
            for minutes in (1, 15):
                target = f["time"] + minutes * 60_000
                if len(times) and times[0] <= target <= times[-1] + 60_000:
                    later = closes.to_numpy()[times.searchsorted(target, side="right") - 1]
                    row[f"m{minutes}"] = side * (later / float(f["px"]) - 1) * 1e4 - fee_bp
                else:
                    row[f"m{minutes}"] = np.nan
            marks.append(row)
        marks = pd.DataFrame(marks)
        round_trips = pd.DataFrame(trips(fills))
        for label, frame, column in (("round trips", round_trips, "bp"), ("markout 1m", marks, "m1"),
                                     ("markout 15m", marks, "m15")):
            if frame.empty or frame[column].notna().sum() < 10:
                continue
            h = halves(frame.dropna(subset=[column]), column)
            ok = all(h[k][1] >= 2 for k in ("first", "second") if np.isfinite(h[k][1]))
            result.append({"coin": coin, "measure": label, "n": h["all"][2], "mean bp": h["all"][0],
                           "t": h["all"][1], "first half t": h["first"][1], "second half t": h["second"][1],
                           "established": "yes" if ok and h["all"][1] >= 2 else "no"})
    pd.set_option("display.width", 200)
    print(pd.DataFrame(result).to_string(index=False, float_format=lambda v: f"{v:.2f}"))


if __name__ == "__main__":
    main()
