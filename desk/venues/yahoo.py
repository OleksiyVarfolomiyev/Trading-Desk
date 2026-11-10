"""Yahoo Finance's free last price for US stocks and ETFs (no key; real time for US listings).

Used where IB only gives delayed data: live entry prices in the IB executor and the intraday
point on the IB holdings chart. Read-only.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import requests

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
USER_AGENT = "trading-desk/1.0 (read-only quote)"


@dataclass(frozen=True)
class LastPrice:
    symbol: str
    price: float
    time: pd.Timestamp          # UTC time of the last regular-session trade
    currency: str
    previous_close: float
    session_start: pd.Timestamp | None
    session_end: pd.Timestamp | None

    def in_session(self, now: pd.Timestamp | None = None) -> bool:
        now = pd.Timestamp.now(tz="UTC") if now is None else now
        return bool(self.session_start is not None and self.session_start <= now <= self.session_end)


def last_price(symbol: str, timeout: float = 15) -> LastPrice:
    """The last regular-session price; raises ValueError when Yahoo has none."""
    response = requests.get(CHART_URL + symbol, params={"interval": "1m", "range": "1d"}, timeout=timeout,
                            headers={"User-Agent": USER_AGENT})
    if not response.ok:
        raise ValueError(f"Yahoo Finance quote for {symbol} failed (HTTP {response.status_code})")
    try:
        meta = response.json()["chart"]["result"][0]["meta"]
        price, stamp = float(meta["regularMarketPrice"]), int(meta["regularMarketTime"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError(f"Yahoo Finance returned no price for {symbol}") from None
    session = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = session.get("start"), session.get("end")
    return LastPrice(
        symbol=symbol, price=price, time=pd.Timestamp(stamp, unit="s", tz="UTC"),
        currency=str(meta.get("currency") or "USD"),
        previous_close=float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0.0),
        session_start=pd.Timestamp(start, unit="s", tz="UTC") if start is not None else None,
        session_end=pd.Timestamp(end, unit="s", tz="UTC") if end is not None else None,
    )


def price_change(symbol: str, since: pd.Timestamp, timeout: float = 15) -> tuple[float, float, str]:
    """``(price at since, price now, currency)`` from 5-minute bars over the last 5 days.

    The price at ``since`` is the last bar closing at or before it, so over a weekend or holiday
    both prices are the last close and the change is zero. Raises ValueError when Yahoo has no bar
    that old or no current price.
    """
    response = requests.get(CHART_URL + symbol, params={"interval": "5m", "range": "5d"}, timeout=timeout,
                            headers={"User-Agent": USER_AGENT})
    if not response.ok:
        raise ValueError(f"Yahoo Finance bars for {symbol} failed (HTTP {response.status_code})")
    try:
        result = response.json()["chart"]["result"][0]
        meta, stamps = result["meta"], result.get("timestamp") or []
        closes = result["indicators"]["quote"][0]["close"]
        now = float(meta["regularMarketPrice"])
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError(f"Yahoo Finance returned no bars for {symbol}") from None
    cutoff = pd.Timestamp(since).timestamp()
    then = [close for stamp, close in zip(stamps, closes) if stamp <= cutoff and close is not None]
    if not then:
        raise ValueError(f"Yahoo Finance has no {symbol} price as old as {since}")
    return float(then[-1]), now, str(meta.get("currency") or "USD")


def extended_price(symbol: str, timeout: float = 15, days: int = 1) -> tuple[float, pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """``(last price, its UTC time, session start, session end)`` including pre-market and after-hours.

    The session runs from the pre-market open to the after-hours close (04:00-20:00 ET for US
    stocks). ``days`` > 1 looks back further, for the last trade when today has none yet
    (overnight, after midnight). Raises ValueError when Yahoo has no bar.
    """
    response = requests.get(CHART_URL + symbol, params={"interval": "1m", "range": f"{days}d", "includePrePost": "true"},
                            timeout=timeout, headers={"User-Agent": USER_AGENT})
    if not response.ok:
        raise ValueError(f"Yahoo Finance bars for {symbol} failed (HTTP {response.status_code})")
    try:
        result = response.json()["chart"]["result"][0]
        periods = result["meta"]["currentTradingPeriod"]
        start, end = int(periods["pre"]["start"]), int(periods["post"]["end"])
        bars = [(stamp, close) for stamp, close in zip(result.get("timestamp") or [],
                                                         result["indicators"]["quote"][0]["close"])
                if close is not None]
        stamp, price = bars[-1]
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError(f"Yahoo Finance returned no extended-hours bars for {symbol}") from None
    as_time = lambda seconds: pd.Timestamp(seconds, unit="s", tz="UTC")
    return float(price), as_time(stamp), as_time(start), as_time(end)
