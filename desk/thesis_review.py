"""Thesis review of the stopped-out and still-held stock positions (IB and Hyperliquid equity perps).

Read-only. A loss on a position is first a question about its thesis, not a reason to act (memory trade-management). This
module lines up, per position: the thesis as far as the repo and the user recorded it, what the exit realised, what the
price did after the exit, and a mechanical test of what can be tested (relative strength against SPY on the thesis side,
SPY's own regime). A thesis part that price cannot test (ads, compute, distribution, product rot) is said to be untestable.

    python -m desk.thesis_review run        # fetch, write research/thesis_review_2026-11.md and the chart, publish the tile

Status words: intact (every price test is on the thesis side), weakened (one is against), broken (both are against and the
price is at its 60-day extreme against the thesis), cannot test (no price history).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "research" / "thesis_review_2026-11.md"
CHART = REPO / "research" / "thesis_review_2026-11.png"
CLOSED = REPO / "data" / "closed_trades_stocks.csv"
SINCE = "2026-09-01"
EXCLUDED = {"KYIV", "SWMR"}   # symbolic holdings (memory ukraine-symbolic-holdings): no thesis calls
DEXES = ("xyz", "io", "para", "hyna", "flx", "vntl", "km", "abcd", "cash", "mkts")

# What is known about each thesis, and where it comes from. The repo's journal holds crypto entries only, so a stock
# thesis is the user's stated frame (memory desk-trading-thesis, 2026-11-13..15) or their statement of 2026-11-19.
THESES = {
    "GOOGL": ("Tech long: ads and compute, big tech has both", "frame 2026-11-13; user: still in thesis 2026-11-19", 1),
    "NET": ("High-beta tech long", "user 2026-11-19: still in thesis; no rationale recorded", 1),
    "NVDA": ("Compute: hardware presented appealingly", "frame 2026-11-13", 1),
    "META": ("Tech long: ads", "frame (ads bucket); no entry note", 1),
    "MU": ("Tech long: compute (memory)", "frame (compute bucket); no entry note", 1),
    "INTC": ("Compute: invisible supplier, conviction long", "frame 2026-11-13", 1),
    "AMD": ("Compute: invisible supplier, conviction long", "frame 2026-11-13", 1),
    "MSFT": ("Complacency short: monopoly, poor product, outages", "frame 2026-11-13", -1),
    "SP500": ("Index exposure (c248)", "no note recorded", 1),
    "ANTH": ("AI app, pre-IPO", "frame 2026-11-15 doubts unprofitable AI names; no entry note", 1),
    "AAPL": ("Tech long", "no entry note", 1),
}
UNTESTABLE = "ads, compute, distribution and product quality are not visible in price"

YAHOO = {"NET": "NET", "GOOGL": "GOOGL", "NVDA": "NVDA", "META": "META", "MU": "MU", "INTC": "INTC", "AMD": "AMD",
         "MSFT": "MSFT", "AAPL": "AAPL", "IONQ": "IONQ", "SOFI": "SOFI", "RIVN": "RIVN", "RELIANCE": "RELIANCE.NS"}


# ------------------------------------------------------------------------------------------------ pure helpers
def base(coin: str) -> str:
    return str(coin).split(":")[-1].upper()


def dex_of(coin: str) -> str:
    return str(coin).split(":")[0] if ":" in str(coin) else ""


def episodes(closed: pd.DataFrame, since: str = SINCE) -> pd.DataFrame:
    """One row per position (account, market, side, opened): size, average entry and exit, exit time, PnL, move on entry."""
    d = closed[pd.to_datetime(closed["time"], utc=True, format="ISO8601") >= pd.Timestamp(since, tz="UTC")].copy()
    if d.empty:
        return pd.DataFrame()
    d["time"] = pd.to_datetime(d["time"], utc=True, format="ISO8601")
    d["opened"] = pd.to_datetime(d["opened"], utc=True, format="ISO8601")
    d["account_key"] = d["venue"].astype(str) + " " + d["account"].astype(str)
    rows = []
    for (account, coin, side, opened), part in d.groupby(["account_key", "coin", "side", "opened"]):
        weight = part["size"].astype(float)
        direction = 1 if side == "LONG" else -1
        entry = float(np.average(part["entry"], weights=weight))
        exit_ = float(np.average(part["exit"], weights=weight))
        rows.append({"account": account, "coin": coin, "name": base(coin), "side": side, "dir": direction, "opened": opened,
                     "exit_time": part["time"].max(), "size": float(weight.sum()), "entry": entry, "exit": exit_,
                     "pnl": float(part["pnl"].sum()), "move": (exit_ / entry - 1) * direction})
    return pd.DataFrame(rows).sort_values("exit_time").reset_index(drop=True)


def path_after(prices: pd.Series, exit_time: pd.Timestamp, entry: float, direction: int) -> pd.Series:
    """The side-adjusted move on the ENTRY price over the days after the exit: the hold-through path (0 is break-even)."""
    after = prices[prices.index >= exit_time.normalize()]
    return (after / entry - 1) * direction


def hold_stats(prices: pd.Series, exit_time: pd.Timestamp, exit_price: float, direction: int) -> dict:
    """What holding from the exit would have meant: move to the latest close, the worst and best on the way, and the move
    5 and 10 days on (side-adjusted, so positive means the price went the thesis's way after the exit)."""
    after = prices[prices.index > exit_time.normalize()]
    if after.empty:
        return {}
    move = (after / exit_price - 1) * direction
    out = {"since_exit": float(move.iloc[-1]), "worst": float(move.min()), "best": float(move.max()), "days": int(len(after)),
           "as_of": after.index[-1]}
    for n in (5, 10):
        out[f"d{n}"] = float(move.iloc[n - 1]) if len(move) >= n else np.nan
    return out


def relative_strength(prices: pd.Series, spy: pd.Series, days: int, end: pd.Timestamp | None = None) -> float:
    """Return of the name less SPY's over the calendar window ending at ``end`` (as-of prices, so a weekend perp lines up)."""
    end = end or min(prices.index.max(), spy.index.max())
    start = end - pd.Timedelta(days=days)

    def asof(series, when):
        before = series[series.index <= when]
        return float(before.iloc[-1]) if len(before) else np.nan

    own = asof(prices, end) / asof(prices, start) - 1
    market = asof(spy, end) / asof(spy, start) - 1
    return float(own - market)


NEUTRAL = 0.02   # a relative strength within 2 points of SPY is neither for nor against the thesis


def classify(direction: int, rs20: float, rs60: float, at_extreme: bool) -> str:
    """intact: neither relative-strength test (20 and 60 days against SPY) is against the thesis side; weakened: one is
    against; broken: both are against and the price sits at its 60-day extreme against the thesis; cannot test: no
    price history. Within 2 points of SPY counts as neutral, so a coin-flip does not flip the status."""
    if not (np.isfinite(rs20) and np.isfinite(rs60)):
        return "cannot test"
    against = [direction * rs20 < -NEUTRAL, direction * rs60 < -NEUTRAL]
    if not any(against):
        return "intact"
    if all(against) and at_extreme:
        return "broken"
    return "weakened"


def at_extreme_against(prices: pd.Series, direction: int, days: int = 60) -> bool:
    """A long's price at its 60-day low, a short's at its 60-day high (within 2%)."""
    window = prices[prices.index >= prices.index.max() - pd.Timedelta(days=days)]
    if window.empty:
        return False
    last = float(window.iloc[-1])
    return last <= float(window.min()) * 1.02 if direction > 0 else last >= float(window.max()) / 1.02


def t_stat(values) -> tuple[float, float, int]:
    values = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
    n = len(values)
    if n < 3 or values.std(ddof=1) == 0:
        return float(values.mean()) if n else np.nan, np.nan, n
    return float(values.mean()), float(values.mean() / (values.std(ddof=1) / np.sqrt(n))), n


# ------------------------------------------------------------------------------------------------ data
def hl_closes(coin: str, days: int = 150) -> pd.Series:
    from desk.hl.trade_analytics import fetch_daily_candles

    frame = fetch_daily_candles(base(coin), dex_of(coin), lookback_days=days)
    series = pd.Series(frame["close"].astype(float).values, index=pd.DatetimeIndex(pd.to_datetime(frame["time"], utc=True)).normalize())
    return series[~series.index.duplicated(keep="last")].sort_index()


def yahoo(name: str, days: int = 150) -> pd.Series:
    from desk.live_risk import yahoo_stock_closes

    series = yahoo_stock_closes(YAHOO[name], days)
    series.index = pd.DatetimeIndex(series.index).normalize()
    return series[~series.index.duplicated(keep="last")].sort_index()


def prices_for(coin: str, account: str) -> tuple[pd.Series | None, str]:
    """The closes of the instrument traded: HL candles for an HL market (weekend prices too), Yahoo for an IB stock."""
    name = base(coin)
    try:
        if account.startswith("HL"):
            return hl_closes(coin), "HL"
        return yahoo(name), "Yahoo"
    except Exception:
        try:
            return yahoo(name), "Yahoo"
        except Exception:
            return None, ""


def verified_stops(since: str = SINCE) -> pd.DataFrame:
    """Filled reduce-only stop orders on both HL wallets (desk/risk_replay.stop_fills): the verified stop exits."""
    from desk.accounts import accounts_for, load_accounts
    from desk.hl.trade_analytics import post_info
    from desk.risk_replay import stop_fills

    rows = []
    for account in accounts_for("hl", load_accounts()):
        try:
            fills = post_info({"type": "userFills", "user": account.id})
            orders = post_info({"type": "historicalOrders", "user": account.id})
        except Exception:
            continue
        for key, stamp, side, price, size in stop_fills(fills, orders):
            rows.append({"wallet": account.id[-4:], "coin": key[1], "time": pd.to_datetime(stamp, unit="ms", utc=True),
                         "price": price, "size": size})
    return pd.DataFrame(rows)


def held_positions() -> list[dict]:
    """Open stock positions: IB from the relay's last read, HL equity perps from the exchange."""
    out = []
    from desk.accounts import accounts_for, load_accounts
    from desk.hl.trade_analytics import post_info

    for account in accounts_for("hl", load_accounts()):
        for dex in DEXES:
            try:
                state = post_info({"type": "clearinghouseState", "user": account.id, "dex": dex})
            except Exception:
                continue
            for item in state.get("assetPositions", []):
                p = item["position"]
                size = float(p["szi"])
                if dex and size:
                    out.append({"account": f"HL {account.id[-4:]}", "coin": p["coin"], "name": base(p["coin"]), "side": "LONG" if size > 0 else "SHORT",
                                "dir": 1 if size > 0 else -1, "size": abs(size), "entry": float(p["entryPx"]),
                                "mark": abs(float(p["positionValue"]) / size), "value": abs(float(p["positionValue"]))})
    return out


def ib_positions() -> list[dict]:
    """IB's open stocks from the desk server's relay copy (the Mac has no TWS session in a research run)."""
    import subprocess

    try:
        data = json.loads(subprocess.check_output(["ssh", "desk", "cat ~/.trading-desk/relay/ib.json"], timeout=60))
    except Exception:
        data = {}
    out = []
    for p in data.get("positions", []):
        if p.get("asset_class") != "equity":
            continue
        out.append({"account": "IB 8440", "coin": p["symbol"], "name": base(p["symbol"]), "side": "LONG" if p["quantity"] > 0 else "SHORT",
                    "dir": 1 if p["quantity"] > 0 else -1, "size": abs(p["quantity"]), "entry": float(p["avg_price"]),
                    "mark": float(p["mark_price"]), "value": abs(p["quantity"] * p["mark_price"])})
    return out


# ------------------------------------------------------------------------------------------------ the review
def spy_regime(spy: pd.Series) -> dict:
    last = float(spy.iloc[-1])
    high = float(spy.max())
    window = spy[spy.index >= spy.index.max() - pd.Timedelta(days=250)]
    return {"as_of": spy.index[-1], "last": last, "from_high": last / float(window.max()) - 1,
            "above_50d": last > float(spy.iloc[-50:].mean()), "ret20": last / float(spy.iloc[-21]) - 1,
            "ret60": last / float(spy.iloc[-61]) - 1, "high_date": window.idxmax()}


def beta_to(prices: pd.Series, spy: pd.Series, days: int = 90) -> float:
    joined = pd.concat([prices.rename("own"), spy.rename("spy")], axis=1, sort=True).dropna()
    joined = joined[joined.index >= joined.index.max() - pd.Timedelta(days=days + 10)]
    returns = joined.pct_change().dropna()
    if len(returns) < 20 or returns["spy"].var() == 0:
        return float("nan")
    return float(returns["own"].cov(returns["spy"]) / returns["spy"].var())


def funding_apr(coin: str, side: int, days: int = 14) -> float:
    """The funding the position earns (positive) or pays (negative), annualised, over the last two weeks."""
    from desk.hl.trade_analytics import fetch_funding_history

    frame = fetch_funding_history(base(coin), dex_of(coin), lookback_days=days, allow_empty=True)
    if frame is None or frame.empty:
        return float("nan")
    return float(-side * frame["fundingRate"].astype(float).mean() * 24 * 365)


def ib_whipsaw(path: Path) -> pd.DataFrame:
    """IB sells and buys of the three tech names from 2026-11-09, side by side: what the exits sold at, what came back in."""
    fills = pd.read_csv(path, parse_dates=["time"])
    fills = fills[(fills["time"] >= "2026-11-09") & fills["symbol"].isin(["NET", "GOOGL", "NVDA"])]
    rows = []
    for symbol, part in fills.groupby("symbol"):
        for day, group in part.groupby(part["time"].dt.strftime("%Y-%m-%d")):
            for side, trades in group.groupby("side"):
                rows.append({"symbol": symbol, "day": day, "side": side, "shares": float(trades["quantity"].sum()),
                             "avg_price": float(np.average(trades["price"], weights=trades["quantity"]))})
    return pd.DataFrame(rows)


def build(closed_path: Path = CLOSED, fills_path: Path | None = None) -> dict:
    closed = pd.read_csv(closed_path)
    eps = episodes(closed)
    eps = eps[(eps["exit_time"] >= pd.Timestamp("2026-10-16", tz="UTC")) & (eps["pnl"] < 0) & (eps["move"] <= -0.01)
              & ~eps["name"].isin(EXCLUDED)].reset_index(drop=True)
    stops = verified_stops()
    spy = yahoo_spy()
    out = {"spy": spy_regime(spy), "spy_series": spy, "episodes": [], "held": [], "notes": []}
    cache: dict[str, tuple[pd.Series | None, str]] = {}

    def get(coin, account):
        key = f"{account[:2]}:{coin}"
        if key not in cache:
            cache[key] = prices_for(coin, account)
        return cache[key]

    for _, ep in eps.iterrows():
        prices, source = get(ep["coin"], ep["account"])
        verified = bool(len(stops)) and ep["account"].startswith("HL") and bool(
            ((stops["coin"] == ep["coin"]) & (stops["time"] <= ep["exit_time"] + pd.Timedelta(minutes=5))
             & (stops["time"] >= ep["opened"])).any())
        record = {**ep.to_dict(), "source": source, "verified_stop": verified}
        if prices is not None:
            record["prices"] = prices
            record["rs20"] = relative_strength(prices, spy, 20)
            record["rs60"] = relative_strength(prices, spy, 60)
            record["beta"] = beta_to(prices, spy)
            record["status"] = classify(int(ep["dir"]), record["rs20"], record["rs60"], at_extreme_against(prices, int(ep["dir"])))
            record.update(hold_stats(prices, ep["exit_time"], ep["exit"], int(ep["dir"])))
            if "since_exit" in record:   # what the exit saved (positive) or cost (negative) against holding to the latest close
                record["stop_effect"] = -record["since_exit"] * ep["size"] * ep["exit"]
            record["path"] = path_after(prices, ep["exit_time"], ep["entry"], int(ep["dir"]))
        out["episodes"].append(record)

    held = ib_positions() + held_positions()
    for position in held:
        if position["name"] in EXCLUDED:
            continue
        prices, source = get(position["coin"], position["account"])
        record = dict(position, source=source)
        if position["name"] == "SP500":   # the market itself: relative strength against SPY is nil by construction
            record["prices"] = prices
            record["status"] = "index: no relative test"
        elif prices is not None:
            record["prices"] = prices
            record["rs20"] = relative_strength(prices, spy, 20)
            record["rs60"] = relative_strength(prices, spy, 60)
            record["beta"] = beta_to(prices, spy)
            record["extreme"] = at_extreme_against(prices, position["dir"])
            record["status"] = classify(position["dir"], record["rs20"], record["rs60"], record["extreme"])
        else:
            record["status"] = "cannot test"
        if position["account"].startswith("HL"):
            try:
                record["funding"] = funding_apr(position["coin"], position["dir"])
            except Exception:
                record["funding"] = float("nan")
        record["move_on_entry"] = (position["mark"] / position["entry"] - 1) * position["dir"]
        out["held"].append(record)
    if fills_path is not None and Path(fills_path).exists():
        out["whipsaw"] = ib_whipsaw(Path(fills_path))
    return out


def yahoo_spy(days: int = 250) -> pd.Series:
    from desk.live_risk import yahoo_stock_closes

    series = yahoo_stock_closes("SPY", days)
    series.index = pd.DatetimeIndex(series.index).normalize()
    return series[~series.index.duplicated(keep="last")].sort_index()


def stop_statistics(episodes_: list[dict]) -> dict:
    """Did the exits sell dislocations? The side-adjusted move from each exit to the latest close (positive: the price
    went the thesis's way after the exit, so the exit cost money), over episodes and over names (a name stopped several
    times counts once, at its exits' average), with the first and second half by exit date. Episodes of one name overlap,
    so the name view is the one to read."""
    rows = pd.DataFrame([{k: e.get(k) for k in ("name", "exit_time", "since_exit", "d5", "stop_effect", "pnl", "verified_stop")}
                         for e in episodes_ if e.get("since_exit") is not None and np.isfinite(e.get("since_exit", np.nan))])
    if rows.empty:
        return {}
    rows = rows.sort_values("exit_time")
    by_name = rows.groupby("name").agg(since_exit=("since_exit", "mean"), exit_time=("exit_time", "min"),
                                       stop_effect=("stop_effect", "sum"), pnl=("pnl", "sum")).sort_values("exit_time")
    half = len(by_name) // 2
    return {"episodes": t_stat(rows["since_exit"]), "names": t_stat(by_name["since_exit"]),
            "names_first": t_stat(by_name["since_exit"].iloc[:half]), "names_second": t_stat(by_name["since_exit"].iloc[half:]),
            "d5_names": t_stat(rows.groupby("name")["d5"].mean()),
            "stop_effect": float(rows["stop_effect"].sum()), "realised": float(rows["pnl"].sum()),
            "helped": int((rows["since_exit"] < 0).sum()), "hurt": int((rows["since_exit"] > 0).sum()),
            "by_name": by_name.reset_index()}


def crypto_shorts() -> list[dict]:
    """Open crypto shorts against the desk's regime read (alt season: none supports shorts): each coin's move since entry
    and its 20-day return against BTC (a short works while the coin is weaker than BTC)."""
    from desk.accounts import accounts_for, load_accounts
    from desk.hl.trade_analytics import post_info

    btc = hl_closes("BTC")
    out = []
    for account in accounts_for("hl", load_accounts()):
        try:
            state = post_info({"type": "clearinghouseState", "user": account.id})
        except Exception:
            continue
        for item in state.get("assetPositions", []):
            p = item["position"]
            size = float(p["szi"])
            if size >= 0:
                continue
            mark = abs(float(p["positionValue"]) / size)
            try:
                coin = hl_closes(p["coin"])
                rs20 = relative_strength(coin, btc, 20)
            except Exception:
                rs20 = float("nan")
            out.append({"account": account.id[-4:], "coin": p["coin"], "entry": float(p["entryPx"]), "mark": mark,
                        "move": -(mark / float(p["entryPx"]) - 1), "value": abs(float(p["positionValue"])), "rs20_btc": rs20})
    return sorted(out, key=lambda row: -row["value"])


def regime_read(path: Path = REPO / "data" / "market_regime.json") -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {"equity": (data.get("equity_selloff") or {}), "alts": (data.get("alt_season") or {})}


# ------------------------------------------------------------------------------------------------ stop placements
def stop_orders() -> pd.DataFrame:
    """Every reduce-only stop order on the two HL wallets that the exchange still lists (about the last 2000 orders per
    wallet: 07f2's history starts 2026-11-02 17:19), with its trigger price and what became of it."""
    from desk.accounts import accounts_for, load_accounts
    from desk.hl.trade_analytics import post_info

    rows = []
    for account in accounts_for("hl", load_accounts()):
        try:
            events = post_info({"type": "historicalOrders", "user": account.id})
        except Exception:
            continue
        for event in events:
            order = event.get("order") or {}
            if order.get("orderType") in ("Stop Market", "Stop Limit") and order.get("reduceOnly") and order.get("triggerPx"):
                rows.append({"wallet": account.id[-4:], "coin": order["coin"], "trigger": float(order["triggerPx"]),
                             "status": event.get("status"), "time": pd.to_datetime(order["timestamp"], unit="ms", utc=True)})
    return pd.DataFrame(rows)


def mark_at(coin: str, when: pd.Timestamp) -> float:
    from desk.hl.trade_analytics import post_info

    stamp = int(when.timestamp() * 1000)
    rows = post_info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "5m", "startTime": stamp - 900_000, "endTime": stamp + 900_000}})
    if not rows:
        return float("nan")
    return float(min(rows, key=lambda row: abs(row["t"] - stamp))["o"])


def stop_placements(episodes_: list[dict], orders: pd.DataFrame) -> list[dict]:
    """For each HL episode that had stop orders: when they were placed, the price then, how far from it (against the position) the stops sat, and
    how much of the position's loss had already happened before the stops were placed."""
    out = []
    if orders is None or orders.empty:
        return out
    for e in episodes_:
        if not e["account"].startswith("HL"):
            continue
        mine = orders[(orders["coin"] == e["coin"]) & (orders["time"] >= e["opened"]) & (orders["time"] <= e["exit_time"])
                      & (orders["status"] != "canceled")]   # "canceled" orders were replaced by the rungs that follow
        if mine.empty:
            continue
        placed = mine["time"].min()
        batch = mine[mine["time"] <= placed + pd.Timedelta(seconds=180)]
        mark = mark_at(e["coin"], placed)
        if not np.isfinite(mark):
            continue
        levels = sorted(batch["trigger"].unique(), key=lambda level: abs(level / mark - 1))
        out.append({"coin": e["coin"], "name": e["name"], "side": e["side"], "placed": placed, "mark": mark, "entry": e["entry"],
                    "lost_before": (mark / e["entry"] - 1) * e["dir"], "days_held": (placed - e["opened"]).total_seconds() / 86400, "levels": levels,
                    "from_mark": [-(level / mark - 1) * e["dir"] for level in levels]})   # how far the stop sat against the position
    return out


# ------------------------------------------------------------------------------------------------ names table, report, chart, tile
def pct(value, digits: int = 1, signed: bool = True) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    text = f"{abs(value) * 100:.{digits}f}%"
    return ("-" if value < 0 else "+" if signed and value > 0 else "") + text


def usd(value) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return ("-" if value < 0 else "") + f"${abs(value):,.0f}"


def beta_text(value) -> str:
    return f"{value:.1f}" if value is not None and np.isfinite(value) else "n/a"


def thesis_of(name: str) -> tuple[str, str]:
    text, source, _ = THESES.get(name, ("No thesis recorded in the repo", "none", 0))
    return text, source


def names_table(review: dict, spy: pd.Series) -> pd.DataFrame:
    """One row per name, stopped or held: thesis, what the stops realised, the move since the exits, and the status."""
    rows = {}
    for e in review["episodes"]:
        row = rows.setdefault((e["name"], e["side"]), {"name": e["name"], "side": e["side"], "dir": e["dir"], "held": False,
                                                      "episodes": [], "prices": None})
        row["episodes"].append(e)
        for key in ("status", "rs20", "rs60", "beta"):   # the price tests as of the latest data, on the thesis side
            if e.get(key) is not None:
                row.setdefault("closed_" + key, e[key])
    for h in review["held"]:
        row = rows.setdefault((h["name"], h["side"]), {"name": h["name"], "side": h["side"], "dir": h["dir"], "held": False,
                                                      "episodes": [], "prices": None})
        row.update(held=True, side=h["side"], dir=h["dir"], status=h.get("status"), rs20=h.get("rs20"), rs60=h.get("rs60"),
                   beta=h.get("beta"), funding=h.get("funding"), move_on_entry=h.get("move_on_entry"), value=h.get("value"))
    table = []
    for (name, _), row in rows.items():
        eps_ = row["episodes"]
        weight = sum(e["size"] * e["entry"] for e in eps_)
        realised = sum(e["move"] * e["size"] * e["entry"] for e in eps_) / weight if weight else np.nan
        since = [e["since_exit"] for e in eps_ if e.get("since_exit") is not None]
        text, source = thesis_of(name)
        table.append({"name": name, "side": row["side"], "held": row["held"], "thesis": text, "source": source,
                      "stop_move": realised, "stop_pnl": sum(e["pnl"] for e in eps_) if eps_ else np.nan,
                      "since_exit": float(np.mean(since)) if since else np.nan,
                      "stop_effect": sum(e.get("stop_effect", 0.0) for e in eps_) if eps_ else np.nan,
                      "verified": any(e["verified_stop"] for e in eps_), "status": row.get("status", row.get("closed_status", "closed")),
                      "rs20": row.get("rs20", row.get("closed_rs20", np.nan)), "rs60": row.get("rs60", row.get("closed_rs60", np.nan)),
                      "beta": row.get("beta", row.get("closed_beta", np.nan)),
                      "funding": row.get("funding", np.nan), "move_on_entry": row.get("move_on_entry", np.nan)})
    frame = pd.DataFrame(table)
    order = {"intact": 0, "weakened": 1, "broken": 2, "cannot test": 3}
    frame["_o"] = frame["status"].map(lambda s: order.get(s, 4))
    return frame.sort_values(["held", "_o", "name"], ascending=[False, True, True]).drop(columns="_o").reset_index(drop=True)


def tile_frame(names: pd.DataFrame) -> pd.DataFrame:
    """The dashboard tile: position, thesis, stop realised, since exit, status. Absent is absent: no zeros, no n/a."""
    frame = pd.DataFrame({
        "Position": [f"{r['name']} {r['side'].lower()}" for _, r in names.iterrows()],
        "Thesis": [r["thesis"] for _, r in names.iterrows()],
        "Stop realised": [r["stop_move"] if np.isfinite(r["stop_move"]) else None for _, r in names.iterrows()],
        "Since exit": [r["since_exit"] if np.isfinite(r["since_exit"]) else None for _, r in names.iterrows()],
        "Status": [r["status"] if r["held"] else ("closed, " + r["status"] if r["status"] not in ("closed", None) else "closed") for _, r in names.iterrows()]})
    return frame


def make_chart(review: dict, path: Path = CHART, minimum: float = 20.0) -> Path:
    """Realised stop against the hold-through path: for each stopped position of $20 or more, the price path after the exit
    as a move on the entry price (side-adjusted); the dashed line is what the exit realised."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    picks = [e for e in review["episodes"] if abs(e["pnl"]) >= minimum and e.get("path") is not None and len(e["path"]) > 1]
    cols = 3
    rows = max(1, int(np.ceil(len(picks) / cols)))
    fig, axes = plt.subplots(rows, cols, figsize=(11, 2.5 * rows), squeeze=False, sharey=False)
    for ax in axes.ravel():
        ax.axis("off")
    for ax, e in zip(axes.ravel(), picks):
        ax.axis("on")
        path_ = e["path"] * 100
        days = (path_.index - e["exit_time"].normalize()).days
        color = "#c62828" if path_.iloc[-1] > e["move"] * 100 else "#1a7f37"   # red: the exit cost money, green: it saved money
        ax.plot(days, path_.values, color=color, lw=1.8)
        ax.axhline(e["move"] * 100, color="#444", ls="--", lw=1)
        ax.axhline(0, color="#999", lw=0.8)
        ax.set_title(f"{e['name']} {e['side'].lower()} ({e['account'].split()[0]})  stop {e['move'] * 100:+.0f}%", fontsize=9)
        ax.tick_params(labelsize=7)
        ax.set_xlabel("days after the exit", fontsize=7)
        ax.set_ylabel("% on entry", fontsize=7)
    fig.suptitle("Realised stop (dashed) against holding on: price path after each exit, as a move on the entry price\n"
                 "above the dashed line the exit cost money, below it the exit saved money", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def by_side(episodes_: list[dict]) -> dict:
    out = {}
    for side in ("LONG", "SHORT"):
        mine = sorted([e for e in episodes_ if e["side"] == side and e.get("since_exit") is not None and np.isfinite(e["since_exit"])],
                      key=lambda e: e["exit_time"])
        frame = pd.DataFrame([{"name": e["name"], "v": e["since_exit"], "t": e["exit_time"]} for e in mine])
        names_ = frame.groupby("name").agg(v=("v", "mean"), t=("t", "min")).sort_values("t")["v"] if mine else pd.Series(dtype=float)
        half = len(names_) // 2
        out[side] = {"names": t_stat(names_), "first": t_stat(names_.iloc[:half]), "second": t_stat(names_.iloc[half:]),
                     "effect": sum(e.get("stop_effect", 0.0) for e in mine), "pnl": sum(e["pnl"] for e in mine)}
    return out


def render_report(review: dict, stats: dict, names: pd.DataFrame, placements: list[dict], regime: dict, crypto: list[dict],
                  whipsaw: pd.DataFrame | None, today: str) -> str:
    spy = review["spy"]
    eq = regime.get("equity", {})
    side = by_side(review["episodes"])
    out = []
    w = out.append
    w(f"# Thesis review, {today}\n")
    w("Read-only: no order was sent and no setting changed. Thesis first: a loss on a position is a question about its thesis, not a reason to act.\n")
    w("## What the data says in short\n")
    nm, nt, nn = stats["names"]
    w("- **GOOGL's and NET's stops went on late and tight.** On Hyperliquid their stop ladders (two or three rungs) were placed on 2026-11-02 about "
      "3 to 8% from the price of the day, on positions that had already lost 18 to 20% from entry. The stops then realised that accumulated loss; they did "
      "not cause it (table 1). The shorts (SHAZ, QNT, RELIANCE) had theirs from entry, 2 to 14% away. IB's order types are not recorded, so its exits "
      "cannot be classed as stops.")
    w(f"- **Whether the exits sold a dislocation is not established.** Over {nn} stopped names the price moved the thesis's way after the exit by "
      f"{pct(nm)} on average (t {nt:.1f}); first half {pct(stats['names_first'][0])} (t {stats['names_first'][1]:.1f}), second half "
      f"{pct(stats['names_second'][0])} (t {stats['names_second'][1]:.1f}). That is under t of 2 and not in both halves, so it is a hypothesis, not a finding. "
      f"Holding every exit to the latest close would have been {usd(stats['stop_effect'] * -1)} better than the {usd(stats['realised'])} realised, "
      f"but the result is driven by the shorts. Prices are daily closes to {review['spy_series'].index[-1]:%Y-%m-%d}.")
    market = float(review["spy_series"].iloc[-1] / review["spy_series"][review["spy_series"].index >= pd.Timestamp("2026-11-04", tz="UTC")].iloc[0] - 1)
    w(f"- **Longs and shorts differ, and the market explains most of it.** Stopped longs: {pct(side['LONG']['names'][0])} after the exit "
      f"(t {side['LONG']['names'][1]:.1f}, n {side['LONG']['names'][2]}); stopped shorts: {pct(side['SHORT']['names'][0])} (t {side['SHORT']['names'][1]:.1f}, "
      f"n {side['SHORT']['names'][2]}; halves {pct(side['SHORT']['first'][0])} t {side['SHORT']['first'][1]:.1f} and {pct(side['SHORT']['second'][0])} t {side['SHORT']['second'][1]:.1f}). "
      f"The shorts were stopped before the sell-off ran (SPY {pct(market)} since 2026-11-04) and the longs before it went on falling, so one market move "
      f"stands behind both: the names are not independent and the short result is not established either. GOOGL and NVDA kept falling after the exit "
      f"(the stops helped); NET recovered (they cost).")
    w(f"- **Held positions:** none is intact on the price tests except NET; the rest are weakened, none broken. Market regime: SPY is "
      f"{abs(spy['from_high']) * 100:.1f}% below its 250-day high, below its 50-day average, and the desk's equity sell-off read is "
      f"{eq.get('level', 'n/a')} ({sum(1 for v in (eq.get('signals') or {}).values() if v)}/4 signals, evidence {eq.get('evidence', {}).get('days', 0)} days only).\n")

    w("## 1. What the stops did (Hyperliquid, from the exchange's order history)\n")
    w("07f2's order history starts 2026-11-02 17:19, so only stops from then on can be seen. The mark is the 5-minute open when the first stop was placed; "
      "the stop distance is how far the stop sat against the position.\n")
    w("| Position | Placed (UTC) | Days held before | Mark then | Entry | Already lost before | Stop levels against the position |")
    w("|---|---|---|---|---|---|---|")
    for p in placements:
        w(f"| {p['name']} {p['side'].lower()} | {p['placed']:%m-%d %H:%M} | {p['days_held']:.0f} | {p['mark']:,.2f} | {p['entry']:,.2f} | {pct(p['lost_before'])} | "
          f"{', '.join(pct(x, 1, False) for x in p['from_mark'])} |")
    already = [f"{p['name']} had lost {abs(p['lost_before']) * 100:.0f}% over {p['days_held']:.0f} days" for p in placements
               if p["name"] in ("GOOGL", "NET")]
    w(f"\n{' and '.join(already)} when their stops went on. Whatever the stops did after that, the thesis question belongs to the days "
      "before them: most of the loss was already there.\n")

    w("## 2. Position by position\n")
    w("Status uses only what price can test: the name's return less SPY's over 20 and 60 days, on the thesis side (within 2 points of SPY counts as "
      "neutral). **intact**: neither test is against; **weakened**: one is; **broken**: both are and the price is at its 60-day extreme against the "
      f"thesis. What price cannot test: {UNTESTABLE}. Source of each thesis in the last column; the journal holds crypto entries only.\n")
    w("| Name | Side | Held | Thesis | Stop realised | $ | Since exit | Exit saved/cost | Status | RS 20d | RS 60d | Beta | Source |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for _, r in names.iterrows():
        w(f"| {r['name']} | {r['side'].lower()} | {'yes' if r['held'] else 'no'} | {r['thesis']} | {pct(r['stop_move'])} | {usd(r['stop_pnl'])} | "
          f"{pct(r['since_exit'])} | {usd(r['stop_effect'])} | {r['status']} | {pct(r['rs20'])} | {pct(r['rs60'])} | "
          f"{beta_text(r['beta'])} | {r['source']}{' (verified stop)' if r['verified'] else ''} |")
    w("\n*Since exit* is the side-adjusted move from the exit price to the latest close: positive means the price went the thesis's way after the exit. "
      "*Exit saved/cost* is that move in dollars on the exited size, negative when the exit cost money. KYIV and SWMR are symbolic holdings and left out.\n")
    held = names[names["held"]]
    w("What would change each held status: the 20 or 60-day relative strength crossing 2 points of SPY on the other side; broken needs the price at "
      "its 60-day low (long) or high (short) with both against. A thesis's own claims (ads, compute, distribution, outages) need evidence price does not give.\n")
    for _, r in held.iterrows():
        extras = []
        if np.isfinite(r["funding"]):
            extras.append(f"funding {pct(r['funding'])} a year to the position")
        if np.isfinite(r["move_on_entry"]):
            extras.append(f"{pct(r['move_on_entry'])} on entry")
        w(f"- **{r['name']} {r['side'].lower()}** ({r['status']}): {', '.join(extras) if extras else 'no further data'}.")
    w("")

    w("## 3. What holding would have meant\n")
    w("| Name | Exits | Realised move on entry | Since exit | Worst after | Best after | 5 days on | $ saved/cost |")
    w("|---|---|---|---|---|---|---|---|")
    for e in sorted(review["episodes"], key=lambda e: e["exit_time"]):
        w(f"| {e['name']} {e['side'].lower()} ({e['account'].split()[0]}) | {e['exit_time']:%m-%d} | {pct(e['move'])} | {pct(e.get('since_exit'))} | "
          f"{pct(e.get('worst'))} | {pct(e.get('best'))} | {pct(e.get('d5'))} | {usd(e.get('stop_effect'))} |")
    w(f"\nOver names: mean {pct(nm)} (t {nt:.1f}, n {nn}); five-day {pct(stats['d5_names'][0])} (t {stats['d5_names'][1]:.1f}). "
      "Episodes of one name overlap and exits cluster on three days, so the name view is the fair one and even it is small. Survivorship runs both "
      "ways: names the desk never stopped are not here. **Not established** by the desk's rule (t of 2 in both halves).\n")
    w("![Realised stop against holding on](thesis_review_2026-11.png)\n")

    if whipsaw is not None and len(whipsaw):
        w("## 4. IB, 2026-11-09 and after\n")
        w("IB's order types are not recorded, so these are fills, not classed stops. The exits came at the open and pre-market of the 9th; part of each "
          "name came back later the same day.\n")
        w("| Name | Day | Side | Shares | Average price |")
        w("|---|---|---|---|---|")
        for _, r in whipsaw.sort_values(["symbol", "day", "side"]).iterrows():
            w(f"| {r['symbol']} | {r['day']} | {r['side'].lower()} | {r['shares']:g} | {r['avg_price']:,.2f} |")
        w("\nNET: six shares sold at the open region for 287.97 on average, two bought back the same day at 297.24 (3% higher), one more on the 17th at 289. "
          "GOOGL: two sold at 292.39, one back at 290.83, two sold again on the 17th at 281.62. NVDA: two sold at 177.66, two back at 177.02, two sold again "
          "on the 17th at 160.70. The 17th's sales are after the last closed-trade statement and are not in the tables above.\n")

    w("## 5. Shorts held and the regime read\n")
    w(f"Desk regime read ({today}): equity sell-off **{eq.get('level', 'n/a')}**, {eq.get('detail', '')}; alt season **{regime.get('alts', {}).get('level', 'n/a')}** "
      f"({regime.get('alts', {}).get('detail', '')}). A short is with the regime when the sell-off is on; whether the name itself is weaker than the market is the test.\n")
    w("- **MSFT short (stocks):** with the equity sell-off read, but MSFT is 6 points stronger than SPY over 60 days and level over 20, so the "
      "complacency thesis is not showing in price yet. Status weakened. The thesis's own claim (product rot, outages) cannot be tested from price.")
    w("- **Crypto shorts** (20-day return against BTC; a short works while the coin is weaker):\n")
    w("| Coin | Wallet | Value | Move for the short | 20d vs BTC | Against the regime? |")
    w("|---|---|---|---|---|---|")
    for c in crypto:
        if c["value"] < 5:
            continue
        verdict = "no: weaker than BTC" if c["rs20_btc"] < -NEUTRAL else ("neutral" if c["rs20_btc"] <= NEUTRAL else "yes: stronger than BTC")
        w(f"| {base(c['coin'])} | {c['account']} | {usd(c['value'])} | {pct(c['move'])} | {pct(c['rs20_btc'])} | {verdict} |")
    w("\nAll crypto shorts sit with an alt-season read of none. GRAM is the one running stronger than BTC. TRUMP and MELANIA are the user-ordered build-ups "
      "(memory trump-melania-buildups). ETH on c248 is dust.\n")

    w("## What was not tested\n")
    w(f"- Per-trade theses for the stocks are not in the journal (13 crypto entries only): the thesis text is the user's stated frame or statement. NET has no recorded rationale.")
    w("- Earnings dates and news: the repo has no source. A stop that sold into earnings cannot be told from one that sold into noise.")
    w(f"- IB stop orders: IB's order types are not saved; closed-trade fills end on the 2026-11-13 statement.")
    w("- HL order history for 07f2 starts 2026-11-02 17:19, so stops placed before then are invisible, and AAPL, JIO and IONQ/SOFI (c248) exits are not matched to a stop.")
    w("- The thesis-level claims themselves (ads, compute, distribution, outages, AI-app valuations) are not visible in price.")
    w("- No trades are recommended; statuses and what would change them are for you to decide with.\n")
    return "\n".join(out)


def run(publish_tile: bool = True) -> dict:
    from desk.accounts import ib_data_dir

    review = build(fills_path=Path(ib_data_dir()) / "ib_fills_8440.csv")
    stats = stop_statistics(review["episodes"])
    names = names_table(review, yahoo_spy())
    placements = stop_placements(review["episodes"], stop_orders())
    regime = regime_read()
    crypto = crypto_shorts()
    today = pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d")
    make_chart(review)
    REPORT.write_text(render_report(review, stats, names, placements, regime, crypto, review.get("whipsaw"), today))
    if publish_tile:
        from desk import tile_feed

        tile_feed.publish(tile_frame(names), "Thesis review", reason="stopped and held stocks: the thesis first, then the loss",
                          key="thesis-review", formats={"Stop realised": "spct", "Since exit": "spct"}, relevance=0.1,
                          hours=24 * 7, push=False)
        tile_feed.push_to_web()
    return {"names": names, "stats": stats, "placements": placements}


def main(argv: list[str] | None = None) -> int:
    command = (argv if argv is not None else sys.argv[1:] or ["run"])[0]
    if command == "run":
        result = run()
        print(f"wrote {REPORT.relative_to(REPO)}, {CHART.relative_to(REPO)} and the Thesis review tile ({len(result['names'])} names)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
