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
