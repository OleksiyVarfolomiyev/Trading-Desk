"""Trade journal: which strategy each position belongs to, so results can be read per strategy.

Every entry the executors make is recorded here with its strategy:

* ``directional``: a normal trade with a stop and take-profit;
* ``conviction``: opened (or kept) without a stop;
* ``hedge``: offsets a holding elsewhere (the VVV short against the staked VVV);
* ``market making``: never recorded here; the ladders mark their own orders (client order ids
  starting ``0x1add``), so their fills are told apart whatever position they trade around.

The journal is ``data/trade_journal.json`` (local, not in git), keyed by the wallet's last four
characters and the coin. ``strategy_frame`` reads HL's fill history and splits the last 30 days'
realized PnL, fees and volume, and today's unrealized PnL, by strategy.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from desk import market_making

import os

JOURNAL_FILE = Path(__file__).resolve().parents[1] / "data" / "trade_journal.json"


def journal_path() -> Path:
    """The journal file: ``DESK_JOURNAL_FILE`` if set (tests point it at a scratch file), else the desk's."""
    return Path(os.environ.get("DESK_JOURNAL_FILE") or JOURNAL_FILE)
STRATEGIES = ("directional", "conviction", "hedge")
MARKET_MAKING = "market making"
UNTAGGED = "untagged (before the journal)"
LADDER_CLOID_PREFIX = "0x1add"
REVIEW_AFTER_DAYS = 7  # directional trades held longer lost money in the desk's history (desk/trade_rules.py)
COLUMNS = ["Strategy", "Open positions", "Unrealized PnL", "Realized PnL (30d)", "Fees (30d)", "Funding (30d)", "Volume (30d)",
           "Fills (30d)"]
FORMATS = {"Open positions": "int", "Unrealized PnL": "susd", "Realized PnL (30d)": "susd", "Fees (30d)": "susd",
           "Funding (30d)": "susd", "Volume (30d)": "usd", "Fills (30d)": "int"}


def load(path: Path | None = None) -> list[dict]:
    path = path or journal_path()
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def save(entries: list[dict], path: Path | None = None) -> None:
    path = path or journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(entries, indent=1))
    temporary.replace(path)


def record_entry(wallet: str, coin: str, side: str, strategy: str, signals: dict | None = None,
                 opened: str | None = None, path: Path | None = None) -> dict:
    """Record (or re-tag) the open position in ``coin``: one open entry per wallet and coin.

    Adding to a position keeps its first entry time; a new strategy replaces the old tag.
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
    entries = load(path)
    key = (str(wallet)[-4:].lower(), coin)
    now = opened or time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    current = next((item for item in entries if (item["wallet"], item["coin"]) == key and not item.get("closed")), None)
    if current is None:
        current = {"wallet": key[0], "coin": coin, "side": side.upper(), "strategy": strategy, "opened": now,
                   "signals": signals or {}, "closed": None}
        entries.append(current)
    else:
        current.update(strategy=strategy, side=side.upper())
        if signals:
            current["signals"] = signals
    save(entries, path)
    return current


def close_missing(open_positions: set[tuple[str, str]], path: Path | None = None) -> int:
    """Mark journal entries whose position is no longer open (``(wallet last four, coin)`` pairs) as closed."""
    entries, changed = load(path), 0
    for item in entries:
        if not item.get("closed") and (item["wallet"], item["coin"]) not in open_positions:
            item["closed"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            changed += 1
    if changed:
        save(entries, path)
    return changed


def strategy_at(entries: list[dict], wallet: str, coin: str, when_ms: float) -> str:
    """The strategy of the journal entry covering a fill's time, or ``UNTAGGED``."""
    key = (str(wallet)[-4:].lower(), coin)
    for item in entries:
        if (item["wallet"], item["coin"]) != key:
            continue
        opened = pd.Timestamp(item["opened"], tz="UTC").timestamp() * 1000
        closed = pd.Timestamp(item["closed"], tz="UTC").timestamp() * 1000 if item.get("closed") else float("inf")
        if opened - 60_000 <= when_ms <= closed + 60_000:
            return item["strategy"]
    return UNTAGGED


def fill_strategy(entries: list[dict], wallet: str, fill: dict) -> str:
    if str(fill.get("cloid") or "").startswith(LADDER_CLOID_PREFIX):
        return MARKET_MAKING
    return strategy_at(entries, wallet, fill["coin"], fill["time"])


def split_by_strategy(entries: list[dict], fills_by_wallet: dict[str, list[dict]],
                      positions_by_wallet: dict[str, list[dict]],
                      funding_by_wallet: dict[str, list[dict]] | None = None) -> pd.DataFrame:
    """Per strategy: open positions and unrealized PnL now; realized PnL, fees, volume and fills from ``fills``.

    ``positions_by_wallet`` holds HL position dicts (``coin``, ``szi``, ``unrealizedPnl``, ``positionValue``). A
    position's unrealized PnL goes to its journal strategy (the ladder trades around it, but the position is yours).

    A ladder fill's realized PnL is split: market making gets only what the fill made against holding, its
    cash plus its size at today's mark less its fee (summed, the ladder PnL of desk/market_making.py); the
    rest, the position's own profit the fill happened to realize, stays with the position's strategy. The
    ladder's fees, volume and fills count under market making; the rows still add up to HL's realized PnL.

    ``funding_by_wallet`` holds HL's funding events (``time`` and ``delta`` with ``coin`` and signed ``usdc``:
    positive received). Funding goes to the strategy the position had when it was paid, never to market making.
    """
    rows: dict[str, dict] = {}

    def row(name: str) -> dict:
        return rows.setdefault(name, {"Strategy": name, "Open positions": 0, "Unrealized PnL": 0.0, "Realized PnL (30d)": 0.0,
                                      "Fees (30d)": 0.0, "Funding (30d)": 0.0, "Volume (30d)": 0.0, "Fills (30d)": 0})

    now_ms = time.time() * 1000
    marks: dict[tuple[str, str], float] = {}
    for wallet, positions in positions_by_wallet.items():
        for position in positions:
            size = float(position.get("szi") or 0)
            if not size:
                continue
            if position.get("positionValue"):
                marks[(wallet, position["coin"])] = abs(float(position["positionValue"]) / size)
            target = row(strategy_at(entries, wallet, position["coin"], now_ms))
            target["Open positions"] += 1
            target["Unrealized PnL"] += float(position.get("unrealizedPnl") or 0)
    for wallet, fills in fills_by_wallet.items():  # no open position (or no value given): the coin's last fill price
        for fill in sorted(fills, key=lambda item: item["time"], reverse=True):
            marks.setdefault((wallet, fill["coin"]), float(fill["px"]))
    for wallet, fills in fills_by_wallet.items():
        for fill in fills:
            if str(fill["coin"]).startswith("@") or "/" in str(fill["coin"]):
                continue  # spot
            price, size, fee = float(fill["px"]), float(fill["sz"]), float(fill.get("fee") or 0)
            realized = float(fill.get("closedPnl") or 0) - fee
            strategy = fill_strategy(entries, wallet, fill)
            if strategy == MARKET_MAKING:
                side = 1.0 if fill["side"] == "B" else -1.0
                own = side * size * (marks[(wallet, fill["coin"])] - price) - fee  # the fill against holding
                row(strategy_at(entries, wallet, fill["coin"], fill["time"]))["Realized PnL (30d)"] += realized - own
                realized = own
            target = row(strategy)
            target["Realized PnL (30d)"] += realized
            target["Fees (30d)"] -= fee  # paid: a cost, shown negative
            target["Volume (30d)"] += price * size
            target["Fills (30d)"] += 1
    for wallet, events in (funding_by_wallet or {}).items():
        by_coin: dict[str, list[dict]] = {}
        for event in events:
            if (event.get("delta") or {}).get("usdc") is not None and event.get("time") is not None:
                by_coin.setdefault(str((event["delta"]).get("coin")), []).append(event)
        for coin, coin_events in by_coin.items():
            ladder_fills = [fill for fill in fills_by_wallet.get(wallet, []) if fill["coin"] == coin
                            and str(fill.get("cloid") or "").startswith(LADDER_CLOID_PREFIX)]
            for event, ladder_part in market_making.ladder_funding_parts(ladder_fills, coin_events):
                when = int(event["time"])
                if ladder_part:
                    row(MARKET_MAKING)["Funding (30d)"] += ladder_part  # the ladder's inventory...
                row(strategy_at(entries, wallet, coin, when))["Funding (30d)"] += float(event["delta"]["usdc"]) - ladder_part  # ...the rest
    order = {name: index for index, name in enumerate((*STRATEGIES, MARKET_MAKING, UNTAGGED))}
    frame = pd.DataFrame(sorted(rows.values(), key=lambda item: order.get(item["Strategy"], 99)), columns=COLUMNS)
    return frame


def strategy_frame(days: int = 30) -> pd.DataFrame:
    """The strategies table from HL's own data for every configured HL wallet."""
    from desk.accounts import accounts_for
    from desk.hl.account_analytics import post

    entries = load()
    since, now = int((time.time() - days * 86400) * 1000), int(time.time() * 1000)
    fills_by_wallet, positions_by_wallet, funding_by_wallet = {}, {}, {}
    for account in accounts_for("hl"):
        fills, start = [], since
        while True:
            batch = post({"type": "userFillsByTime", "user": account.id, "startTime": start, "endTime": now}) or []
            fills += batch
            if len(batch) < 2000:
                break
            start = max(fill["time"] for fill in batch) + 1
        fills_by_wallet[account.id] = list({fill["tid"]: fill for fill in fills}.values())
        state = post({"type": "clearinghouseState", "user": account.id}) or {}
        positions_by_wallet[account.id] = [item["position"] for item in state.get("assetPositions", [])]
        funding_by_wallet[account.id] = market_making.funding_events(account.id, since)
    return split_by_strategy(entries, fills_by_wallet, positions_by_wallet, funding_by_wallet)


def total_row(frame: pd.DataFrame) -> dict:
    return {"Strategy": "Total", **{column: frame[column].sum() for column in COLUMNS[1:]}}


def overdue(entries: list[dict], open_positions: set[tuple[str, str]], now: float | None = None) -> list[dict]:
    """Open directional entries held longer than ``REVIEW_AFTER_DAYS`` (hedges and conviction trades are meant to last)."""
    now = time.time() if now is None else now
    rows = []
    for item in entries:
        if item.get("closed") or item["strategy"] != "directional" or (item["wallet"], item["coin"]) not in open_positions:
            continue
        days = (now - pd.Timestamp(item["opened"], tz="UTC").timestamp()) / 86400
        if days > REVIEW_AFTER_DAYS:
            rows.append({**item, "days": days})
    return rows


def show_strategies() -> None:
    """The strategies table for the notebooks."""
    from desk.hl.account_analytics import display_interactive_table

    frame = strategy_frame()
    if frame.empty:
        print("No HL positions or fills in the last 30 days.")
        return
    frame = pd.concat([frame, pd.DataFrame([total_row(frame)])], ignore_index=True)
    money = lambda value: "" if value != value else (f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}")
    count = lambda value: "" if value != value else f"{value:,.0f}"
    display_interactive_table(frame, bold_last=True, title="PERFORMANCE BY STRATEGY (HL)",
                              formatters={"Unrealized PnL": money, "Realized PnL (30d)": money, "Fees (30d)": money,
                                          "Funding (30d)": money, "Volume (30d)": money, "Open positions": count, "Fills (30d)": count},
                              key_columns={"Realized PnL (30d)": "abs"})
    print("Realized PnL is net of fees; funding (received positive, paid negative) is separate and goes to the strategy that held the position when it was paid. A position's unrealized PnL counts under its own strategy; the ladders' "
          "fills count under market making. Fills before a position was journalled are untagged.")
