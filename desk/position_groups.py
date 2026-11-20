"""Open positions by strategy: the label each position carries, so like is compared with like.

A position's strategy is the journal's tag for it (the daemon's research feed carries it as ``theses``: coin, side,
strategy, per open trade). A coin with a neutral market-making ladder and no tag is ``market making``; anything else is
``untagged`` (never guessed). ``annotate`` puts the label, and each position's open risk as a share of the desk's risk
cap, on the positions tables' rows; ``group_totals`` is the per-strategy subtotal the page shows and that
``desk.reconcile.strategy_groups`` ties out against the table's own total.

The front end (desk/dashboard_static/app.js, ``rowStrategy``) reads the same label from ``theses`` when a row does not
carry one, so the local dashboard and the web build group identically.
"""

from __future__ import annotations

import math

MARKET_MAKING = "market making"
UNTAGGED = "untagged"
ORDER = ("directional", "fade", "conviction", "hedge", "momentum", MARKET_MAKING, UNTAGGED)   # how groups are listed
TABLES = ("positions", "positions_stocks")
RISK_TTL_SECONDS = 600
_risk_cache: dict = {"at": 0.0, "value": {}}


def bare(symbol) -> str:
    """The market's own name: ``xyz:AMD`` -> ``AMD``."""
    return str(symbol or "").split(":")[-1].strip().upper()


def side_of(row: dict) -> str:
    side = str(row.get("Side") or "").upper()
    if side in ("LONG", "SHORT"):
        return side
    value = row.get("Value")
    return "SHORT" if isinstance(value, (int, float)) and value < 0 else "LONG"


def strategy_of(theses: list[dict] | None, symbol, side: str, ladder_coins=()) -> str:
    """The journal's strategy for this coin and side, else market making when a neutral ladder runs on it, else untagged."""
    name = bare(symbol)
    for item in theses or []:
        if bare(item.get("coin")) == name and str(item.get("side") or "").upper() == str(side).upper() and item.get("strategy"):
            return str(item["strategy"])
    return MARKET_MAKING if name in {bare(coin) for coin in ladder_coins} else UNTAGGED


_theses_cache: dict = {"at": 0.0, "value": []}


def current_theses(research: dict | None, now: float | None = None) -> list[dict]:
    """The journal's open trades (coin, side, strategy). This machine's research feed carries them only while the daemon
    runs here; on the Mac the file is days old, so the live feed is read from the relay (cached ten minutes). Nothing
    found: an empty list, and every position reads ``untagged`` rather than a guess."""
    import time

    theses = (research or {}).get("theses") if isinstance(research, dict) else None
    if theses:
        return list(theses)
    now = time.time() if now is None else now
    if now - _theses_cache["at"] < RISK_TTL_SECONDS:
        return list(_theses_cache["value"])
    from desk.web_sync import relay_get

    value = list((relay_get("/research") or {}).get("theses") or [])
    _theses_cache.update(at=now, value=value)
    return value


def risk_key(account, symbol) -> tuple[str, str]:
    """(account's last four, market) as the risk map is keyed: ``HL 07f2`` + ``xyz:AMD`` -> (``07f2``, ``AMD``)."""
    return str(account or "").split()[-1][-4:].lower(), bare(symbol)


def annotate(tables: dict, theses: list[dict] | None, ladder_coins=(), risks: dict | None = None, cap: float | None = None) -> None:
    """Add Strategy (and Risk: the position's open risk over the desk's risk cap) to the positions tables, in place."""
    for name in TABLES:
        spec = tables.get(name) or {}
        rows = spec.get("rows") or []
        if not rows:
            continue
        keys = {column["key"] for column in spec.get("columns", [])}
        for row in rows:
            row["Strategy"] = strategy_of(theses, row.get("Symbol"), side_of(row), ladder_coins)
            if risks is not None and cap:
                risk = risks.get(risk_key(row.get("Account"), row.get("Symbol")))
                row["Risk"] = risk / cap if isinstance(risk, (int, float)) and math.isfinite(risk) else None
        extra = [{"key": "Strategy", "fmt": "text"}] + ([{"key": "Risk", "fmt": "rpct"}] if risks is not None and cap else [])
        spec["columns"] = list(spec.get("columns", [])) + [column for column in extra if column["key"] not in keys]
        if spec.get("total") is not None and risks is not None and cap:
            spec["total"]["Risk"] = sum(row["Risk"] for row in rows if isinstance(row.get("Risk"), (int, float))) or None


def group_totals(rows: list[dict]) -> dict[str, dict]:
    """Per strategy: positions, signed value, uPnL, share of gross weight and share of the risk cap."""
    groups: dict[str, dict] = {}
    for row in rows:
        group = groups.setdefault(str(row.get("Strategy") or UNTAGGED), {"positions": 0, "Value": 0.0, "uPnL": 0.0, "Weight": 0.0, "Risk": 0.0})
        group["positions"] += 1
        for key in ("Value", "uPnL", "Weight", "Risk"):
            if isinstance(row.get(key), (int, float)) and math.isfinite(row[key]):
                group[key] += row[key]
    return dict(sorted(groups.items(), key=lambda item: (ORDER.index(item[0]) if item[0] in ORDER else len(ORDER), item[0])))


def position_risks(overview, now: float | None = None) -> dict:
    """Each position's open risk in dollars as the executor counts it, keyed by ``risk_key``: HL from the executor's own
    per-position split (cached ten minutes: it reads candles and funding), IB from the book entry's position risks."""
    import time

    now = time.time() if now is None else now
    if now - _risk_cache["at"] < RISK_TTL_SECONDS and _risk_cache["value"]:
        return dict(_risk_cache["value"])
    from desk.risk import hl_position_risks

    out: dict = {}
    wallets = [snapshot.account for snapshot in overview.snapshots if snapshot.venue == "hl"]
    for item in hl_position_risks(wallets) if wallets else []:
        out[(str(item["wallet"])[-4:].lower(), bare(item["coin"]))] = float(item["risk"])
    for entry in overview.book:
        if entry.venue != "ib":
            continue
        for symbol, risk, _ in entry.position_risks:
            out[(str(entry.label).split()[-1][-4:].lower(), bare(symbol))] = float(risk)
    _risk_cache.update(at=now, value=out)
    return dict(out)
