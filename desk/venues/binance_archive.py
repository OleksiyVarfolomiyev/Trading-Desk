"""Binance USDT-margined futures history from the public archive (data.binance.vision): free, years deep.

HL serves only its last 5,000 candles (about 17 days of 5-minute bars), too few to test intraday rules such as the
momentum burst at t >= 2 in both halves. Binance's archive keeps every month since each contract listed: klines at
any interval (with taker-buy volume) and the funding rate. Files are cached as downloaded under
``data/binance/`` (git-ignored) and read with pandas; a month already on disk is never fetched again, the current
month comes from daily files, which the archive publishes the next day.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data.binance.vision/data/futures/um"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
from desk.cache import cache_dir  # noqa: E402

CACHE = cache_dir("binance")
KLINE_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
                 "taker_buy_volume", "taker_buy_quote_volume", "ignore"]


def _fetch(url: str, path: Path, session: requests.Session) -> Path | None:
    """The archive file at ``url``, cached at ``path``; None when the archive has no such file (not listed yet)."""
    if path.exists():
        return path if path.stat().st_size else None
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            response = session.get(url, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if response.status_code == 404:
            return None
        if response.ok:
            path.write_bytes(response.content)
            return path
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Binance archive: could not fetch {url}")


def _read(path: Path, columns: list[str] | None) -> pd.DataFrame:
    """One archive zip as a frame; older files have no header row, newer ones do."""
    with zipfile.ZipFile(path) as archive:
        raw = archive.read(archive.namelist()[0])
    first = raw.split(b"\n", 1)[0]
    has_header = bool(re.search(rb"[a-z]", first))
    frame = pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)
    if columns is not None and not has_header:
        frame.columns = columns[: len(frame.columns)]
    return frame


def _months(start: dt.date, end: dt.date) -> list[dt.date]:
    months, month = [], start.replace(day=1)
    while month <= end:
        months.append(month)
        month = (month + dt.timedelta(days=32)).replace(day=1)
    return months


def _files(kind: str, symbol: str, start: dt.date, end: dt.date, interval: str = "") -> list[Path]:
    """Monthly files for every finished month in [start, end], daily files for the current month (to yesterday)."""
    today = dt.datetime.now(dt.timezone.utc).date()
    this_month = today.replace(day=1)
    sub = f"{symbol}/{interval}" if interval else symbol
    stem = f"{symbol}-{interval}" if interval else f"{symbol}-{kind}"
    paths = []
    with requests.Session() as session:
        for month in _months(start, min(end, today)):
            if month < this_month:
                name = f"{stem}-{month:%Y-%m}.zip"
                path = _fetch(f"{BASE}/monthly/{kind}/{sub}/{name}", CACHE / "monthly" / kind / sub / name, session)
                if path:
                    paths.append(path)
                continue
            if kind != "klines":
                continue   # funding has no daily files; the month lands once it is over
            day = month
            while day < today and day <= end:
                name = f"{stem}-{day:%Y-%m-%d}.zip"
                path = CACHE / "daily" / kind / sub / name
                if day >= today - dt.timedelta(days=2) and path.exists() and not path.stat().st_size:
                    path.unlink()   # yesterday's file may not have been published at the last try
                found = _fetch(f"{BASE}/daily/{kind}/{sub}/{name}", path, session)
                if found is None and not path.exists():
                    path.write_bytes(b"")   # remember the gap so it is not requested again
                if found:
                    paths.append(found)
                day += dt.timedelta(days=1)
    return paths


def klines(symbol: str, interval: str, start: str | dt.date, end: str | dt.date | None = None) -> pd.DataFrame:
    """Klines for ``symbol`` (e.g. "BTCUSDT") at ``interval`` ("5m", "1h", "1d") from ``start`` to ``end``,
    indexed by the bar's open time (UTC): open, high, low, close, volume, quote_volume, taker_buy_quote_volume."""
    start_date = pd.Timestamp(start).date()
    end_date = pd.Timestamp(end).date() if end is not None else dt.datetime.now(dt.timezone.utc).date()
    frames = [_read(path, KLINE_COLUMNS) for path in _files("klines", symbol, start_date, end_date, interval)]
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "quote_volume",
                                     "taker_buy_quote_volume"])
    frame = pd.concat(frames, ignore_index=True)
    frame["open_time"] = pd.to_numeric(frame["open_time"])
    frame = frame.drop_duplicates("open_time").sort_values("open_time")
    frame.index = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    frame.index.name = "time"
    keep = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_quote_volume"]
    frame = frame[keep].astype(float)
    return frame.loc[pd.Timestamp(start_date, tz="UTC"): pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)]


def funding(symbol: str, start: str | dt.date, end: str | dt.date | None = None) -> pd.Series:
    """The funding rate per payment (a fraction, positive: longs pay) for ``symbol``, indexed by payment time."""
    start_date = pd.Timestamp(start).date()
    end_date = pd.Timestamp(end).date() if end is not None else dt.datetime.now(dt.timezone.utc).date()
    frames = [_read(path, ["calc_time", "funding_interval_hours", "last_funding_rate"])
              for path in _files("fundingRate", symbol, start_date, end_date)]
    if not frames:
        return pd.Series(dtype=float, name="funding")
    frame = pd.concat(frames, ignore_index=True).drop_duplicates("calc_time").sort_values("calc_time")
    series = pd.Series(frame["last_funding_rate"].astype(float).values,
                       index=pd.to_datetime(frame["calc_time"].astype("int64"), unit="ms", utc=True), name="funding")
    return series


def symbols() -> list[str]:
    """Every USDT-margined futures symbol the archive holds klines for (listed or delisted)."""
    found, marker = [], ""
    prefix = "data/futures/um/monthly/klines/"
    with requests.Session() as session:
        while True:
            params = {"delimiter": "/", "prefix": prefix}
            if marker:
                params["marker"] = marker
            text = session.get(LISTING, params=params, timeout=60).text
            page = re.findall(r"<Prefix>" + re.escape(prefix) + r"([^<]+)/</Prefix>", text)
            found += page
            if "<IsTruncated>true</IsTruncated>" not in text or not page:
                break
            marker = prefix + page[-1] + "/"
    return sorted(set(found))
