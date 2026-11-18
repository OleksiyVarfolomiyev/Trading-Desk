"""Price-only replay of verified Hyperliquid protective stop fills."""

from collections import defaultdict
from math import isfinite

import pandas as pd

from desk.books import hl_kind
from desk.hl.trade_analytics import post_info

HOUR_MS = 3_600_000
HOLD_HOURS = 24
STOP_TYPES = {"Stop Market", "Stop Limit"}
CLOSE_SIDE = {"Close Long": 1, "Close Short": -1}


def stop_fills(fills, orders):
    """Match by exchange order ID and market. A filled reduce-only stop is a verified exit."""
    stops = set()
    for event in orders:
        order = event.get("order") or {}
        if order.get("orderType") in STOP_TYPES and order.get("reduceOnly") is True and order.get("oid") is not None:
            stops.add((order.get("oid"), order.get("coin")))
    for fill in fills:
        key = fill.get("oid"), fill.get("coin")
        side = CLOSE_SIDE.get(fill.get("dir"))
        if key not in stops or not side or not fill.get("time"):
            continue
        price, size = float(fill["px"]), float(fill["sz"])
        if price > 0 and size > 0 and isfinite(price) and isfinite(size):
            yield key, int(fill["time"]), side, price, size


def _marks(targets):
    """Read exact completed hourly closes, batching nearby targets to limit calls."""
    marks = {}
    for coin, wanted in targets.items():
        batches = []
        for close in sorted(wanted):
            if not batches or close - batches[-1][0] > 24 * HOUR_MS:
                batches.append([close])
            else:
                batches[-1].append(close)
        for batch in batches:
            try:
                rows = post_info({"type": "candleSnapshot", "req": {
                    "coin": coin, "interval": "1h", "startTime": batch[0] - HOUR_MS, "endTime": batch[-1],
                }})
                for row in rows or []:
                    close = int(row["t"]) + HOUR_MS
                    price = float(row["c"])
                    if close in wanted and price > 0 and isfinite(price):
                        marks[coin, close] = price
            except Exception:
                continue  # missing marks reduce replay coverage
    return marks


def stop_replay(fills_by_wallet, equity_by_book, windows, now=None):
    """Compare each filled stop with retaining its closed quantity for 24h, at the next hourly close.

    Returns are gross price effects over prior trading equity. It is a diagnostic of
    identifiable stop fills, independent of—and excluded from—benchmark attribution.
    """
    now = now or pd.Timestamp.now(tz="UTC")
    completed = int(now.floor("h").timestamp() * 1000)
    targets = defaultdict(set)
    samples = defaultdict(list)
    wallet_count = len(fills_by_wallet)
    wallets_read = 0
    coverage = []
    for wallet, fills in fills_by_wallet.items():
        try:
            orders = post_info({"type": "historicalOrders", "user": wallet})
            if not isinstance(orders, list):
                continue
        except Exception:
            continue
        wallets_read += 1
        order_times = [
            event.get("statusTimestamp") or (event.get("order") or {}).get("timestamp")
            for event in orders
        ]
        if any(order_times):
            coverage.append(min(stamp for stamp in order_times if stamp))
        for (oid, coin), stamp, side, exit_price, size in stop_fills(fills, orders):
            target = ((stamp + HOUR_MS - 1) // HOUR_MS + HOLD_HOURS) * HOUR_MS
            if target > completed:
                continue
            name = hl_kind("perp", coin).lower()
            day = pd.Timestamp(stamp, unit="ms", tz="UTC").normalize()
            equity = equity_by_book.get(name)
            if equity is None or day not in equity.index or not (equity.loc[day] > 0):
                continue
            samples[name].append(((wallet, oid), day, coin, target, side, exit_price, size, float(equity.loc[day])))
            targets[coin].add(target)

    marks = _marks(targets)
    result = {}
    coverage_start = pd.Timestamp(max(coverage), unit="ms", tz="UTC").strftime("%Y-%m-%d") if coverage else None
    for name, periods in windows.items():
        result[name] = {}
        for label, window in periods.items():
            dates = window.get("days")
            if dates is None:
                first, last = (pd.Timestamp(window[key], tz="UTC") for key in ("start", "end"))
            estimates = defaultdict(lambda: [0.0, 0.0])
            identified, incomplete = set(), set()
            for key, day, coin, target, side, exit_price, size, equity in samples.get(name, []):
                if not (day in dates if dates is not None else first < day <= last):
                    continue
                identified.add(key)
                mark = marks.get((coin, target))
                if mark is None:
                    incomplete.add(key)
                    continue
                effect = side * size * (exit_price - mark)
                estimates[key][0] += effect
                estimates[key][1] += effect / equity
            for key in incomplete:
                estimates.pop(key, None)
            result[name][label] = {
                "effect": sum(total[0] for total in estimates.values()) if estimates else None,
                "share": sum(total[1] for total in estimates.values()) if estimates else None,
                "covered": len(estimates), "identified": len(identified),
                "helped": sum(total[0] > 0 for total in estimates.values()),
                "hurt": sum(total[0] < 0 for total in estimates.values()),
                "coverage_start": coverage_start, "wallets_read": wallets_read, "wallets": wallet_count,
            }
    return result
