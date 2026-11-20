"""Live desk dashboard: the desk_accounts notebook as a local web page that keeps itself current.

Run from the repository root with the py314 environment (it needs plotly):

    python -m desk.dashboard            # http://127.0.0.1:8050
    python -m desk.dashboard --open --interval 120

A background thread re-reads every account in accounts.json (HL wallets, their tokens on other
chains, and IB through a read-only TWS connection) every ``--interval`` seconds. HL value history,
IB Flex history, benchmark prices and the risk statistics are refreshed hourly; IB account
snapshots are appended to the saved IB history at most every 30 minutes. The page polls for the
latest result and redraws in place.

The server listens on 127.0.0.1 only and has no login, because the page shows account balances.
It never places orders.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import faulthandler
import json
import math
import signal
import sys
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd

from desk import degraded, emphasis
from desk.privacy import mask
from desk.day_change import DayChange, desk_day_change, position_changes, position_funding
from desk.overview import day_change_total, funding_total, with_position_returns
from desk.desk_accounts import PERIODS, ROOT_DATA, WINDOWS, DeskAccounts

STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"
DEFAULT_PORT = 8050
DEFAULT_INTERVAL_SECONDS = 60
MIN_INTERVAL_SECONDS = 30
HISTORY_EVERY_SECONDS = 3600
IB_RECORD_EVERY_SECONDS = 1800
MOVERS = 5
# The completed rebalance left only these fractional IB remnants; omit them from open positions.
# A changed quantity (for example, after the next target buy) is shown normally.
REBALANCE_FLAT_RESIDUALS = {"META": 0.75, "GOOGL": 0.25}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}


def _stress_profile(overview) -> dict:
    """What each stress scenario would do to the desk, as a share of equity (desk/stress.py)."""
    from desk import stress
    try:
        exposures = [row for snapshot in overview.snapshots for row in stress.position_exposures(snapshot)]
        return stress.profile(exposures, overview.desk_equity)
    except Exception as exc:
        degraded.report("Stress", exc)
        return {}


def intraday_series(desk) -> dict:
    """Hourly points for the return chart's D and W: the HL wallets' PnL and value (``desk``, [t ms, pnl, value]) and BTC and the
    S&P 500 perp's hourly closes ([t ms, close]); the page rebases each to its window. Matched desk observations
    use snapshots at or before the hour, with all configured wallets present within 65 minutes."""
    import time as _time

    import pandas as _pd

    out = {"desk": [], "matched_desk": [], "accounts": [], "BTC": [], "S&P500": []}
    try:
        from desk.accounts import accounts_for

        wallets = accounts_for("hl", desk.accounts)
        named = [(a.name, desk.histories[a.name].copy()) for a in wallets if a.name in desk.histories]
        histories = [frame for _, frame in named]
        cutoff = _pd.Timestamp.now(tz="UTC") - _pd.Timedelta(days=8)
        frames = []
        for history in histories:
            frame = history[history["ts"] >= cutoff].copy()
            frame["hour"] = _pd.to_datetime(frame["ts"], utc=True).dt.floor("h")
            frames.append(frame.groupby("hour")[["pnl", "accountValue"]].last())
        if frames and all(len(f) for f in frames):
            grid = sorted(set().union(*[set(f.index) for f in frames]))
            combined = _pd.concat([f.reindex(grid).ffill() for f in frames], axis=1, keys=range(len(frames))).dropna()
            for stamp, row in combined.iterrows():
                out["desk"].append([int(stamp.timestamp() * 1000), float(sum(row[(k, "pnl")] for k in range(len(frames)))),
                                    float(sum(row[(k, "accountValue")] for k in range(len(frames))))])
            out["accounts"] = [{"name": name, "points": [
                [int(stamp.timestamp() * 1000), float(row[(k, "accountValue")])]
                for stamp, row in combined.iterrows() if _pd.notna(row[(k, "accountValue")])
            ]} for k, (name, _) in enumerate(named)]
        if histories and len(histories) == len(wallets) and all(len(frame) for frame in frames):
            hours = _pd.date_range(end=_pd.Timestamp.now(tz="UTC").floor("h"), periods=169, freq="h")
            aligned = []
            for history in histories:
                recent = history.loc[history["ts"] >= cutoff, ["ts", "pnl", "accountValue"]].copy()
                recent["ts"] = _pd.to_datetime(recent["ts"], utc=True)
                recent = recent.drop_duplicates("ts", keep="last").set_index("ts").sort_index()
                aligned.append(recent.reindex(hours, method="ffill", tolerance=_pd.Timedelta(minutes=65)))
            joined = _pd.concat(aligned, axis=1, keys=range(len(aligned))).dropna()
            out["matched_desk"] = [
                [int(stamp.timestamp() * 1000), float(row.xs("pnl", level=1).sum()),
                 float(row.xs("accountValue", level=1).sum())]
                for stamp, row in joined.iterrows()
            ]
    except Exception as exc:
        degraded.report("Matched PnL", exc)
    try:
        from desk.hl.trade_analytics import post_info

        end = int(_time.time() * 1000)
        for name, coin, dex in (("BTC", "BTC", ""), ("S&P500", "xyz:SP500", "")):
            request = {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": end - 8 * 86_400_000, "endTime": end}}
            if dex:
                request["dex"] = dex
            out[name] = [[int(row["t"]), float(row["c"])] for row in (post_info(request) or [])]
    except Exception as exc:
        degraded.report("Benchmarks", exc)
    return out


def position_moves(overview) -> dict:
    """Each held position's price move over 7 and 30 days, keyed by its bare symbol, for the uPnL tile's periods: HL positions
    from daily candles, IB stocks from Yahoo closes. A position without history is left out."""
    import pandas as _pd

    out = {}
    try:
        from desk.hl.trade_analytics import fetch_daily_candles
        from desk.live_risk import yahoo_stock_closes

        for snapshot in overview.snapshots:
            for position in snapshot.positions:
                symbol = str(position.symbol)
                key = symbol.split(":")[-1]
                if key in out or not position.mark_price:
                    continue
                try:
                    if position.asset_class == "equity":
                        closes = yahoo_stock_closes(symbol, 45)
                        series = _pd.Series(closes.values, index=_pd.to_datetime(closes.index, utc=True))
                    else:
                        frame = fetch_daily_candles(key, "xyz" if ":" in symbol else "", lookback_days=45)
                        series = _pd.Series(frame["close"].astype(float).values, index=_pd.to_datetime(frame["time"], utc=True))
                except Exception:
                    continue
                moves = {}
                for days in (7, 30):
                    before = series[series.index <= _pd.Timestamp.now(tz="UTC") - _pd.Timedelta(days=days)]
                    if len(before) and float(before.iloc[-1]):
                        moves[str(days)] = float(position.mark_price) / float(before.iloc[-1]) - 1
                if moves:
                    out[key] = moves
    except Exception as exc:
        degraded.report("7d and 30d moves", exc)
    return out


def books_by_kind(tables: dict, max_age: float = 3 * 3600) -> None:
    """The performance table by book (crypto, stocks) instead of by account, from the Mac's hourly computation
    (desk/web_sync.py push_books); the desk's total row stays. Left as it is when that file is missing or stale."""
    import json as _json
    import time as _time

    from desk.web_sync import BOOKS_FILE

    try:
        if _time.time() - BOOKS_FILE.stat().st_mtime > max_age:
            return
        rows = _json.loads(BOOKS_FILE.read_text())["rows"]
    except (OSError, ValueError, KeyError):
        return
    performance = tables.get("performance")
    if performance and rows:
        performance["rows"] = rows


def trade_stats_now() -> dict | None:
    """The HL fills' key numbers (hit rate, average win and loss, EV per close, liquidations) from the Mac's hourly
    computation (desk/web_sync.py trade_stats)."""
    import json as _json

    from desk.web_sync import BOOKS_FILE

    try:
        return _json.loads(BOOKS_FILE.read_text()).get("stats")
    except (OSError, ValueError):
        return None


def book_comparison_now() -> dict:
    """The invested asset-class curves and attribution from the cached book report."""
    import json as _json

    from desk.web_sync import BOOKS_FILE

    try:
        payload = _json.loads(BOOKS_FILE.read_text())
        return {key: payload.get(key, {}) for key in ("attribution", "class_series", "risk_replay", "regimes")}
    except (OSError, ValueError):
        return {}


def book_lines(start) -> list[dict]:
    """The crypto and stocks books on the return chart, each rebased to 0% on the chart's first day (or its own first day)."""
    import json as _json

    from desk.web_sync import BOOKS_FILE

    try:
        series = _json.loads(BOOKS_FILE.read_text()).get("series", {})
    except (OSError, ValueError):
        return []
    lines = []
    for name, points in series.items():
        points = [(day, value) for day, value in points if value is not None and day >= start.strftime("%Y-%m-%d")]
        if len(points) < 2:
            continue
        base = 1 + points[0][1]
        lines.append({"name": name, "x": [day for day, _ in points],
                      "levels": [1 + value for _, value in points],
                      "y": [round(100 * ((1 + value) / base - 1), 2) for _, value in points]})
    return lines


def _managed_since() -> str:
    from desk import drawdown

    return drawdown.managed_since().strftime("%Y-%m-%d")


def usage_model_now() -> dict:
    try:
        from desk import usage_model

        return usage_model.current()
    except Exception:
        return {}


def catalog_entries() -> list[dict]:
    try:
        from desk import tile_catalog

        return tile_catalog.entries()
    except Exception:
        return []


def published_tiles() -> list[dict]:
    try:
        from desk import tile_feed

        return tile_feed.tiles()
    except Exception:   # the feed must never break the dashboard
        return []


FEEDBACK_FILE = Path(__file__).resolve().parents[1] / "data" / "feedback.jsonl"
HIGHLIGHT_STATE_FILE = Path(__file__).resolve().parents[1] / "data" / "highlight_state.json"


def save_highlight_state(raw: bytes, path: Path | None = None) -> bool:
    """Save the dashboard's reviewed highlights for the next browser/device session."""
    try:
        state = json.loads(raw.decode("utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("read"), dict) or not isinstance(state.get("deleted"), list):
            return False
        clean = {"read": state["read"], "deleted": [x for x in state["deleted"] if isinstance(x, str)][:500]}
        destination = path or HIGHLIGHT_STATE_FILE
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(clean), encoding="utf-8")
        return True
    except (ValueError, UnicodeDecodeError, OSError):
        return False


def save_feedback(raw: bytes, path: Path | None = None) -> int:
    """Append the reader's events (a highlight marked read or not important, a section hidden or moved, the look
    changed) to ``data/feedback.jsonl``, one JSON line each; research/feedback_summary.py reads them back."""
    path = path or FEEDBACK_FILE
    try:
        events = json.loads(raw.decode("utf-8")).get("events", [])
    except (ValueError, AttributeError):
        return 0
    lines = [json.dumps({"received": dt.datetime.now(dt.timezone.utc).isoformat(), "source": "local", **event})
             for event in events[:200] if isinstance(event, dict) and isinstance(event.get("kind"), str)
             and isinstance(event.get("action"), str)]
    if lines:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
    return len(lines)


# ----------------------------------------------------------------------------------------------
# Payload: everything the page shows, as plain JSON.

def json_safe(value):
    """Convert numpy/pandas values to JSON types; NaN and infinity become null."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return None if pd.isna(value) else pd.Timestamp(value).isoformat()
    if value is None or value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value if isinstance(value, str) else str(value)


def table(frame: pd.DataFrame, formats: dict[str, str], total: dict | None = None,
          total_last: bool = False, key: dict | None = None) -> dict:
    """A table for the page: column formats, body rows, an optional total row, and key columns.

    ``key`` maps the column(s) that answer the table's question to how their standout value is
    picked ("abs", "max", "min" or None); the page tints them (see desk/emphasis.py) and puts them
    first, right after the leading text columns that name each row (account, symbol, side).
    """
    if frame is None or frame.empty:
        return {"columns": [], "rows": [], "total": None}
    keys = emphasis.normalize(key)
    frame = frame[key_columns_first(list(frame.columns), keys, formats)]
    rows = frame.to_dict(orient="records")
    if total_last:
        rows, total = rows[:-1], rows[-1]
    return {"columns": [{"key": column, "fmt": formats.get(column, "text"),
                         **({"emphasis": keys[column] or "none"} if column in keys else {})}
                        for column in frame.columns],
            "rows": rows, "total": total}


# Formats the page colours (by sign, as risk, or LONG/SHORT): these columns move up with the key ones.
COLORED_FORMATS = {"susd", "vusd", "vpct", "spct", "sbps", "rusd", "rpct", "side"}


def key_columns_first(columns: list[str], keys, formats: dict[str, str]) -> list[str]:
    """Column order with the key (tinted) and colour-coded columns moved up (desk/emphasis.py)."""
    return emphasis.key_columns_first(columns, keys, [c for c in columns if formats.get(c, "text") in COLORED_FORMATS],
                                      text={c for c in columns if formats.get(c, "text") == "text"})


# Account dollar columns shown as a share of the account's own equity.
ACCOUNT_SHARES = {"Cash": "Cash / equity", "Margin used": "Margin used / equity", "Available": "Available / equity",
                  "Unrealized PnL": "uPnL / equity", "Gross": "Gross / equity", "Net": "Net / equity"}


def account_shares(accounts: pd.DataFrame) -> tuple[pd.DataFrame, dict | None]:
    """The accounts table in percent: % of desk, and each dollar column over the account's equity.
    The total row divides the summed dollars by the summed equity."""
    if accounts.empty:
        return accounts, None
    shares = accounts[["Venue", "Account", "% of desk"]].copy()
    equity = accounts["Equity"].where(accounts["Equity"] > 0)
    for column, name in ACCOUNT_SHARES.items():
        shares[name] = accounts[column] / equity
    shares["Positions"] = accounts["Positions"]
    total_equity = accounts["Equity"].sum()
    total = {"Venue": "Total", "Account": "", "% of desk": None, "Positions": accounts["Positions"].sum(),
             **{name: accounts[column].sum() / total_equity if total_equity > 0 else None
                for column, name in ACCOUNT_SHARES.items()}}
    return shares, total


def _daily(series: pd.Series) -> pd.Series:
    series = series.sort_index()
    return series.resample("1D").last().ffill().dropna()


def _wallet_value_series(desk: DeskAccounts, start: pd.Timestamp) -> dict | None:
    """One series for the HL wallets' tokens on other chains: recorded values, rebuilt before.

    Values are recorded from run to run (``onchain_value_history_<wallet>.csv``); the days before
    recording began come from the one-off rebuild in ``desk.venues.onchain_history``
    (``onchain_value_backfill_<wallet>.csv``), used only where nothing was recorded.
    """
    from desk.overview import wallet_daily_value

    parts, rebuilt_until = [], None
    for account in desk.accounts:
        if account.venue != "hl" or not account.onchain:
            continue
        series, rebuilt_last = wallet_daily_value(desk.data_dir, account.id)
        if rebuilt_last is not None:
            rebuilt_until = max(rebuilt_until or rebuilt_last, rebuilt_last)
        if len(series):
            parts.append(series)
    if not parts:
        return None
    total = pd.concat(parts, axis=1).ffill().sum(axis=1, min_count=1).dropna()
    total = total[total.index >= start.normalize()]
    if total.empty:
        return None
    note = (f"wallets rebuilt from on-chain balances to {rebuilt_until:%Y-%m-%d}, recorded after"
            if rebuilt_until is not None else f"wallets recorded since {total.index[0]:%Y-%m-%d}")
    return {"name": "Wallets outside HL", "note": note,
            "x": [ts.strftime("%Y-%m-%d") for ts in total.index], "y": total.round(2).tolist()}


def _value_with_wallets(value_series: list[dict], wallets: dict | None, min_days: int = 2) -> dict | None:
    """Desk value (the stacked accounts) plus wallet tokens, on the days wallet values are known."""
    if wallets is None or len(wallets["x"]) < min_days:
        return None
    accounts = [pd.Series(series["y"], index=pd.to_datetime(series["x"])) for series in value_series]
    stacked = pd.concat(accounts, axis=1).ffill().fillna(0.0).sum(axis=1) if accounts else pd.Series(dtype=float)
    wallet = pd.Series(wallets["y"], index=pd.to_datetime(wallets["x"]))
    total = (stacked.reindex(wallet.index, method="ffill").fillna(0.0) + wallet).round(2)
    return {"name": "Total", "note": wallets.get("note", ""),
            "x": [ts.strftime("%Y-%m-%d") for ts in total.index], "y": total.tolist()}


def _without_dust_wallets(performance: pd.DataFrame, desk_value: float) -> pd.DataFrame:
    """The performance rows without wallets worth less than WALLET_MIN_SHARE of the desk (the totals keep them)."""
    from desk.overview import WALLET_MIN_SHARE

    if not desk_value or "Value" not in performance:
        return performance
    dust = performance["Account"].astype(str).str.endswith("wallet") & (performance["Value"] / desk_value < WALLET_MIN_SHARE)
    return performance[~dust].reset_index(drop=True)


def with_day_returns(performance: pd.DataFrame, day_change: DayChange | None) -> pd.DataFrame:
    """The performance table with a 24h return first: each account's 24-hour change (net of flows, the
    desk-value tile's own figures) over its value a day ago. The two desk rows sum their accounts; an
    account whose change could not be read leaves a blank, and the desk rows then leave it out."""
    frame = performance.copy()
    parts = (day_change.parts if day_change is not None else {})
    values = dict(zip(frame["Account"], frame["Value"]))
    day = []
    for name in frame["Account"]:
        if name.startswith("Desk") or name == "Total":
            named = [account for account in parts if account in values and not (name == "Desk (HL + IB)" and account.endswith(" wallet"))]
            change, value = sum(parts[account] for account in named), sum(values[account] for account in named)
            day.append(change / (value - change) if named and value - change > 0 else float("nan"))
        elif name in parts and values[name] - parts[name] > 0:
            day.append(parts[name] / (values[name] - parts[name]))
        else:
            day.append(float("nan"))
    frame.insert(frame.columns.get_loc("Return 7D"), "Return 24h", day)
    return frame


def build_payload(desk: DeskAccounts, day_change: DayChange | None = None,
                  moves: dict[tuple[str, str], float] | None = None,
                  funding: dict[tuple[str, str], float] | None = None) -> dict:
    """The page's data from a fetched DeskAccounts: tiles, highlights, tables and chart series.

    ``day_change`` is the desk's 24-hour change net of deposits (``desk.day_change``), fetched by
    the refresh loop; without it the tile shows no change. ``moves`` is each position's 24-hour
    price change ({(account, symbol): fraction}, ``desk.day_change.position_changes``); ``funding`` is each
    HL perp's funding APR from the holder's side (``desk.day_change.position_funding``).
    """
    overview = desk.overview
    trading, wallets = overview._trading(), overview._wallets()
    trading_equity = sum(snapshot.equity_usd for snapshot in trading)
    gross = sum(snapshot.gross_usd for snapshot in trading)
    net = sum(snapshot.net_usd for snapshot in trading)
    unrealized = sum(snapshot.unrealized_usd for snapshot in trading)
    positions = overview.positions_frame(trading_only=True)  # wallet tokens have their own table
    if len(positions):
        residual = positions.apply(lambda row: row.get("Venue") == "ib"
                                   and row.get("Symbol") in REBALANCE_FLAT_RESIDUALS
                                   and math.isclose(abs(float(row.get("Quantity", 0))),
                                                    REBALANCE_FLAT_RESIDUALS[row["Symbol"]], abs_tol=1e-8), axis=1)
        positions = positions[~residual].copy()
    # The tiles show the whole desk, wallets included (the performance table's bold last row); the
    # money-weighted PnL stays HL + IB, since the wallets have no deposit record.
    headline = desk.headline_index()
    returns = desk.desk_returns(headline)
    peak = headline.cummax()
    kpis = {
        "trade_stats": trade_stats_now(),
        "desk_value": overview.desk_equity,
        "day_change": day_change.as_dict() if day_change is not None else None,
        "trading_equity": trading_equity,
        "wallet_value": sum(snapshot.equity_usd for snapshot in wallets),
        "unrealized": unrealized,
        "unrealized_share": unrealized / gross if gross else None,
        "gross": gross, "net": net,
        "gross_leverage": gross / trading_equity if trading_equity else None,
        "net_leverage": net / trading_equity if trading_equity else None,
        "positions": int(sum(len(snapshot.positions) for snapshot in trading)),
        "stress": _stress_profile(overview),
        "returns": {"since_start": returns.get("since start"), **{label: returns.get(label) for label in WINDOWS}},
        "money_weighted": desk.money_weighted(),
        "since": headline.index[0],
        "drawdown": float(headline.iloc[-1] / peak.iloc[-1] - 1),
        "peak_date": headline.idxmax(),
    }

    well, attention, _, _ = desk.highlight_rows(funding if funding is not None else {})
    from desk import alerts as daemon_alerts

    attention[:0] = daemon_alerts.rows(daemon_alerts.read())  # the trading daemon's problems first
    from desk import funding_watch

    attention = funding_watch.consolidate(attention)  # the daemon's and the current-rate funding rows as one
    # The desk return is in the tiles and the performance table's bold row: no highlight on the dashboard.
    well = [row for row in well if row[0] != "Desk return"]
    attention = [row for row in attention if row[0] != "Desk return"]

    accounts, account_total = account_shares(overview.accounts_frame())
    # Open risk over each account's risk equity (the executor's); the total row is the desk against its cap.
    risk_by_label = {entry.label: entry.risk_usd / entry.equity_usd for entry in overview.book if entry.equity_usd > 0}
    if len(accounts):
        accounts["Open risk"] = accounts["Account"].map(risk_by_label)
        if account_total is not None and overview.book:
            risk_equity, risk_used, _ = overview.risk_totals()
            account_total["Open risk"] = risk_used / risk_equity if risk_equity else None
    money = {column: "usd" for column in ("Equity", "Cash", "Margin used", "Available", "Gross", "Value", "Risk equity",
                                           "Open risk", "Long", "Short", "VaR 95%", "VaR 99%")}
    signed = {column: "susd" for column in ("uPnL", "Net", "Value")}
    tables = {
        "accounts": table(accounts, {**{column: "pct" for column in ACCOUNT_SHARES.values()},
                                     "uPnL / equity": "spct", "Net / equity": "spct",
                                     "Positions": "int", "% of desk": "pct", "Open risk": "rpct"}, account_total,
                          key={"% of desk": "max"}),
        # Returns only: the dollar PnL is in the accounts' value chart and the desk value tile.
        "performance": table(_without_dust_wallets(with_day_returns(desk.performance_frame(), day_change), overview.desk_equity)[
                                 ["Account", "Return 24h"] + [f"Return {label}" for label in PERIODS] + ["Since"]],
                             {f"Return {label}": "spct" for label in ["24h", *PERIODS]}, total_last=True,
                             key={"Return 30D": "abs"}),
        "exposure": table(overview.exposure_frame(), {**money, "Net": "susd", "Gross / equity": "pct",
                                                      "Net / equity": "spct"}, total_last=True,
                          key={"Net / equity": "abs"}),
        **{key: table(overview.movers_frame(MOVERS, kind), {"Side": "side", "Value": "susd", "uPnL": "susd",
                                                            "% of position": "spct"}, key={"uPnL": "abs"})
           for key, kind in (("movers", "Crypto"), ("movers_stocks", "Stocks"))},
    }
    def position_table(rows):
        ordered = rows.assign(_abs=rows["Value"].abs()).sort_values(
            ["Asset class", "_abs"], ascending=[True, False]).drop(columns="_abs")
        total = {column: None for column in ordered.columns}
        total.update({"Asset class": "Total", "Value": ordered["Value"].sum(),
                      "uPnL": ordered["uPnL"].sum(), "Weight": None,   # a 100% total is not shown
                      "Exposure / equity": ordered["Exposure / equity"].sum()})
        priced = ordered["uPnL (%)"].notna()
        cost = (ordered["Value"] - ordered["uPnL"]).abs()[priced].sum()
        total["uPnL (%)"] = ordered["uPnL"][priced].sum() / cost if cost else None
        total["24h change (%)"] = day_change_total(ordered["Value"], ordered["24h change (%)"])
        total["Funding (APR)"] = funding_total(ordered["Value"], ordered["Funding (APR)"])
        return table(ordered, {"Side": "side", "Quantity": "num", "Avg price": "price", "Mark": "price", **signed,
                               "uPnL (%)": "spct", "24h change (%)": "spct", "Funding (APR)": "spct",
                               "Weight": "pct", "Exposure / equity": "pct"}, total,
                     key={"Weight": "max"})

    if len(positions):
        ordered = with_position_returns(positions, moves, funding)
        crypto = ordered["Asset class"].astype(str).str.startswith("crypto")  # else stocks and tradfi (IB, HL equity perps)
        tables["positions"] = position_table(ordered[crypto])
        tables["positions_stocks"] = position_table(ordered[~crypto])
    else:
        tables["positions"] = table(positions, {})
        tables["positions_stocks"] = table(positions, {})
    try:  # market-making ladders (desk/market_making.py); resting quotes from the trading daemon
        from desk import alerts as daemon_alerts
        from desk import market_making

        daemon_state = daemon_alerts.read() or {}
        quotes = {(item["wallet"], item["coin"]): item["quotes"] for item in daemon_state.get("ladders", [])}
        queued = {(item["wallet"], item["coin"]): item.get("queued", 0) for item in daemon_state.get("ladders", [])}
        status = {(item["wallet"], item["coin"]): item["status"] for item in daemon_state.get("ladders", []) if item.get("status")}
        settings = {(item["wallet"], item["coin"]): item.get("tuning", "") for item in daemon_state.get("ladders", [])}
        frame = market_making.market_making_frame(quotes, queued, status, settings)
        tables["market_making"] = (table(frame, market_making.FORMATS, market_making.total_row(frame),
                                         key={"Ladder PnL": "abs"}) if len(frame) else table(frame, {}))
    except Exception as exc:
        desk.notes.append(f"Market-making metrics unavailable ({type(exc).__name__}: {exc}).")
    research: dict = {}
    # Performance by strategy, trades due for review and the screener, as the trading daemon last saved them (hourly).
    try:
        import json as _json

        from desk import journal, screener

        research_path = ROOT_DATA / "research_state.json"
        research = _json.loads(research_path.read_text()) if research_path.exists() else {}
        strategies = pd.DataFrame(research.get("strategies", []), columns=journal.COLUMNS)
        from desk import alerts as daemon_alerts

        alerts_now = daemon_alerts.read() or {}   # the daemon's live uPnL by strategy (every minute), when fresh
        if alerts_now.get("strategy_upnl") and time.time() - float(alerts_now.get("strategy_upnl_at") or 0) < 600:
            strategies = journal.with_live_unrealized(strategies, alerts_now["strategy_upnl"])
        view = journal.performance_view(strategies, overview.desk_equity) if len(strategies) > 1 else pd.DataFrame()
        tables["strategies"] = (table(view, journal.VIEW_FORMATS, total_last=True, key={"Total": "abs"}) if len(view)
                                else table(view, {}))
        screen = research.get("screener") or screener.load() or {}
        shorts = pd.DataFrame(screen.get("shorts", []), columns=screener.CANDIDATE_COLUMNS)
        tables["candidates"] = table(shorts, screener.CANDIDATE_FORMATS, key={"Score": "min"}) if len(shorts) else table(shorts, {})
        mm = pd.DataFrame(screen.get("market_making", []), columns=screener.MM_COLUMNS)
        tables["mm_candidates"] = table(mm, screener.MM_FORMATS, key={"Efficiency 30d": "min"}) if len(mm) else table(mm, {})
    except Exception as exc:
        desk.notes.append(f"Research tables unavailable ({type(exc).__name__}: {exc}).")
    try:  # each token's 24-hour move (DefiLlama), as the positions tables show it
        from desk.day_change import position_changes

        wallet_moves = position_changes(overview._wallets())
    except Exception as exc:
        degraded.report("Wallet moves", exc)
        wallet_moves = {}
    holdings = overview.wallet_holdings_frame(moves=wallet_moves)
    if len(holdings):  # from WALLET_MIN_SHARE of the desk up: smaller holdings are dust for decisions
        from desk.overview import WALLET_MIN_SHARE

        holdings = holdings[holdings["% of desk"] >= WALLET_MIN_SHARE]
    if len(holdings):
        # "HL 07f2 wallet" -> "HL 07f2": the table is already the wallets table. Notes go last.
        holdings = holdings.assign(Wallet=holdings["Wallet"].str.replace(r"\s+wallet$", "", regex=True))
        holdings = holdings.rename(columns={"Token": "Symbol"})
        holdings = holdings[[column for column in holdings.columns if column != "Note"] + ["Note"]]
        tables["wallets"] = table(holdings, {"Amount": "num", "Price": "price", "Value": "vusd", "% of desk": "vpct",
                                             "24h change (%)": "spct"},
                                  {"Wallet": "Total", "Value": holdings["Value"].sum(),
                                   "% of desk": holdings["% of desk"].sum()}, key={"Value": "max"})

    start = desk.common_start
    value_series = []
    daily_values = {name: _daily(history.set_index(pd.to_datetime(history["ts"], utc=True))["accountValue"])
                    for name, history in desk.histories.items()}
    # Every layer runs to the chart's last day: a stacked layer with no point on a date reads as $0,
    # and IB's Flex NAV stops at the last completed session (Friday, on a weekend). The last known
    # value carries forward; a layer still starts at its own first day.
    last_day = max(series.index.max() for series in daily_values.values()) if daily_values else None
    # Each layer ends today at the account's live value (the accounts table's), so the chart's end labels
    # are current values rather than the last saved day (IB's Flex NAV is yesterday's close).
    current = {snapshot.label: snapshot.equity_usd for snapshot in trading}
    today = pd.Timestamp.now(tz="UTC").normalize()
    if last_day is not None and current:
        last_day = max(last_day, today)
    for name, series in daily_values.items():
        series = series.reindex(pd.date_range(series.index.min(), last_day, freq="D")).ffill()
        if name in current and np.isfinite(current[name]) and len(series):
            series.iloc[-1] = current[name]
        series = series[series.index >= start.normalize()]
        value_series.append({"name": name, "x": [ts.strftime("%Y-%m-%d") for ts in series.index],
                             "y": series.round(2).tolist()})
    # Wallet tokens are not a stacked layer: a stacked layer reads as zero wherever it has no data,
    # and their values are only recorded from run to run. Once two days exist, a separate line shows
    # the desk value including them, from the first recorded day only.
    with_wallets = _value_with_wallets(value_series, _wallet_value_series(desk, start))
    # The return chart's desk line is HL + IB without wallet tokens, like the crypto and stocks books drawn with it
    # (like with like: the books exclude wallets), as % since its start; the benchmarks start the same day.
    index = _daily(desk.index)
    index.index = index.index.normalize()
    twr = [{"name": "Desk (HL + IB)",
            "x": [ts.strftime("%Y-%m-%d") for ts in index.index], "levels": index.tolist(),
            "y": (100 * (index / index.iloc[0] - 1)).round(2).tolist()}]
    for name in ("BTC", "SPY"):
        if name in desk.benchmarks:
            prices = desk.benchmarks[name].dropna()
            prices = prices[prices.index >= index.index[0]]
            if len(prices):
                twr.append({"name": name, "x": [ts.strftime("%Y-%m-%d") for ts in prices.index], "levels": prices.tolist(),
                            "y": (100 * (prices / prices.iloc[0] - 1)).round(2).tolist()})

    twr += book_lines(index.index[0])

    from desk import reconcile

    books_by_kind(tables)
    book_comparison = book_comparison_now()
    # Tables that should tie out and don't, and compared figures whose windows differ: first.
    attention = reconcile.checks(tables) + reconcile.windows({"twr": twr}, book_comparison) + attention
    # Rows a published tile carries (the forward monitor's "Live drift" and "No historical / forward test").
    attention += [list(row) for tile in published_tiles() for row in tile.get("attention", [])]
    notes = list(desk.notes)
    notes += [f"{snapshot.label}: {note}" for snapshot in overview.snapshots for note in snapshot.notes]
    book_comparison["desk_series"] = [
        [stamp.strftime("%Y-%m-%d"), float(level)] for stamp, level in _daily(desk.index).items()
    ]
    return {
        "kpis": kpis,
        "horizon_days": research.get("horizon_days") if isinstance(research, dict) else None,
        "theses": (research.get("theses") if isinstance(research, dict) else None) or [],   # the thesis clock   # the open trades' median age
        "book_comparison": book_comparison,
        "intraday": intraday_series(desk),   # the day and week at hourly resolution (the return chart's D and W)
        "moves": position_moves(overview),   # the held positions' price moves over 7 and 30 days
        "usage_model": usage_model_now(),   # what the reader has said is noise (desk/usage_model.py)
        "tiles": published_tiles(),
        "catalog": catalog_entries(),   # the notebook sections that can be pulled onto the dashboard (desk/tile_catalog.py)   # charts and tables published from the notebooks (desk/tile_feed.py)
        "highlights": {"well": [list(row) for row in well], "attention": [list(row) for row in attention],
                       "hover": daemon_alerts.hovers(daemon_alerts.read())},  # headlines around a big move
        "tables": tables,
        "charts": {"value": value_series, "value_with_wallets": with_wallets, "twr": twr,
                   "managed_since": _managed_since(),   # the repo start: the return chart marks where the managed desk begins
                   # Stretches with no trading on any account, collapsed on the value chart.
                   "value_idle": [[start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), (end - start).days]
                                  for start, end in desk.idle_periods()]},
        "problems": list(overview.problems),
        "notes": notes,
    }


# ----------------------------------------------------------------------------------------------
# Refresh loop

class Dashboard:
    """Holds the latest payload and refreshes it in a background thread."""

    def __init__(self, interval: int = DEFAULT_INTERVAL_SECONDS, data_dir: Path | str = ROOT_DATA,
                 wallet_data_dir: Path | str | None = None, regime_start: str | None = None):
        self.interval = max(MIN_INTERVAL_SECONDS, int(interval))
        self.desk = DeskAccounts(data_dir=data_dir, wallet_data_dir=wallet_data_dir, regime_start=regime_start)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.payload: dict | None = None
        self.payload_json = b""
        self.last_history = 0.0
        self.last_ib_record = 0.0
        self.last_seen: dict[str, str] = {}
        self.status = {"refreshing": False, "last_ok": None, "last_attempt": None, "error": None,
                       "duration_s": None, "history_at": None, "interval_s": self.interval}

    def account_status(self, problems: list[str]) -> list[dict]:
        now = pd.Timestamp.now(tz="UTC").isoformat()
        rows = []
        for account in self.desk.accounts:
            problem = next((item for item in problems if item.split(":")[0].strip() == account.name), None)
            if problem is None:
                self.last_seen[account.name] = now
            rows.append({"name": account.name, "venue": account.venue, "ok": problem is None,
                         "problem": problem, "last_seen": self.last_seen.get(account.name)})
        names = {row["name"] for row in rows}
        for row in degraded.rows():   # data that could not be built: a chip each, like an unreadable account
            if row["name"] not in names:
                rows.append(row)
        return rows

    def refresh(self) -> None:
        started = time.time()
        history = started - self.last_history >= HISTORY_EVERY_SECONDS
        record = started - self.last_ib_record >= IB_RECORD_EVERY_SECONDS
        with self.lock:
            self.status.update(refreshing=True, last_attempt=pd.Timestamp.now(tz="UTC").isoformat())
            self._publish()
        degraded.clear()
        try:
            self.desk.fetch_data(refresh_history=history, record_ib_history=record)
            try:
                day = desk_day_change(self.desk.overview.snapshots)
            except Exception as exc:  # the tile then shows no 24-hour change; everything else still updates
                traceback.print_exc()
                degraded.report("24h change", exc)
                day = None
            try:
                moves = position_changes(self.desk.overview.snapshots)
            except Exception as exc:  # the 24h column is then blank
                traceback.print_exc()
                degraded.report("24h moves", exc)
                moves = None
            try:
                funding = position_funding(self.desk.overview.snapshots)
            except Exception as exc:  # the funding column is then blank
                traceback.print_exc()
                degraded.report("Funding", exc)
                funding = None
            payload = build_payload(self.desk, day, moves, funding)
            payload["accounts_status"] = self.account_status(payload["problems"])
            self._send_ib_to_web(moves)
            with self.lock:
                self.payload = json_safe(payload)
                self.status.update(last_ok=pd.Timestamp.now(tz="UTC").isoformat(), error=None,
                                   duration_s=round(time.time() - started, 1))
                if history:
                    self.last_history = started
                    self.status["history_at"] = self.status["last_ok"]
                if record and any(snapshot.venue == "ib" for snapshot in self.desk.overview.snapshots):
                    self.last_ib_record = started
        except Exception as exc:  # keep serving the last good data and say what failed
            traceback.print_exc()
            with self.lock:
                self.status["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            with self.lock:
                self.status["refreshing"] = False
                self._publish()

    def _send_ib_to_web(self, moves) -> None:
        """Send IB to the web dashboard's relay (desk/web_sync.py) at most every 10 minutes, in the background."""
        from desk import web_sync

        if time.time() - getattr(self, "last_web_push", 0.0) < web_sync.PUSH_EVERY_SECONDS:
            return
        from desk import alerts as daemon_alerts

        daemon = daemon_alerts.read() or {}
        daemon_sends_ib = time.time() - float(daemon.get("heartbeat", 0)) < daemon_alerts.STALE_SECONDS
        overview = self.desk.overview
        snapshot = next((item for item in overview.snapshots if item.venue == "ib"), None)
        if snapshot is None or daemon_sends_ib:  # the trading daemon sends IB itself while it runs
            return
        try:
            if not web_sync.ingest_token():
                return  # not set up: the web dashboard simply has no IB
        except Exception:
            return
        entry = next((item for item in overview.book if item.venue == "ib"), None)
        self.last_web_push = time.time()

        def send():
            try:
                web_sync.push_ib(snapshot, entry, moves)
                if time.time() - getattr(self, "last_slow_push", 0.0) >= 3600:
                    self.last_slow_push = time.time()
                    web_sync.push_wallets(self.desk.data_dir)
                    web_sync.push_regime()
            except Exception as exc:  # the web dashboard keeps the last IB it received
                print(f"Sending IB to the web desk failed: {exc}")

        threading.Thread(target=send, name="web-sync", daemon=True).start()

    def _publish(self) -> None:
        """Serialize payload and status once per change (the page polls often)."""
        document = {"status": self.status, "data": self.payload,
                    "server_time": pd.Timestamp.now(tz="UTC").isoformat()}
        # Account errors can quote a full wallet address or account number; the page shows only the last four.
        self.payload_json = mask(json.dumps(json_safe(document), allow_nan=False)).encode()

    def state_json(self) -> bytes:
        with self.lock:
            if not self.payload_json:
                self._publish()
            return self.payload_json

    def run_forever(self) -> None:
        # ib_async needs an event loop in this thread; the desk code calls it directly here.
        asyncio.set_event_loop(asyncio.new_event_loop())
        while True:
            self.refresh()
            self.wake.wait(self.interval)
            self.wake.clear()


# ----------------------------------------------------------------------------------------------
# HTTP

def make_handler(dashboard: Dashboard, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    plotly_js = None

    class Handler(BaseHTTPRequestHandler):
        server_version = "DeskDashboard/1.0"

        def _host_ok(self) -> bool:
            # Rejects other hostnames pointed at this port (DNS rebinding) so only this machine's
            # browser, addressing it as localhost, can read the balances.
            if self.headers.get("Host", "") not in allowed_hosts:
                self.send_error(403, "Use http://127.0.0.1 or http://localhost")
                return False
            return True

        def _send(self, body: bytes, content_type: str, cache: bool = False) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            nonlocal plotly_js
            if not self._host_ok():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/highlight_state":
                try:
                    payload = HIGHLIGHT_STATE_FILE.read_bytes()
                except OSError:
                    payload = b'{"read":{},"deleted":[]}'
                self._send(payload, "application/json")
            elif path == "/api/tiles/status":
                from desk import tile_catalog

                self._send(json.dumps(tile_catalog.STATUS).encode(), "application/json")
            elif path == "/api/state":
                self._send(dashboard.state_json(), "application/json")
            elif path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                self._send((STATIC_DIR / name).read_bytes(), content_type)
            elif path == "/plotly.min.js":
                if plotly_js is None:
                    import plotly

                    plotly_js = (Path(plotly.__file__).parent / "package_data" / "plotly.min.js").read_bytes()
                self._send(plotly_js, "text/javascript; charset=utf-8", cache=True)
            else:
                self.send_error(404)

        def do_POST(self):
            if not self._host_ok():
                return
            origin = self.headers.get("Origin")
            if origin and origin.split("//", 1)[-1] not in allowed_hosts:
                self.send_error(403)
                return
            if self.path == "/api/refresh":
                dashboard.wake.set()
                self._send(b'{"ok": true}', "application/json")
            elif self.path == "/api/tiles/pull":
                from desk import tile_catalog

                length = min(int(self.headers.get("Content-Length") or 0), 4_000)
                wanted = json.loads(self.rfile.read(length) or b"{}").get("id", "")
                tile_catalog.start_pull(str(wanted))
                self._send(json.dumps({"ok": True, "status": tile_catalog.STATUS.get(str(wanted), "running")}).encode(), "application/json")
            elif self.path == "/api/feedback":
                self._send(json.dumps({"ok": True, "saved": save_feedback(self.rfile.read(
                    min(int(self.headers.get("Content-Length") or 0), 64_000)))}).encode(), "application/json")
            elif self.path == "/api/highlight_state":
                self._send(json.dumps({"ok": save_highlight_state(self.rfile.read(
                    min(int(self.headers.get("Content-Length") or 0), 128_000)))}).encode(), "application/json")
            else:
                self.send_error(404)

        def log_message(self, format, *args):  # quiet: the page polls every few seconds
            if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
                super().log_message(format, *args)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live desk on http://127.0.0.1 (read-only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
                        help=f"seconds between account refreshes (minimum {MIN_INTERVAL_SECONDS})")
    parser.add_argument("--data-dir", default=str(ROOT_DATA), help="desk data folder (default: the repo's data/)")
    parser.add_argument("--wallet-data-dir", default=None, help="HL history folder (default: the repo's data/)")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    parser.add_argument("--regime-start", default=None,
                        help="start of the current regime (YYYY-MM-DD) for risk statistics; detected from the HL "
                             "book's exposure by default")
    args = parser.parse_args(argv)

    # `kill -USR1 <pid>` writes every thread's stack to the log: shows where a slow refresh is waiting.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    try:
        dashboard = Dashboard(args.interval, args.data_dir, args.wallet_data_dir, args.regime_start)
    except ValueError as exc:  # no accounts.json, or an invalid one
        print(f"Cannot start the desk: {exc}", file=sys.stderr)
        return 2
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(dashboard, args.port))
    except OSError as exc:
        print(f"Cannot listen on 127.0.0.1:{args.port} ({exc.strerror or exc}); is the desk already running? "
              "Use --port to pick another port.", file=sys.stderr)
        return 2
    threading.Thread(target=dashboard.run_forever, name="desk-refresh", daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Desk on {url} (refresh every {dashboard.interval}s; Ctrl+C stops). Read-only.", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    from desk.privacy import mask_output

    mask_output()  # only the last four characters of any wallet or account number are shown
    raise SystemExit(main())
