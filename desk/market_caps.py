"""Market cap and 24h volume of every asset the desk can trade, fetched once a day and kept for the whole desk.

One bulk CoinGecko request per page of 250 coins (four pages, a few seconds) replaces one request per coin per run.
The rows go to ``data/market_caps.csv`` (date, symbol, market_cap, volume), one set per UTC day, for every HL-listed
perp that CoinGecko lists plus BTC and ETH. ``refresh`` does nothing when today is already saved, so any caller may
ask: the Mac's daily job, the notebooks, the screener.

    from desk import market_caps
    market_caps.refresh()                 # at most one fetch a day
    market_caps.latest()                  # symbol -> market_cap, volume, as of the last saved day
    market_caps.history("BTC")            # date -> market_cap, volume
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "data" / "market_caps.csv"
COLUMNS = ["date", "symbol", "market_cap", "volume"]
URL = "https://api.coingecko.com/api/v3/coins/markets"
PAGES = 4            # the top 1000 coins by market cap
PAUSE_SECONDS = 2.6  # the public API allows about one request every 2.5 seconds


def gecko_symbol(name: str) -> str:
    """An HL market name as CoinGecko writes the symbol: no dex prefix, no k or 1000 multiplier (kBONK -> BONK)."""
    base = str(name).split(":")[-1]
    if base.startswith("1000") and len(base) > 4:
        base = base[4:]
    elif len(base) > 1 and base[0] == "k" and base[1:].isupper():
        base = base[1:]
    return base.upper()


def hl_symbols() -> set[str]:
    """CoinGecko symbols of every HL perp (the desk's tradable universe), plus BTC and ETH."""
    from desk.hl.trade_analytics import post_info

    meta = post_info({"type": "meta"})
    return {gecko_symbol(item["name"]) for item in meta.get("universe", [])} | {"BTC", "ETH"}


def fetch_top(pages: int = PAGES, get=None, pause: float = PAUSE_SECONDS) -> dict[str, tuple[float, float]]:
    """{symbol: (market cap, 24h volume)} of the top coins; where several coins share a symbol, the largest wins."""
    import requests

    get = get or requests.get
    found: dict[str, tuple[float, float]] = {}
    for page in range(1, pages + 1):
        response = get(URL, params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page,
                                    "sparkline": "false"}, timeout=30)
        if not response.ok:
            raise RuntimeError(f"CoinGecko markets page {page} failed (HTTP {response.status_code})")
        for coin in response.json():
            symbol = str(coin.get("symbol", "")).upper()
            cap = float(coin.get("market_cap") or 0.0)
            if symbol and cap > 0 and (symbol not in found or cap > found[symbol][0]):
                found[symbol] = (cap, float(coin.get("total_volume") or 0.0))
        if page < pages:
            time.sleep(pause)
    return found


def load(path: Path = FILE) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):
        return pd.DataFrame(columns=COLUMNS)
    frame["date"] = pd.to_datetime(frame["date"], utc=True).dt.normalize()
    return frame


def refresh(today: pd.Timestamp | None = None, path: Path = FILE, force: bool = False, symbols: set[str] | None = None,
            fetch=None) -> int:
    """Save today's market cap and volume for the desk's universe. Returns the rows added (0 when today is saved)."""
    day = (today or pd.Timestamp.now(tz="UTC")).normalize()
    saved = load(path)
    if not force and len(saved) and saved["date"].max() >= day:
        return 0
    wanted = symbols if symbols is not None else hl_symbols()
    top = (fetch or fetch_top)()
    rows = [{"date": day.strftime("%Y-%m-%d"), "symbol": symbol, "market_cap": cap, "volume": volume}
            for symbol, (cap, volume) in sorted(top.items()) if symbol in wanted]
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows, columns=COLUMNS)
    keep = saved[saved["date"] != day] if len(saved) else saved
    out = pd.concat([keep.assign(date=keep["date"].dt.strftime("%Y-%m-%d")) if len(keep) else keep, new], ignore_index=True)
    out.to_csv(path, index=False)
    return len(new)


def latest(path: Path = FILE) -> pd.DataFrame:
    """Each symbol's market cap and volume on the last saved day, indexed by symbol."""
    frame = load(path)
    if frame.empty:
        return pd.DataFrame(columns=["market_cap", "volume"])
    return frame[frame["date"] == frame["date"].max()].set_index("symbol")[["market_cap", "volume"]]


def history(symbol: str, path: Path = FILE) -> pd.DataFrame:
    """One symbol's saved days: date -> market_cap, volume."""
    frame = load(path)
    return frame[frame["symbol"] == gecko_symbol(symbol)].set_index("date")[["market_cap", "volume"]].sort_index()
