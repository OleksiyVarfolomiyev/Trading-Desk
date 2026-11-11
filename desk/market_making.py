"""Market-making ladders: how they are doing, for the dashboards and notebooks.

The trading daemon's ladders (``hl_daemon.py ladder``) mark every order with a client order id
starting ``0x1add``, so HL's own fill history tells their fills apart from the positions you trade.
Per ladder, from those fills alone:

* Fills, Volume: how many ladder orders filled, and their notional;
* Spread captured: the matched part (as much bought as sold) times the average sell less the
  average buy: what the round trips earned;
* Ladder PnL: every ladder fill's cash, plus what the ladder still holds net at today's mark, less
  fees: the strategy's own result, apart from the position you opened;
* Fees: paid, or negative for maker rebates;
* Position vs base: the position now against the size the ladder was started on (its band is
  +/-50%).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

LADDERS_FILE = Path.home() / ".trading-desk" / "ladders.json"
LADDER_CLOID_PREFIX = "0x1add"
BAND = 0.50
COLUMNS = ["Account", "Coin", "Side", "Status", "Since", "Fills", "Volume", "Spread captured", "Ladder PnL", "Fees",
           "Position vs base", "Quotes"]
FORMATS = {"Fills": "int", "Volume": "usd", "Spread captured": "susd", "Ladder PnL": "susd", "Fees": "usd",
           "Position vs base": "spct", "Quotes": "int"}


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
    return {"Fills": len(fills), "Volume": cost + proceeds, "Spread captured": captured, "Ladder PnL": pnl, "Fees": fees}


def market_making_frame(quotes: dict[tuple[str, str], int] | None = None) -> pd.DataFrame:
    """One row per ladder (running, or stopped with fills in the last 30 days) and a total.

    ``quotes`` ((wallet last four, coin) -> resting quotes) comes from the daemon when it is known.
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
        fills = ladder_fill_rows(wallet, min(month_ago, int(min(starts).timestamp() * 1000)))
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
            row = {"Account": wallets[wallet].name, "Coin": coin,
                   "Side": ("LONG" if base > 0 else "SHORT") if ladder else "",
                   "Status": "quoting" if ladder else "stopped",
                   "Since": ladder["started"] if ladder else "", **metrics(own, mark),
                   "Position vs base": size / base - 1 if ladder and base else float("nan"),
                   "Quotes": (quotes or {}).get((short_id(wallet), coin), float("nan"))}
            rows.append(row)
    frame = pd.DataFrame(rows, columns=COLUMNS)
    return frame


def total_row(frame: pd.DataFrame) -> dict:
    return {"Account": "Total", **{column: frame[column].sum() for column in ("Fills", "Volume", "Spread captured",
                                                                              "Ladder PnL", "Fees")}}


def show_market_making() -> None:
    """The ladders' table for the notebooks."""
    from desk.hl.account_analytics import display_interactive_table

    frame = market_making_frame()
    if frame.empty:
        print("No market-making ladder has run (hl_daemon.py ladder starts one).")
        return
    frame = pd.concat([frame, pd.DataFrame([total_row(frame)])], ignore_index=True)
    money = lambda value: "" if value != value else (f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}")
    display_interactive_table(
        frame, bold_last=True, title="MARKET MAKING (LADDERS)",
        formatters={"Volume": money, "Spread captured": money, "Ladder PnL": money, "Fees": money,
                    "Fills": lambda value: "" if value != value else f"{value:,.0f}",
                    "Quotes": lambda value: "" if value != value else f"{value:,.0f}",
                    "Position vs base": lambda value: "" if value != value else f"{value:.1%}"},
        key_columns={"Ladder PnL": "abs"})
    print("Ladder PnL is the ladders' own trading (each fill's cash, plus what they still hold net at today's mark, "
          "less fees), apart from the position you opened. Position vs base: the band is +/-50%.")
