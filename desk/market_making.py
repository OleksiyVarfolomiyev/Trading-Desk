"""Market-making ladders: how they are doing, for the dashboards and notebooks.

The trading daemon's ladders (``hl_daemon.py ladder``) mark every order with a client order id
starting ``0x1add``, so HL's own fill history tells their fills apart from the positions you trade.
Per ladder, from those fills alone:

* Fills, Volume: how many ladder orders filled, and their notional;
* Spread captured: the matched part (as much bought as sold) times the average sell less the
  average buy: what the round trips earned;
* Inventory drift: what the unmatched part (bought more than sold, or the other way) made or lost
  as the price moved: what trimming into a trend costs;
* Fees: what the ladders paid, as a negative amount (a maker rebate would be positive);
* Funding: what the ladder's own inventory (the position above or below its base) earned (+) or paid (-)
  in funding: each hourly payment times the share of the position that was ladder inventory then;
* Ladder PnL = spread captured + inventory drift + fees + funding: every ladder fill's cash, plus what the
  ladder still holds net at today's mark, less fees, plus the funding on that inventory: the strategy's
  result against simply holding the position you opened;
* Markout 1m / 15m: how far the price moved for (+) or against (-) each fill 1 and 15 minutes
  later, in basis points, averaged over the fills of the last three days. Negative means the
  quotes fill when the price runs through them (adverse selection), which the spacing has to beat;
* Position vs base: the position now against the size the ladder was started on (its band is in
  ``~/.trading-desk/ladder.json``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
from desk.numbers import since, usd as _usd

LADDERS_FILE = Path.home() / ".trading-desk" / "ladders.json"
LADDER_CLOID_PREFIX = "0x1add"
DISPLAY_TIMEZONE = "America/New_York"  # the desk's clock: times are shown in Eastern time
MARKOUT_MINUTES = (1, 15)
MARKOUT_DAYS = 3  # HL serves about 3.5 days of 1-minute candles
COLUMNS = ["Account", "Coin", "Side", "Status", "Since", "Fills", "Volume", "Spread captured", "Inventory drift", "Fees",
           "Funding", "Ladder PnL", "Captured per $1k", "Markout 1m (bps)", "Markout 15m (bps)", "Position vs base", "Quotes", "Queued"]
FORMATS = {"Fills": "int", "Volume": "usd", "Spread captured": "susd", "Inventory drift": "susd", "Fees": "susd",
           "Funding": "susd", "Ladder PnL": "susd", "Captured per $1k": "susd", "Markout 1m (bps)": "sbps", "Markout 15m (bps)": "sbps",
           "Position vs base": "spct", "Quotes": "int", "Queued": "int"}


def eastern(utc_text: str) -> str:
    """A UTC stamp (as the daemon records it) on the desk's clock: ``10 am``, ``Nov 12, 5:24 pm``."""
    from desk.numbers import clock

    return clock(utc_text)


def running_ladders(path: Path = LADDERS_FILE) -> list[dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def ladder_fill_rows(wallet: str, since_ms: int) -> list[dict]:
    """The ladder's own fills in a wallet since ``since_ms`` (HL's fill history, by client order id)."""
    from desk.hl.account_analytics import post

    fills, start, now = [], since_ms, int(time.time() * 1000)
    while True:
        batch = post({"type": "userFillsByTime", "user": wallet, "startTime": start, "endTime": now}) or []
        fills += batch
        if len(batch) < 2000:
            break
        start = max(fill["time"] for fill in batch) + 1
    unique = {fill["tid"]: fill for fill in fills}.values()
    return [fill for fill in unique if str(fill.get("cloid") or "").startswith(LADDER_CLOID_PREFIX)]


_FUNDING_EVENTS: dict[str, list[dict]] = {}  # wallet -> funding events already downloaded this run


def funding_events(wallet: str, since_ms: int) -> list[dict]:
    """HL's funding events for a wallet from ``since_ms``, fetching only what is new since the last call."""
    from desk.hl.account_analytics import _download_user_funding

    events = [event for event in _FUNDING_EVENTS.get(wallet, []) if int(event["time"]) >= since_ms]
    latest = max((int(event["time"]) for event in events), default=None)
    fresh = _download_user_funding(wallet, since_ms if latest is None else latest + 1)
    key = lambda event: (int(event["time"]), (event.get("delta") or {}).get("coin"))
    seen = {key(event) for event in events}
    events += [event for event in fresh if key(event) not in seen]
    _FUNDING_EVENTS[wallet] = events
    return events


def ladder_funding_parts(ladder_fills: list[dict], events: list[dict]):
    """Each funding event of one coin with the part that belongs to the ladder's inventory: ``(event, amount)``.

    Funding is paid on the whole position (the event's ``szi``). The ladder's inventory at that moment is
    the net of its fills so far (bought minus sold), so its share of the signed payment ``usdc`` is
    ``usdc * inventory / szi``. The rest belongs to the position the ladder trades around.
    """
    fills = sorted(ladder_fills, key=lambda fill: fill["time"])
    inventory, index = 0.0, 0
    for event in sorted(events, key=lambda item: int(item["time"])):
        while index < len(fills) and fills[index]["time"] <= int(event["time"]):
            inventory += float(fills[index]["sz"]) * (1.0 if fills[index]["side"] == "B" else -1.0)
            index += 1
        delta = event.get("delta") or {}
        size = float(delta.get("szi") or 0)
        yield event, (float(delta["usdc"]) * inventory / size if size and delta.get("usdc") is not None else 0.0)


def metrics(fills: list[dict], mark: float) -> dict:
    """Fills, volume, spread captured, ladder PnL (fills' cash + net holding at ``mark`` - fees) and fees."""
    bought = sold = cost = proceeds = fees = 0.0
    for fill in fills:
        price, size = float(fill["px"]), float(fill["sz"])
        fees += float(fill.get("fee") or 0)
        if fill["side"] == "B":
            bought, cost = bought + size, cost + price * size
        else:
            sold, proceeds = sold + size, proceeds + price * size
    matched = min(bought, sold)
    captured = matched * (proceeds / sold - cost / bought) if bought and sold else 0.0
    net = bought - sold
    pnl = proceeds - cost + (net * mark if mark == mark else 0.0) - fees
    volume = cost + proceeds
    # The ladder's edge, apart from where the price went: what the round trips earned, net of fees, per $1,000 traded.
    per_thousand = (captured - fees) / volume * 1000 if volume else float("nan")
    # Fees paid are a cost: shown negative, so spread captured + inventory drift + fees = ladder PnL.
    return {"Fills": len(fills), "Volume": volume, "Spread captured": captured, "Inventory drift": pnl - captured + fees,
            "Captured per $1k": per_thousand, "Ladder PnL": pnl, "Fees": -fees}


def markouts(fills: list[dict], closes: pd.Series, minutes: tuple[int, ...] = MARKOUT_MINUTES) -> dict[str, float]:
    """The average move after each fill, in basis points, for (+) or against (-) the fill, ``minutes`` later.

    ``closes`` holds 1-minute closes indexed by the candle's open time in ms; a fill counts only once a
    candle covers ``fill time + minutes``.
    """
    result = {}
    for minute in minutes:
        moves = []
        if len(closes):
            times, values, last = closes.index.to_numpy(), closes.to_numpy(), closes.index[-1]
            for fill in fills:
                target = fill["time"] + minute * 60_000
                if target < times[0] or target > last + 60_000:
                    continue
                later = values[times.searchsorted(target, side="right") - 1]
                side = 1.0 if fill["side"] == "B" else -1.0
                moves.append(side * (later / float(fill["px"]) - 1) * 1e4)
        result[f"Markout {minute}m (bps)"] = sum(moves) / len(moves) if moves else float("nan")
    return result


def minute_closes(coin: str, since_ms: int) -> pd.Series:
    """1-minute closes of an HL market from ``since_ms`` (at most the last ``MARKOUT_DAYS``), by open time in ms."""
    from desk.hl.account_analytics import post

    now = int(time.time() * 1000)
    start = max(since_ms, now - MARKOUT_DAYS * 86_400_000)
    request = {"type": "candleSnapshot", "req": {"coin": coin.split(":")[-1], "interval": "1m", "startTime": start, "endTime": now}}
    if ":" in coin:
        request["dex"] = coin.split(":")[0]
    candles = post(request) or []
    return pd.Series({int(candle["t"]): float(candle["c"]) for candle in candles}, dtype=float).sort_index()


def market_making_frame(quotes: dict[tuple[str, str], int] | None = None,
                        queued: dict[tuple[str, str], int] | None = None,
                        status: dict[tuple[str, str], str] | None = None) -> pd.DataFrame:
    """One row per ladder (running, or stopped with fills in the last 30 days) and a total.

    ``quotes`` ((wallet last four, coin) -> quotes resting on HL's book), ``queued`` (quotes the daemon holds
    in memory until the price nears them) and ``status`` (e.g. trims paused in a trend) come from the daemon
    when it is known.
    """
    from desk.accounts import load_accounts
    from desk.hl.account_analytics import post
    from desk.privacy import short_id

    ladders = running_ladders()
    wallets = {account.id.lower(): account for account in load_accounts() if account.venue == "hl"}
    by_wallet: dict[str, list[dict]] = {}
    for ladder in ladders:
        by_wallet.setdefault(ladder["wallet"].lower(), []).append(ladder)
    rows = []
    month_ago = int((time.time() - 30 * 86400) * 1000)
    for wallet in by_wallet:
        if wallet not in wallets:
            continue
        state = post({"type": "clearinghouseState", "user": wallet}) or {}
        positions = {item["position"]["coin"]: item["position"] for item in state.get("assetPositions", [])}
        starts = [pd.Timestamp(item["started"], tz="UTC") for item in by_wallet[wallet]]
        window_start = min(month_ago, int(min(starts).timestamp() * 1000))
        fills = ladder_fill_rows(wallet, window_start)
        try:  # funding is extra: the table shows without it
            events = funding_events(wallet, window_start)
        except Exception:
            events = []
        running = {item["coin"]: item for item in by_wallet[wallet]}
        for coin in sorted(set(running) | {fill["coin"] for fill in fills}):
            ladder = running.get(coin)
            position = positions.get(coin, {})
            size = float(position.get("szi") or 0)
            mark = abs(float(position.get("positionValue") or 0) / size) if size else float("nan")
            since = pd.Timestamp(ladder["started"], tz="UTC") if ladder else None
            own = [fill for fill in fills if fill["coin"] == coin and (since is None or fill["time"] >= since.timestamp() * 1000)]
            if mark != mark and own:
                mark = float(own[-1]["px"])
            base = float(ladder["base"]) if ladder else float("nan")
            try:
                closes = minute_closes(coin, min(fill["time"] for fill in own)) if own else pd.Series(dtype=float)
            except Exception:  # markouts are extra: the table shows without them
                closes = pd.Series(dtype=float)
            earned = sum(amount for _, amount in ladder_funding_parts(
                own, [event for event in events if (event.get("delta") or {}).get("coin") == coin
                      and (since is None or int(event["time"]) >= since.timestamp() * 1000)]))
            measured = metrics(own, mark)
            measured["Funding"] = earned
            measured["Ladder PnL"] += earned  # the funding on the ladder's inventory is part of its result
            row = {"Account": wallets[wallet].name, "Coin": coin,
                   "Side": ("LONG" if base > 0 else "SHORT") if ladder else "",
                   "Status": ((status or {}).get((short_id(wallet), coin), "quoting")) if ladder else "stopped",
                   "Since": since(ladder["started"]) if ladder else "", **measured, **markouts(own, closes),
                   "Position vs base": size / base - 1 if ladder and base else float("nan"),
                   "Quotes": (quotes or {}).get((short_id(wallet), coin), float("nan")),
                   "Queued": (queued or {}).get((short_id(wallet), coin), float("nan"))}
            rows.append(row)
    frame = pd.DataFrame(rows, columns=COLUMNS)
    return frame


def total_row(frame: pd.DataFrame) -> dict:
    row = {"Account": "Total", **{column: frame[column].sum() for column in ("Fills", "Volume", "Spread captured",
                                                                             "Inventory drift", "Funding", "Ladder PnL", "Fees")}}
    row["Captured per $1k"] = (row["Spread captured"] + row["Fees"]) / row["Volume"] * 1000 if row["Volume"] else float("nan")
    for column in (f"Markout {minute}m (bps)" for minute in MARKOUT_MINUTES):  # weighted by each ladder's fills
        known = frame[frame[column].notna()]
        row[column] = (known[column] * known["Fills"]).sum() / known["Fills"].sum() if known["Fills"].sum() else float("nan")
    return row


def show_market_making() -> None:
    """The ladders' table for the notebooks."""
    from desk.hl.account_analytics import display_interactive_table

    ladders = running_ladders()
    print(f"Ladders on file: {', '.join(item['coin'] + ' (...' + item['wallet'][-4:] + ')' for item in ladders) or 'none'} "
          f"({LADDERS_FILE}).")
    try:
        frame = market_making_frame()
    except Exception as exc:  # say what failed instead of showing nothing
        print(f"Market-making metrics could not be read: {type(exc).__name__}: {exc}")
        return
    if frame.empty:
        print("No market-making ladder is running or has traded in the last 30 days: start one with "
              "hl_daemon.py ladder --wallet c248 --coin X (the trading daemon runs the ladders).")
        return
    frame = pd.concat([frame, pd.DataFrame([total_row(frame)])], ignore_index=True)
    money = lambda value: "" if value != value else (f"{_usd(-abs(value))}" if value < 0 else f"{_usd(value)}")
    bps = lambda value: "" if value != value else f"{value:.1f}"
    display_interactive_table(
        frame, bold_last=True, title="MARKET MAKING (LADDERS)",
        formatters={"Volume": money, "Spread captured": money, "Inventory drift": money, "Funding": money, "Ladder PnL": money, "Fees": money,
                    "Captured per $1k": money, "Markout 1m (bps)": bps, "Markout 15m (bps)": bps,
                    "Fills": lambda value: "" if value != value else f"{value:,.0f}",
                    "Quotes": lambda value: "" if value != value else f"{value:,.0f}",
                    "Queued": lambda value: "" if value != value else f"{value:,.0f}",
                    "Position vs base": lambda value: "" if value != value else f"{value:.1%}"},
        key_columns={"Ladder PnL": "abs"})
    print("Ladder PnL is the ladders' result against simply holding the position you opened: spread captured (round "
          "trips) plus inventory drift (what the unmatched buys or sells made as the price moved) plus fees (paid: negative) plus funding (on the ladder's own inventory only: received positive, paid negative). Captured per "
          "$1k is the edge apart from the price's path. Markouts: the move 1 and 15 minutes after each fill, for (+) or "
          "against (-) it, in bps over the last 3 days; negative means the price runs through the quotes. "
          "Position vs base: the band is in ladder.json.")
