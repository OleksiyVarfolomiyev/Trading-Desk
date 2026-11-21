"""Hyperliquid meme-spike replay and paper execution against the public order book.

Fetch and replay use closed 15-minute candles. Paper-watch keeps an isolated
budget and refreshes the candidate rule weekly. Every execution mode is simulated.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import statistics
import time
from pathlib import Path

import pandas as pd

from desk.hl.trade_analytics import post_info

MEMES = ("kPEPE", "kBONK", "DOGE", "WIF", "PENGU", "FARTCOIN", "PUMP", "TRUMP")
HOUR_MS = 3600_000
BAR_MS = HOUR_MS // 4
NOTICE_LOSS = 0.10
DEFAULT_RULE = (0.12, 3.0, 4.0)
FEE = 0.00045
SLIPPAGE_PAD = 0.0008
MAX_BOOK_SLIPPAGE = 0.005
TICKET = 25
LOOKBACK_DAYS = 45
COOLDOWN = 96
HORIZONS = (16, 48)


def candles(coin: str, days: int) -> pd.DataFrame:
    end = int(time.time() * 1000)
    rows = post_info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "15m",
                     "startTime": end - days * 86_400_000, "endTime": end}}, timeout=12)
    parsed = [(coin, pd.to_datetime(r["t"], unit="ms", utc=True), float(r["o"]), float(r["c"]),
               float(r["v"]) * float(r["c"])) for r in rows if r["T"] < end]
    return pd.DataFrame(parsed, columns=["coin", "time", "open", "close", "volume"])


def features(frame: pd.DataFrame, gain_threshold: float = DEFAULT_RULE[0], volume_threshold: float = DEFAULT_RULE[1]) -> pd.DataFrame:
    part = frame.set_index("time").sort_index().asfreq("15min")
    close, volume = part["close"], part["volume"]
    gain = close.pct_change(4, fill_method=None)
    hour = volume.rolling(4).sum()
    typical = hour.rolling(7 * 96, min_periods=3 * 96).median().shift(4)
    four_hour = volume.rolling(16).sum()
    typical_four = four_hour.rolling(7 * 96, min_periods=3 * 96).median().shift(16)
    early = ((gain >= 0.04) & (gain - gain.shift(4) >= 0.03) & (hour >= 2 * typical)
             & (hour >= 1.5 * hour.shift(4)) & (typical > 0))
    spike = (close.pct_change(16, fill_method=None) >= gain_threshold) & (typical_four > 0) & (four_hour >= volume_threshold * typical_four)
    return part.assign(early=early, spike=spike)


def replay(frame: pd.DataFrame, gain: float = DEFAULT_RULE[0], volume: float = DEFAULT_RULE[1]) -> pd.DataFrame:
    events = []
    for coin, group in frame.groupby("coin"):
        part = features(group, gain, volume)
        for rule in ("early", "spike"):
            last = -COOLDOWN
            for i, matched in enumerate(part[rule]):
                start, stop = i + 1, i + 1 + max(HORIZONS)
                if not matched or i - last < COOLDOWN or stop >= len(part):
                    continue
                prices = part["open"].iloc[start:stop + 1]
                if prices.isna().any() or (prices <= 0).any():
                    continue
                last = i
                later_spike = part["spike"].iloc[i:min(i + 13, len(part))].any()
                for horizon in HORIZONS:
                    move = prices.iloc[horizon] / prices.iloc[0] - 1
                    events.append({"coin": coin, "at": part.index[start], "rule": rule, "hours": horizon / 4,
                                   "move": move, "spike_within_3h": bool(later_spike)})
    return pd.DataFrame(events, columns=["coin", "at", "rule", "hours", "move", "spike_within_3h"])


def print_replay(frame: pd.DataFrame, results: pd.DataFrame) -> None:
    split = (frame.time.min() + (frame.time.max() - frame.time.min()) / 2).ceil("D")
    print(f"15m cache: {frame.coin.nunique()} coins, {frame.time.min():%F} to {frame.time.max():%F}; split {split:%F}")
    early = results[(results.rule == "early") & (results.hours == 4)]
    if len(early):
        print(f"Early signals meeting the 4h spike rule within 3h: {early.spike_within_3h.mean():.1%} of {len(early)}")
    for (rule, horizon), group in results.groupby(["rule", "hours"]):
        day = group["at"].dt.normalize()
        for side in (1, -1):
            net = side * group.move - 2 * (FEE + SLIPPAGE_PAD)
            means = net.groupby(day).mean()
            print(f"{rule} {horizon:g}h {'long' if side > 0 else 'short'}: n={len(group)} across {len(means)} days, "
                  f"mean {net.mean():+.2%}, day means {means[means.index < split].mean():+.2%} / {means[means.index >= split].mean():+.2%}")


def paper_fill(book: dict, *, buy: bool, quantity: float) -> float:
    """Simulated immediate fill through visible levels, with a 0.5% limit from mid and no partial position."""
    bids, asks = book.get("levels") or ([], [])
    if not bids or not asks or not quantity > 0:
        raise ValueError("No usable depth or quantity")
    observed = int(time.time() * 1000)
    if not 0 <= observed - int(book["time"]) <= 5000:
        raise ValueError("Public book snapshot is stale")
    mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
    remaining, value = quantity, 0.0
    for level in asks if buy else bids:
        price, size = float(level["px"]), float(level["sz"])
        if (price / mid - 1) * (1 if buy else -1) > MAX_BOOK_SLIPPAGE:
            break
        used = min(remaining, size)
        value += used * price
        remaining -= used
        if remaining <= quantity * 1e-9:
            return value / quantity
    raise ValueError("Insufficient size inside the paper limit")


def paper_round_trip(coin: str) -> None:
    # Unsigned public reads only. Smoke test closes immediately and is not a strategy outcome.
    entry_book = post_info({"type": "l2Book", "coin": coin}, timeout=8)
    mid = sum(float(side[0]["px"]) for side in entry_book["levels"]) / 2
    quantity = TICKET / mid
    entry = paper_fill(entry_book, buy=False, quantity=quantity)
    exit_book = post_info({"type": "l2Book", "coin": coin}, timeout=8)
    close = paper_fill(exit_book, buy=True, quantity=quantity)
    net = quantity * (entry - close - FEE * (entry + close))
    print(json.dumps({"mode": "paper smoke", "coin": coin, "short_usd": quantity * entry,
                      "entry_price": entry, "cover_price": close, "net_usd": net, "open_quantity": 0}))


def recent_signals(gain: float = DEFAULT_RULE[0], volume: float = DEFAULT_RULE[1]) -> list[dict]:
    signals = []
    for coin in MEMES:
        bars = candles(coin, 8)
        if bars.empty:
            continue
        part = features(bars, gain, volume)
        signal = part.iloc[-1]
        if pd.Timestamp.now(tz="UTC") - part.index[-1] < pd.Timedelta(minutes=30) and (signal.spike or signal.early):
            signals.append({"coin": coin, "bar": int(part.index[-1].timestamp() * 1000),
                            "spike": bool(signal.spike), "early": bool(signal.early)})
    return signals


def net_cover(position: dict, cover: float) -> float:
    entry = position["entry"]
    return position["quantity"] * (entry - cover - FEE * (entry + cover))


def evaluate_calibration(frame: pd.DataFrame, cost: float) -> dict:
    """Select one rule on older data, then check it once on the most recent 14 days."""
    cutoff = frame.time.max().normalize() - pd.Timedelta(days=13)
    best = None
    for gain in (0.08, 0.10, 0.12):
        for volume in (2.5, 3.0):
            rows = replay(frame, gain, volume)
            for hours in (4.0, 12.0):
                samples = rows[(rows.rule == "spike") & (rows.hours == hours)].copy()
                samples["net"] = -samples.move - cost
                train = samples[samples["at"] + pd.Timedelta(hours=hours) < cutoff]
                daily = train.groupby(train["at"].dt.normalize()).net.mean()
                if len(train) < 20 or len(daily) < 12:
                    continue
                score = float(daily.mean())
                if best is None or score > best["train_daily"]:
                    best = {"rule": [gain, volume, hours], "train_n": len(train), "train_daily": score,
                            "holdout": samples[samples["at"] >= cutoff]}
    if best is None:
        return {"status": "insufficient training evidence", "holdout_from": cutoff.isoformat()}
    holdout = best.pop("holdout")
    daily = holdout.groupby(holdout["at"].dt.normalize()).net.mean()
    best.update({"holdout_from": cutoff.isoformat(), "holdout_n": len(holdout), "holdout_days": len(daily),
                 "holdout_daily": float(daily.mean()) if len(daily) else None})
    # A conservative paper-only gate with independent calendar days and positive returns after costs.
    best["status"] = ("promote in paper" if best["train_daily"] > 0 and len(holdout) >= 15 and len(daily) >= 8
                      and daily.mean() > 0 else "hold current rule")
    return best


def calibrate_paper_state(state: dict) -> None:
    """Weekly rolling refresh with costs floored at the research assumption."""
    now = int(time.time() * 1000)
    if (now - state.get("last_calibration", 0) < 7 * 24 * HOUR_MS
            or now - state.get("last_calibration_attempt", 0) < HOUR_MS):
        return
    state["last_calibration_attempt"] = now
    parts = []
    for coin in MEMES:
        parts.append(candles(coin, LOOKBACK_DAYS))
        time.sleep(5)
    covered = [part for part in parts if not part.empty]
    if len(covered) < len(MEMES):
        return
    costs = state["observed_costs"]
    cost = max(2 * (FEE + SLIPPAGE_PAD), statistics.median(costs) if len(costs) >= 20 else 0)
    result = evaluate_calibration(pd.concat(covered, ignore_index=True), cost)
    state["last_calibration"] = now
    state["calibration"] = {**result, "cost": cost}
    if result["status"] == "promote in paper":
        state["rule"] = result["rule"]


def budget_notice(state: dict, marked_pnl: float) -> dict | None:
    equity = state["budget"] + state["realized"] + marked_pnl
    change = equity / state["budget"] - 1
    if change > -NOTICE_LOSS + 1e-12:
        state["below_notice"] = False
    elif not state["below_notice"]:
        state["below_notice"] = True
        return {"message": "Paper spike fade is down at least 10% of its starting budget",
                "budget": state["budget"], "equity": equity, "return": change, "at": int(time.time() * 1000)}
    return None


def paper_step(path: Path, budget: float, fraction: float, *, learn: bool = False) -> dict:
    """One position at a time, with marked budget PnL and a one-time notice per breach."""
    if not math.isfinite(budget) or budget <= 0 or not 0.10 <= fraction <= 0.20:
        raise ValueError("Set a positive paper budget and an entry fraction from 10% to 20%")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {
            "budget": budget, "fraction": fraction, "position": None, "last_bar": {}, "realized": 0.0,
            "below_notice": False, "observed_costs": [], "rule": list(DEFAULT_RULE),
        }
        if (state.get("budget"), state.get("fraction")) != (budget, fraction):
            raise ValueError("Use the budget and fraction already recorded for this state file")
        if learn and state["position"] is None:
            try:
                calibrate_paper_state(state)
            except (OSError, ValueError, RuntimeError):
                pass   # a failed history read cannot prevent a position check or budget notice
        now = int(time.time() * 1000)
        position = state["position"]
        marked_pnl = 0.0
        if position:
            book = post_info({"type": "l2Book", "coin": position["coin"]}, timeout=8)
            try:
                cover = paper_fill(book, buy=True, quantity=position["quantity"])
            except ValueError as exc:
                if str(exc) != "Insufficient size inside the paper limit":
                    raise
                cover = None
            mark = cover if cover is not None else float(book["levels"][1][0]["px"])
            marked_pnl = net_cover(position, mark)
            result = {"status": "holding" if cover is not None else "insufficient cover depth", "position": position}
            if cover is not None and now >= position["entered"] + position["hold_hours"] * HOUR_MS:
                state["realized"] += marked_pnl
                mid = sum(float(side[0]["px"]) for side in book["levels"]) / 2
                cost = FEE * 2 + max(0, cover / mid - 1) + position["entry_slippage"]
                state["observed_costs"] = (state["observed_costs"] + [cost])[-100:]
                state["position"] = None
                result = {"status": "covered", "coin": position["coin"], "net_usd": marked_pnl}
                marked_pnl = 0
        else:
            gain, volume, hold = state["rule"]
            if state.get("scan_bar") != now // BAR_MS:
                state["signals"] = recent_signals(gain, volume)
                state["scan_bar"] = now // BAR_MS
            signals = state.get("signals", [])
            now = int(time.time() * 1000)
            match = next((s for s in signals if s["spike"] and now - state["last_bar"].get(s["coin"], 0) >= 24 * HOUR_MS
                          and now - s["bar"] < 30 * 60_000), None)
            notional = min(budget, budget + state["realized"]) * fraction
            result = {"status": "no eligible spike", "signals": signals}
            if match is not None and notional > 0:
                coin = match["coin"]
                book = post_info({"type": "l2Book", "coin": coin}, timeout=8)
                bid, ask = (float(side[0]["px"]) for side in book["levels"])
                quantity = notional / bid
                entry = paper_fill(book, buy=False, quantity=quantity)
                mark = paper_fill(book, buy=True, quantity=quantity)
                position = {"coin": coin, "quantity": quantity, "entry": entry, "entered": int(time.time() * 1000),
                            "hold_hours": hold, "entry_slippage": max(0, 1 - entry / ((bid + ask) / 2))}
                marked_pnl = net_cover(position, mark)
                state["position"] = position
                state["last_bar"][coin] = match["bar"]
                result = {"status": "short opened", "position": position}
        notice = budget_notice(state, marked_pnl)
        if notice:
            state["notice"] = notice
            result["notice"] = notice
        state["equity"] = budget + state["realized"] + marked_pnl
        state["updated_at"] = int(time.time() * 1000)
        result.update({"mode": "paper", "budget": budget, "equity": state["equity"],
                       "rule": state["rule"], "calibration": state.get("calibration")})
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(path)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fetch", "replay", "calibrate", "paper-smoke", "paper-step", "paper-watch", "scan"))
    parser.add_argument("--cache", type=Path, default=Path("data/spike_fade_15m.csv.gz"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--budget", type=float)
    parser.add_argument("--fraction", type=float, default=0.10)
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    if args.mode in ("paper-step", "paper-watch") and (args.state is None or args.budget is None):
        parser.error("paper execution requires --state and --budget")
    if args.mode == "fetch":
        parts = []
        for coin in MEMES:
            rows = candles(coin, LOOKBACK_DAYS)
            print(f"{coin}: {len(rows)} bars", flush=True)
            if not rows.empty:
                parts.append(rows)
            time.sleep(1)
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        pd.concat(parts).to_csv(args.cache, index=False)
    elif args.mode == "replay":
        frame = pd.read_csv(args.cache, parse_dates=["time"]).drop_duplicates(["coin", "time"])
        print_replay(frame, replay(frame))
    elif args.mode == "calibrate":
        frame = pd.read_csv(args.cache, parse_dates=["time"]).drop_duplicates(["coin", "time"])
        print(json.dumps(evaluate_calibration(frame, 2 * (FEE + SLIPPAGE_PAD))))
    elif args.mode == "paper-smoke":
        paper_round_trip("DOGE")
    elif args.mode == "paper-step":
        print(json.dumps(paper_step(args.state, args.budget, args.fraction)), flush=True)
    elif args.mode == "paper-watch":
        while True:
            try:
                print(json.dumps(paper_step(args.state, args.budget, args.fraction, learn=True)), flush=True)
            except (OSError, ValueError, RuntimeError) as exc:
                print(json.dumps({"mode": "paper", "error": str(exc)}), flush=True)
            time.sleep(max(args.interval, 60))
    else:
        print(json.dumps({"mode": "read-only scan", "signals": recent_signals()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
