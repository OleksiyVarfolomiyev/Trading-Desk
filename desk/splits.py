"""Stock splits on tradfi perps: price history put on today's scale.

A HIP-3 equity perp (xyz:SNDK, para:SOFI, io:IONQ ...) follows its stock, so when the stock splits (SanDisk 10-for-1 on
2026-11-20) the market's price is re-based at once while its history, candles and the 24h reference price are not. That
reads as a 90% fall: the open positions tile showed SNDK at -90%, and the daily ATR (from candles on the old scale) was
larger than the new price, so the planner refused to size the trade.

* ``snap(ratio)`` says whether a price ratio is a clean split: 1/N (a forward split) or N (a reverse split) for the usual
  N. Small N must be exact (3%): a real -45% crash is not a 2-for-1 split. Large N may be off by 20%, because the stock also
  moves on the day (SNDK read 1/9.55 against its price 24 hours before the split).
* ``detect(frame)`` finds splits inside a daily candle series (a close or an open at a clean ratio of the close before).
* ``observe`` records a split seen live (the mark against the 24h price or the last completed close), because the candle
  series has no post-split day yet; ``apply`` puts a candle frame on the new scale using both.
* Crypto (the main DEX, dex "") is never touched: a -50% crypto day is real.

The record is ``splits.json`` next to the candle cache, so the daemon, the books and the page agree.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

RATIOS = (2, 3, 4, 5, 10, 20, 50)   # the usual splits; fewer ratios keep a same-day move from reading as another one (1/9 is 1/10, not 1/8)
SAME_DAY_BAND = 0.20   # N >= 5: the stock also moves on the split day
EXACT_BAND = 0.03      # N < 5: only an exact ratio (a crash of 45-50% is not a 2-for-1 split)
MATCH_DAYS = 3         # a recorded split and a detected one within this many days are the same split


@dataclass(frozen=True)
class Split:
    date: str       # the first day on the new scale (UTC, YYYY-MM-DD)
    factor: float   # new price over old price: 0.1 for 10-for-1, 10 for 1-for-10

    @property
    def text(self) -> str:   # "1:10" (one old share became ten), "10:1" for a reverse split
        return f"1:{round(1 / self.factor)}" if self.factor < 1 else f"{round(self.factor)}:1"


def tolerance(n: int) -> float:
    return EXACT_BAND if n < 5 else SAME_DAY_BAND


def snap(ratio: float) -> float | None:
    """The clean split factor a price ratio is, or None."""
    if not ratio or ratio <= 0 or ratio != ratio:
        return None
    best, error = None, None
    for n in RATIOS:
        for factor in (1 / n, float(n)):
            miss = abs(ratio / factor - 1)
            if miss <= tolerance(n) and (error is None or miss < error):
                best, error = factor, miss
    return best


def is_tradfi(dex: str | None) -> bool:
    return bool(dex)


def detect(frame: pd.DataFrame) -> list[Split]:
    """Splits inside a daily candle frame (columns time, open, close), oldest first."""
    if frame is None or len(frame) < 2:
        return []
    closes = frame["close"].astype(float).tolist()
    opens = frame["open"].astype(float).tolist() if "open" in frame else closes
    times = pd.to_datetime(frame["time"], utc=True).tolist()
    found = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        factor = snap(closes[i] / closes[i - 1]) or (snap(opens[i] / closes[i - 1]) if opens[i] > 0 else None)
        if factor:
            found.append(Split(times[i].strftime("%Y-%m-%d"), factor))
    return found


# ---------------------------------------------------------------- the record of splits seen live
_MEMORY: dict[str, list[dict]] = {}


def _path() -> Path | None:
    if os.environ.get("HL_DISABLE_CACHE"):
        return None
    try:
        from desk.hl.trade_analytics import CACHE_DIR
    except Exception:
        return None
    return Path(CACHE_DIR) / "splits.json" if CACHE_DIR else None


def _key(coin: str, dex: str) -> str:
    return f"{dex}:{str(coin).split(':')[-1].upper()}"


def _load() -> dict[str, list[dict]]:
    path = _path()
    if path is None:
        return _MEMORY
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save(record: dict[str, list[dict]]) -> None:
    path = _path()
    if path is None:
        return
    try:
        path.write_text(json.dumps(record, indent=1, sort_keys=True))
    except OSError:
        pass


def recorded(coin: str, dex: str) -> list[Split]:
    return [Split(item["date"], float(item["factor"])) for item in _load().get(_key(coin, dex), [])]


def observe(coin: str, dex: str, mark: float, *references: float, today: pd.Timestamp | None = None) -> Split | None:
    """Record a split the live price shows: the mark against the price 24h ago or the last completed close."""
    if not is_tradfi(dex) or not mark or mark <= 0:
        return None
    for reference in references:
        if not reference or reference <= 0:
            continue
        factor = snap(mark / reference)
        if factor:
            day = (today or pd.Timestamp.now(tz="UTC")).strftime("%Y-%m-%d")
            record = _load()
            known = record.setdefault(_key(coin, dex), [])
            if not any(abs((pd.Timestamp(item["date"]) - pd.Timestamp(day)).days) <= MATCH_DAYS for item in known):
                known.append({"date": day, "factor": factor})
                _save(record)
            return Split(day, factor)
    return None


def adjusted_previous(coin: str, dex: str, mark: float, previous: float) -> float:
    """The price 24h ago on today's scale (HL's prevDayPx is not split-adjusted), unchanged for crypto and for no split."""
    if not is_tradfi(dex) or not previous or previous <= 0 or not mark or mark <= 0:
        return previous
    factor = snap(mark / previous)
    if not factor:
        return previous
    observe(coin, dex, mark, previous)
    return previous * factor


def _merge(found: list[Split], seen: list[Split]) -> list[Split]:
    """Detected splits, plus the recorded ones the candles do not show yet (a recorded split within MATCH_DAYS of a
    detected one is the same split)."""
    out = list(found)
    for item in seen:
        if not any(abs((pd.Timestamp(item.date) - pd.Timestamp(other.date)).days) <= MATCH_DAYS for other in found):
            out.append(item)
    return sorted(out, key=lambda split: split.date)


def splits_for(frame: pd.DataFrame, coin: str, dex: str) -> list[Split]:
    if not is_tradfi(dex):
        return []
    return _merge(detect(frame), recorded(coin, dex))


def apply(frame: pd.DataFrame, coin: str, dex: str, mark: float | None = None) -> pd.DataFrame:
    """The candle frame on today's scale: every row before a split is multiplied by its factor (volume divided).
    With ``mark`` the last completed close is also checked against it (a split today leaves no post-split candle yet)."""
    if frame is None or frame.empty or not is_tradfi(dex):
        return frame
    if mark and mark > 0:
        observe(coin, dex, mark, float(frame["close"].iloc[-1]))
    applied = set(frame.attrs.get("splits_applied", ()))   # a frame already on the new scale is not adjusted twice
    found = [split for split in splits_for(frame, coin, dex) if f"{split.date}:{split.factor}" not in applied]
    if not found:
        return frame
    out = frame.copy()
    out.attrs["splits_applied"] = sorted(applied | {f"{split.date}:{split.factor}" for split in found})
    times = pd.to_datetime(out["time"], utc=True)
    price_columns = [column for column in ("open", "high", "low", "close") if column in out]
    for split in found:
        before = times < pd.Timestamp(split.date, tz="UTC")
        for column in price_columns:
            out.loc[before, column] = out.loc[before, column].astype(float) * split.factor
        if "volume" in out:
            out.loc[before, "volume"] = out.loc[before, "volume"].astype(float) / split.factor
    return out


def active(coin: str, dex: str, days: int = 45) -> Split | None:
    """The most recent recorded split within ``days``, for the tile's quiet marker."""
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)
    mine = [split for split in recorded(coin, dex) if pd.Timestamp(split.date, tz="UTC") >= cutoff]
    return mine[-1] if mine else None
