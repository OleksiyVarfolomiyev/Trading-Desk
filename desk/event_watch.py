"""Event watch: supply events (burns, unlocks), a heads-up before the scheduled ones, and a debrief after each.

The framework: every event the desk sees (a token burn or unlock, a hack, an alt rally, a stock dip) is logged with
the prices of the moment (the event's coin, BTC and the 40-alt basket). 1 and 7 days later the log is filled in
with what followed, measured against the alt basket; a coin's own events (burns, unlocks) also get a debrief
alert. Over time the log answers "after this kind of news, what did the price do?" for the coins the desk trades.

Supply sources, read for free:
* **VVV** (Base): the VVV held by the zero address, where Venice burns. It burns a trickle with every subscription
  and API credit purchase, and once a month buys VVV with platform revenue and burns it, announced on X (Dec 8,
  2025 onward; $458K on Nov 9, 2026). A day whose burn is far above the trickle and worth BURN_ALERT_USD is that
  monthly burn; the next is expected a month after the last, and BURN_HEADS_UP_DAYS before it a heads-up goes out.
* **HYPE** (HL): circulating supply from HL's token details; a jump of UNLOCK_SHARE in a day is an unlock.
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENTS_FILE = ROOT / "data" / "market_events.jsonl"
SUPPLY_FILE = ROOT / "data" / "supply_history.json"
BURN_ALERT_USD = 100_000
BURN_SPIKE = 5.0                  # a discretionary burn: this many times the usual daily trickle
BURN_HEADS_UP_DAYS = 3
UNLOCK_SHARE = 0.003              # circulating supply up 0.3% in a day
HORIZONS = {"1d": 86400, "7d": 7 * 86400}
DEBRIEF_KINDS = {"burn", "unlock"}  # events about one coin get a debrief alert; the rest are logged only
VVV = {"coin": "VVV", "chain": "base", "token": "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
       "burn_address": "0x0000000000000000000000000000000000000000"}
HYPE = {"coin": "HYPE", "token_id": "0x0d01dc56dcaaca66ad901c959b4011ec"}
# The monthly buy-and-burns known before the watch began (Venice's announcements), to date the next one.
KNOWN_VVV_BURNS = [("2026-09-08", 391_000.0), ("2026-11-09", 458_000.0)]


# ------------------------------------------------------------------ reading supply
def vvv_burned() -> float:
    """VVV held by the zero address (burned to date), from a public Base node."""
    from desk.venues import onchain

    padded = VVV["burn_address"][2:].rjust(64, "0")
    calls = [{"jsonrpc": "2.0", "id": 0, "method": "eth_call",
              "params": [{"to": VVV["token"], "data": "0x70a08231" + padded}, "latest"]}]
    raw = onchain._rpc_batch(onchain.PUBLIC_RPC[VVV["chain"]], calls).get(0)
    return int(raw, 16) / 1e18 if raw and raw != "0x" else float("nan")


def hype_circulating() -> float:
    from desk.hl.trade_analytics import post_info

    details = post_info({"type": "tokenDetails", "tokenId": HYPE["token_id"]}) or {}
    return float(details.get("circulatingSupply") or "nan")


# ------------------------------------------------------------------ daily supply history
def load_supply(path: Path | None = None) -> dict:
    try:
        return json.loads((path or SUPPLY_FILE).read_text())
    except (OSError, ValueError):
        return {}


def record_supply(history: dict, coin: str, value: float, day: str) -> dict:
    """Keep the last reading of each UTC day per coin: ``history[coin][day] = value``."""
    if math.isfinite(value):
        history.setdefault(coin, {})[day] = value
    return history


def day_change(history: dict, coin: str, day: str) -> float:
    """The change of ``coin``'s reading from the previous recorded day to ``day`` (nan without both)."""
    series = history.get(coin, {})
    earlier = sorted(key for key in series if key < day)
    return series[day] - series[earlier[-1]] if day in series and earlier else float("nan")


def usual_burn(history: dict, coin: str, day: str, days: int = 30) -> float:
    """The median daily burn over the ``days`` before ``day`` (the programmatic trickle)."""
    series = history.get(coin, {})
    keys = sorted(key for key in series if key < day)[-(days + 1):]
    changes = sorted(series[b] - series[a] for a, b in zip(keys, keys[1:]) if series[b] >= series[a])
    return changes[len(changes) // 2] if changes else float("nan")


def is_discretionary_burn(burned_today: float, usual: float, price: float) -> bool:
    """A day's burn far above the trickle and worth BURN_ALERT_USD: the monthly buy-and-burn."""
    if not (math.isfinite(burned_today) and burned_today > 0 and math.isfinite(price)):
        return False
    spike = not math.isfinite(usual) or usual <= 0 or burned_today >= BURN_SPIKE * usual
    return spike and burned_today * price >= BURN_ALERT_USD


def next_burn(burns: list[tuple[str, float]]) -> date | None:
    """The expected date of the next monthly burn: the same day of the month after the last one."""
    if not burns:
        return None
    last = date.fromisoformat(sorted(burns)[-1][0])
    month, year = (last.month % 12) + 1, last.year + (last.month == 12)
    for day in (last.day, 28):  # Jan 31 -> Feb 28
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def heads_up(burns: list[tuple[str, float]], today: date) -> tuple[str, str] | None:
    """("Burn approaching", text) within BURN_HEADS_UP_DAYS of the expected burn (and a day after), else None."""
    expected = next_burn(burns)
    if expected is None:
        return None
    days = (expected - today).days
    if not -1 <= days <= BURN_HEADS_UP_DAYS:
        return None
    when = "today" if days == 0 else "tomorrow" if days == 1 else "overdue by a day" if days < 0 else f"in {days} days"
    last_day, last_usd = sorted(burns)[-1]
    from desk.numbers import usd

    return ("Burn approaching", f"VVV monthly buy-and-burn expected {when} (~{expected:%b} {expected.day}); "
            f"last {date.fromisoformat(last_day):%b} {date.fromisoformat(last_day).day}, {usd(last_usd)}")


# ------------------------------------------------------------------ the event log and its debriefs
def prices(meta: dict, contexts: list[dict], coins: list[str]) -> dict:
    """Marks of ``coins``, BTC and the alt basket's members from HL's main-DEX answer: {"marks": {...}, "basket": [...]}."""
    from desk.alt_rally import ALT_COUNT, EXCLUDED

    marks, rows = {}, []
    for asset, context in zip(meta.get("universe", []), contexts):
        name = str(asset.get("name", ""))
        try:
            mark, volume = float(context.get("markPx") or 0), float(context.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            continue
        if name in coins or name == "BTC":
            marks[name] = mark
        if not asset.get("isDelisted") and name not in EXCLUDED and not name.startswith("k") and mark > 0:
            rows.append((volume, name, mark))
    basket = sorted(rows, reverse=True)[:ALT_COUNT]
    marks.update({name: mark for _, name, mark in basket})
    return {"marks": marks, "basket": [name for _, name, _ in basket]}


def record(kind: str, info: dict, snapshot: dict, path: Path | None = None, now: float | None = None) -> dict:
    """Append an event: its kind, what happened, and the prices of the moment (for the debriefs)."""
    entry = {"time": time.time() if now is None else now, "kind": kind, **info, "prices": snapshot, "outcomes": {}}
    path = path or EVENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, default=str) + "\n")
    return entry


def load_events(path: Path | None = None) -> list[dict]:
    try:
        return [json.loads(line) for line in (path or EVENTS_FILE).read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def outcome(event: dict, marks_now: dict) -> dict:
    """The event's coin, BTC and the alt basket since the event, from the marks then and now."""
    then = (event.get("prices") or {}).get("marks", {})
    change = lambda name: marks_now[name] / then[name] - 1 if then.get(name) and marks_now.get(name) else float("nan")
    basket = [change(name) for name in (event.get("prices") or {}).get("basket", [])]
    basket = [value for value in basket if math.isfinite(value)]
    result = {"btc": change("BTC"), "alts": sum(basket) / len(basket) if basket else float("nan")}
    if event.get("coin"):
        result["coin"] = change(event["coin"])
    return result


def debrief(event: dict, horizon: str) -> tuple[str, str] | None:
    """The debrief alert for a coin's event at ``horizon``: "VVV 3.1% 1d after the Nov 9 burn (alts -1%, BTC 0.4%)"."""
    from desk.numbers import pct

    result = (event.get("outcomes") or {}).get(horizon)
    if event.get("kind") not in DEBRIEF_KINDS or not result or not event.get("coin"):
        return None
    when = time.gmtime(event["time"])
    label = f"{time.strftime('%b', when)} {when.tm_mday}"
    return (f"{event['kind'].capitalize()} debrief",
            f"{event['coin']} {pct(result.get('coin'))} {horizon} after the {label} {event['kind']} "
            f"(alts {pct(result.get('alts'))}, BTC {pct(result.get('btc'))})")


def due_outcomes(events: list[dict], marks_now: dict, now: float) -> list[tuple[dict, str]]:
    """Fill in each event's outcome at every horizon it has reached; returns the (event, horizon) pairs just filled."""
    filled = []
    for event in events:
        for horizon, seconds in HORIZONS.items():
            if horizon not in event.setdefault("outcomes", {}) and now - float(event.get("time") or now) >= seconds:
                event["outcomes"][horizon] = outcome(event, marks_now)
                filled.append((event, horizon))
    return filled


def save_events(events: list[dict], path: Path | None = None) -> None:
    (path or EVENTS_FILE).write_text("".join(json.dumps(event, default=str) + "\n" for event in events))
