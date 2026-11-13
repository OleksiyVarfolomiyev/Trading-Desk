"""IB account value before the Flex window, rebuilt from an IB activity statement (CSV).

IB's Flex Web Service returns at most a year of daily NAV. An activity statement exported from the
Client Portal (Reports > Statements > Activity, CSV) covers any period but holds no daily values,
only every transaction. This module replays it day by day:

* cash in each currency: deposits and withdrawals (by settle date), each trade's proceeds and
  commission, dividends, withholding tax and fees;
* positions: each trade's quantity, plus stock dividends and rights from corporate actions;
* value: positions at Yahoo Finance's daily closes (the last trade price where Yahoo has none),
  cash and foreign positions at Yahoo's daily exchange rates, in USD.

The result, ``ib_nav_backfill_<last4>.csv`` (``date, nav, flows`` like the Flex history), fills the
days before the Flex history starts; the desk uses it only there. Dividend accruals and interest
are left out, so the rebuilt value can differ from IB's by a few dollars.

    python -m desk.venues.ib_statement ../trading-desk-data/ib/transactions.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import pandas as pd
import requests
from desk.numbers import usd as _usd

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"
# IB listing exchange -> Yahoo Finance ticker suffix (US exchanges need none).
YAHOO_SUFFIX = {"BVME": ".MI", "IBIS": ".DE", "FWB": ".F", "SBF": ".PA", "AEB": ".AS", "LSE": ".L", "EBS": ".SW"}
US_EXCHANGES = {"NASDAQ", "NYSE", "ARCA", "AMEX", "BATS", "PINK", "VALUE", "NYSEAMEX", "IEX"}


def read_sections(path: Path | str) -> dict[str, list[dict[str, str]]]:
    """Every section of an IB statement CSV as a list of row dicts (a section may change header)."""
    sections: dict[str, list[dict[str, str]]] = {}
    headers: dict[str, list[str]] = {}
    with open(path, encoding="utf-8-sig", newline="") as file:
        for row in csv.reader(file):
            if len(row) < 2:
                continue
            name, kind, values = row[0], row[1], row[2:]
            if kind == "Header":
                headers[name] = values
            elif kind == "Data" and name in headers:
                sections.setdefault(name, []).append(dict(zip(headers[name], values)))
    return sections


def _number(text: str) -> float:
    try:
        return float(str(text).replace(",", ""))
    except ValueError:
        return 0.0


def _day(text: str) -> pd.Timestamp:
    return pd.Timestamp(str(text).split(",")[0].strip()).normalize()


def events(sections: dict) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """(cash moves, position moves, deposits): ``(day, currency, amount)``, ``(day, symbol, quantity)``
    and ``(day, usd)``."""
    cash, moves, deposits = [], [], []
    for row in sections.get("Trades", []):
        if row.get("DataDiscriminator") != "Order":
            continue
        day, currency = _day(row["Date/Time"]), row["Currency"]
        quantity, proceeds = _number(row["Quantity"]), _number(row["Proceeds"])
        if row.get("Asset Category") == "Forex":
            base = row["Symbol"].split(".")[0]
            cash.append((day, base, quantity))
            cash.append((day, currency, proceeds))
            cash.append((day, "USD", _number(row.get("Comm in USD", 0))))
        else:
            moves.append((day, row["Symbol"], quantity))
            cash.append((day, currency, proceeds + _number(row.get("Comm/Fee", 0))))
    for name in ("Dividends", "Withholding Tax", "Fees"):
        for row in sections.get(name, []):
            if row.get("Currency") in ("", None) or row["Currency"].startswith("Total") or not row.get("Date"):
                continue
            cash.append((_day(row["Date"]), row["Currency"], _number(row["Amount"])))
    for row in sections.get("Deposits & Withdrawals", []):
        if row.get("Currency") in ("", None) or row["Currency"].startswith("Total") or not row.get("Settle Date"):
            continue
        day, amount = _day(row["Settle Date"]), _number(row["Amount"])
        cash.append((day, row["Currency"], amount))
        deposits.append((day, amount if row["Currency"] == "USD" else 0.0))  # every deposit here is USD
    for row in sections.get("Corporate Actions", []):
        if row.get("Asset Category") != "Stocks" or not row.get("Date/Time"):
            continue
        received = re.findall(r"\(([^,()]+),[^()]*\)\s*$", row.get("Description", ""))
        if received and _number(row.get("Quantity")):
            moves.append((_day(row["Date/Time"]), received[-1].strip(), _number(row["Quantity"])))
    return cash, moves, deposits


def yahoo_ticker(symbol: str, exchange: str) -> str | None:
    if exchange in US_EXCHANGES:
        return symbol.replace(" ", "-")
    suffix = YAHOO_SUFFIX.get(exchange)
    return (re.sub(r"d$", "", symbol) + suffix) if suffix else None  # IB writes German listings as RHMd


def yahoo_closes(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    import time

    for attempt in range(4):  # Yahoo throttles bursts: wait and ask again
        response = requests.get(YAHOO_CHART + ticker, timeout=30, headers={"User-Agent": "Mozilla/5.0 (trading desk)"},
                                params={"interval": "1d", "period1": int(start.timestamp()),
                                        "period2": int(end.timestamp()) + 86400})
        if response.status_code != 429:
            break
        time.sleep(2 * (attempt + 1))
    result = (response.json().get("chart") or {}).get("result") if response.ok else None
    if not result or not result[0].get("timestamp"):
        return pd.Series(dtype=float)
    closes = result[0]["indicators"]["quote"][0]["close"]
    index = pd.to_datetime(result[0]["timestamp"], unit="s", utc=True).tz_convert(None).normalize()
    return pd.Series(closes, index=index, dtype=float).dropna().groupby(level=0).last()


def rebuild_nav(path: Path | str) -> tuple[pd.DataFrame, list[str]]:
    """Daily ``date, nav, flows`` in USD from the first deposit to the statement's last event."""
    sections = read_sections(path)
    cash, moves, deposits = events(sections)
    start = min(day for day, *_ in cash)
    end = max(day for day, *_ in cash + moves)
    days = pd.bdate_range(start, end)
    notes = []
    instruments = {row["Symbol"]: row.get("Listing Exch", "") for row in sections.get("Financial Instrument Information", [])
                   if row.get("Asset Category") == "Stocks"}
    trade_currency = {row["Symbol"]: row["Currency"] for row in sections.get("Trades", [])
                      if row.get("DataDiscriminator") == "Order" and row.get("Asset Category") == "Stocks"}
    # Positions and cash per day: each event counts from its own day on.
    def daily(events_, key_index):
        frame = pd.DataFrame(events_, columns=["day", "key", "amount"])
        table = frame.pivot_table(index="day", columns="key", values="amount", aggfunc="sum")
        return table.reindex(pd.DatetimeIndex(sorted(set(table.index) | set(days)))).fillna(0.0).cumsum().reindex(days, method="ffill")
    holdings = daily(moves, 1).round(8)
    balances = daily(cash, 1)
    # Prices: Yahoo daily closes, else each trade's own price carried forward.
    last_trade = {}
    for row in sections.get("Trades", []):
        if row.get("DataDiscriminator") == "Order" and row.get("Asset Category") == "Stocks":
            last_trade.setdefault(row["Symbol"], []).append((_day(row["Date/Time"]), _number(row["T. Price"])))
    prices = {}
    for symbol in holdings.columns:
        ticker = yahoo_ticker(symbol, instruments.get(symbol, ""))
        series = yahoo_closes(ticker, start, end) if ticker else pd.Series(dtype=float)
        if series.empty:
            fills = pd.Series(dict(last_trade.get(symbol, []))).sort_index()
            series = fills.groupby(level=0).last() if len(fills) else pd.Series(dtype=float)
            if holdings[symbol].abs().max() > 0:
                notes.append(f"{symbol}: no Yahoo prices ({ticker or 'no ticker'}); valued at its own trade prices")
        prices[symbol] = series.reindex(series.index.union(days)).ffill().reindex(days)
    fx = {"USD": pd.Series(1.0, index=days)}
    for currency in set(balances.columns) | set(trade_currency.values()):
        if currency != "USD":
            series = yahoo_closes(f"{currency}USD=X", start, end)
            fx[currency] = series.reindex(series.index.union(days)).ffill().bfill().reindex(days)
    value = sum(balances[currency] * fx[currency] for currency in balances.columns)
    for symbol in holdings.columns:
        price = prices[symbol].fillna(0.0)
        value = value + holdings[symbol] * price * fx.get(trade_currency.get(symbol, "USD"), fx["USD"])
    flows = pd.DataFrame(deposits, columns=["day", "amount"]).groupby("day")["amount"].sum()
    frame = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "nav": value.to_numpy(),
                          "flows": flows.reindex(days).fillna(0.0).to_numpy()})
    return frame, notes


def statement_account(path: Path | str) -> str:
    """The IB account number in the statement (its account field can carry a description after it)."""
    for row in read_sections(path).get("Account Information", []):
        if row.get("Field Name") == "Account":
            found = re.search(r"\b(?:DU|U)\d{5,}\b", row.get("Field Value", ""))
            if found:
                return found.group(0)
    for row in read_sections(path).get("Trades", []):
        if re.fullmatch(r"(?:DU|U)\d{5,}", row.get("Account", "")):
            return row["Account"]
    return ""


def main(argv: list[str] | None = None) -> int:
    from desk.accounts import ib_data_dir
    from desk.privacy import account_file, mask_output

    mask_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("statement", type=Path, help="IB activity statement exported as CSV")
    args = parser.parse_args(argv)
    account = statement_account(args.statement) or "ib"
    frame, notes = rebuild_nav(args.statement)
    path = account_file(ib_data_dir(), "ib_nav_backfill", account)
    frame.to_csv(path, index=False)
    print(f"Rebuilt {len(frame)} days, {frame['date'].iloc[0]} to {frame['date'].iloc[-1]}: {path.name}")
    flex = account_file(ib_data_dir(), "ib_nav_history", account)
    if flex.exists():
        saved = pd.read_csv(flex)
        both = frame.merge(saved, on="date", suffixes=("_rebuilt", "_ib"))
        if len(both):
            gap = (both["nav_rebuilt"] - both["nav_ib"]).abs()
            print(f"Against IB's own daily NAV on {len(both)} overlapping days: mean gap {_usd(gap.mean())}, "
                  f"largest {_usd(gap.max())} (IB {_usd(both['nav_ib'].mean())} on average).")
    for note in notes:
        print("note:", note)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(main())
