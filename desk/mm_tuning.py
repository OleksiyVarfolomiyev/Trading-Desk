"""Adaptive market making: what the ladder's own fills and the market say, turned into small bounded adjustments.

Every few hours the daemon re-reads each running c248 ladder's fills since it started and the market around it, and for
each coin works out:

* **adverse selection:** the markout of each fill 1, 5 and 60 minutes later (bps, + when the price moved the fill's way);
* **edge:** round trips' PnL net of fees per $1,000 traded, per fill;
* **inventory:** the ladder's net holding against its band;
* **market:** the daily ATR (the spacing is already a share of it), the book's spread, and funding.

Changes follow the desk's hypothesis rule: nothing moves on fewer than ``MIN_FILLS`` fills ("too few fills"), and a
setting moves only when its statistic clears ``T_STAT`` (Newey-West-free: fills are treated as independent, which
overstates t; the bounds and the small steps keep that safe). One step per evaluation, inside fixed bounds:

* spacing (``spacing_atr``): wider by ``STEP`` when the 5-minute markout is significantly negative (fills are picked off),
  or when the book's spread is wider than the spacing; tighter when markouts and edge are both significantly positive;
* size per level (``add_level_pct``/``trim_level_pct``): smaller when the edge is significantly negative, larger when it
  is significantly positive;
* lean (``add_first``): further from the mid when adds are picked off more than trims.

Anything that would leave the bounds, and turning a coin on or off, is a notice for the user, never applied. Every
change is logged with the statistics behind it (``~/.trading-desk/mm_tuning.jsonl``) and written as that coin's own entry
in ``ladder.json`` (the daemon's ``ladder_config`` reads it), so it is reviewable and reversible.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

MIN_FILLS = 30
T_STAT = 2.0
STEP = 1.25
BOUNDS = {"spacing_atr": (0.05, 0.30), "add_level_pct": (0.02, 0.08), "trim_level_pct": (0.015, 0.06), "add_first": (0.25, 1.5)}
MARKOUT_MINUTES = (1, 5, 60)
RUN_DIR = Path.home() / ".trading-desk"
LOG_FILE = RUN_DIR / "mm_tuning.jsonl"
STATE_FILE = RUN_DIR / "mm_tuning.json"


def _t(values: list[float]) -> tuple[float, float, int]:
    """(mean, t, n) of a sample, treating its values as independent."""
    n = len(values)
    if n < 2:
        return (values[0] if values else float("nan")), float("nan"), n
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, (mean / math.sqrt(var / n) if var > 0 else float("nan")), n


def fill_markouts(fills: list[dict], times: list[int], closes: list[float], minute: int) -> list[float]:
    """Per-fill markout in bps ``minute`` minutes later (+ when the price moved the fill's way); fills without a later
    candle are left out."""
    import bisect

    out = []
    if not times:
        return out
    for fill in fills:
        target = int(fill["time"]) + minute * 60_000
        if target < times[0] or target > times[-1] + 60_000:
            continue
        later = closes[bisect.bisect_right(times, target) - 1]
        side = 1.0 if fill["side"] == "B" else -1.0
        out.append(side * (later / float(fill["px"]) - 1) * 1e4)
    return out


def edge_per_fill(fills: list[dict]) -> list[float]:
    """Each closed round trip's PnL net of both fills' fees per $1,000 of its notional (first in, first out)."""
    ordered = sorted(fills, key=lambda fill: fill["time"])
    lots: list[list[float]] = []   # [signed size, price, fee per unit]
    out = []
    for fill in ordered:
        price, size, fee = float(fill["px"]), float(fill["sz"]), float(fill.get("fee") or 0)
        unit_fee = fee / size if size else 0.0
        left = size if fill["side"] == "B" else -size
        while left and lots and (lots[0][0] > 0) != (left > 0):
            lot = lots[0]
            part = min(abs(lot[0]), abs(left))
            gross = part * (price - lot[1]) if lot[0] > 0 else part * (lot[1] - price)
            net = gross - part * (lot[2] + unit_fee)
            out.append(net / (part * (price + lot[1])) * 1000)
            lot[0] += -part if lot[0] > 0 else part
            left += -part if left > 0 else part
            if abs(lot[0]) < 1e-12:
                lots.pop(0)
        if abs(left) > 1e-12:
            lots.append([left, price, unit_fee])
    return out


def evaluate(coin: str, fills: list[dict], times: list[int], closes: list[float], config: dict, spread_bps: float,
             atr_bps: float, funding_apr: float, base_side: float) -> dict:
    """The statistics and the bounded changes for one ladder. ``base_side`` is +1 under a long, -1 under a short."""
    stats = {"fills": len(fills)}
    for minute in MARKOUT_MINUTES:
        mean, t, n = _t(fill_markouts(fills, times, closes, minute))
        stats[f"markout_{minute}m"] = {"mean_bps": mean, "t": t, "n": n}
    edge, edge_t, edge_n = _t(edge_per_fill(fills))
    stats["edge_per_1k"] = {"mean": edge, "t": edge_t, "n": edge_n}
    adds = [f for f in fills if (f["side"] == "B") == (base_side > 0)]
    trims = [f for f in fills if (f["side"] == "B") != (base_side > 0)]
    stats["markout_5m_adds"] = _t(fill_markouts(adds, times, closes, 5))[0]
    stats["markout_5m_trims"] = _t(fill_markouts(trims, times, closes, 5))[0]
    stats.update(spread_bps=spread_bps, atr_bps=atr_bps, funding_apr=funding_apr)
    changes, notices, reasons = {}, [], []
    if len(fills) < MIN_FILLS:
        return {"coin": coin, "stats": stats, "changes": {}, "notices": [], "status": f"too few fills ({len(fills)} of {MIN_FILLS})"}

    def move(key: str, factor: float, why: str) -> None:
        low, high = BOUNDS[key]
        now = float(config[key])
        new = round(now * factor, 4)
        if new < low or new > high:
            notices.append(f"{coin}: {key} would go from {now:g} to {new:g}, outside {low:g}-{high:g} ({why}); left as is")
            return
        changes[key] = new
        reasons.append(f"{key} {now:g} -> {new:g}: {why}")

    m5 = stats["markout_5m"]
    spacing_bps = float(config["spacing_atr"]) * atr_bps
    if m5["n"] >= MIN_FILLS and m5["t"] <= -T_STAT:
        move("spacing_atr", STEP, f"fills picked off: 5m markout {m5['mean_bps']:.1f} bps, t {m5['t']:.1f}")
    elif spread_bps > spacing_bps and spacing_bps > 0:
        move("spacing_atr", STEP, f"book spread {spread_bps:.0f} bps is wider than the spacing {spacing_bps:.0f} bps")
    elif m5["t"] >= T_STAT and edge_t >= T_STAT:
        move("spacing_atr", 1 / STEP, f"fills earn: 5m markout {m5['mean_bps']:.1f} bps (t {m5['t']:.1f}), edge t {edge_t:.1f}")
    if edge_n >= MIN_FILLS // 2 and edge_t <= -T_STAT:
        for key in ("add_level_pct", "trim_level_pct"):
            move(key, 1 / STEP, f"round trips lose: {edge:.2f} per $1k, t {edge_t:.1f}")
    elif edge_n >= MIN_FILLS // 2 and edge_t >= T_STAT:
        for key in ("add_level_pct", "trim_level_pct"):
            move(key, STEP, f"round trips earn: {edge:.2f} per $1k, t {edge_t:.1f}")
    adds_m, trims_m = stats["markout_5m_adds"], stats["markout_5m_trims"]
    if len(adds) >= MIN_FILLS // 2 and adds_m == adds_m and trims_m == trims_m and adds_m < trims_m - 5:
        move("add_first", STEP, f"adds picked off more than trims (5m {adds_m:.1f} vs {trims_m:.1f} bps)")
    if edge_n >= 100 and edge_t <= -T_STAT:
        notices.append(f"{coin}: round trips lost {edge:.2f} per $1k over {edge_n} round trips (t {edge_t:.1f}); "
                       "consider turning the ladder off")
    if abs(funding_apr) >= 0.5 and funding_apr * base_side > 0:
        notices.append(f"{coin}: funding {funding_apr:.0%} a year is paid by the side the ladder leans with")
    status = "adjusted" if changes else "no change: nothing significant"
    return {"coin": coin, "stats": stats, "changes": changes, "notices": notices, "reasons": reasons, "status": status}


def apply(results: list[dict], config_path: Path, log_path: Path = LOG_FILE, state_path: Path = STATE_FILE) -> None:
    """Write each coin's changes as its own entry in the ladder config and log every evaluation with its statistics."""
    try:
        saved = json.loads(config_path.read_text())
    except (OSError, ValueError):
        saved = {}
    coins = saved.setdefault("coins", {})
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for result in results:
        if result["changes"]:
            coins.setdefault(result["coin"].split(":")[-1].upper(), {}).update(result["changes"])
    config_path.write_text(json.dumps(saved, indent=1))
    with log_path.open("a") as handle:
        for result in results:
            handle.write(json.dumps({"at": stamp, **result}, default=float) + "\n")
    state_path.write_text(json.dumps({"at": stamp, "results": results}, indent=1, default=float))


def last_state(path: Path = STATE_FILE) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
