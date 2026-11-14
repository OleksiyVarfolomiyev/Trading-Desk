"""Market context beyond price: the mood, the crowding, hacks and the news around a big move.

None of it predicts: price moves first and the news follows. It is there so that when an alert fires (an alt
rally, a stock dip), the operator can see why the market moved and judge whether the move should last (a major
hack reprices trust for weeks) or fade (a regulatory headline can be a one-day pop). Every big-move alert also
logs its context to EVENTS_FILE, so the desk can later measure how each kind of event played out.

* **Mood:** the Crypto Fear & Greed index (alternative.me, daily, free).
* **Crowding:** how many of the 40 most traded alts have shorts paying funding on HL right now.
* **Hacks:** DefiLlama's hacks list (free, updated within the day). Above HACK_ALERT_USD the trading daemon raises
  a "Hack" alert; above MAJOR_HACK_USD it is critical.
* **News:** the headlines of four crypto news feeds (RSS, free) from the hours around the move.

Token unlocks are not here: DefiLlama's unlock calendar is a paid API.
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
EVENTS_FILE = ROOT / "data" / "market_events.jsonl"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
HACKS_URL = "https://api.llama.fi/hacks"
NEWS_FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "The Block": "https://www.theblock.co/rss.xml",
    "Decrypt": "https://decrypt.co/feed",
}
USER_AGENT = "trading-desk/1.0 (read-only market context)"
HACK_ALERT_USD = 50_000_000
MAJOR_HACK_USD = 200_000_000
HACK_WINDOW_HOURS = 48
NEWS_HOURS = 24
CROWD_COUNT = 40
_cache: dict[str, tuple[float, object]] = {}


def _get(url: str, ttl: float):
    """A GET answer, kept for ``ttl`` seconds (the feeds change slowly; the daemon asks often)."""
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    response.raise_for_status()
    value = response.json() if response.text.lstrip()[:1] in ("[", "{") else response.text
    _cache[url] = (time.time(), value)
    return value


# ------------------------------------------------------------------ the sources
def fear_greed() -> tuple[int, str] | None:
    """The Crypto Fear & Greed index now: (0 to 100, "Extreme Fear" ... "Extreme Greed"), or None."""
    try:
        item = _get(FEAR_GREED_URL, 3600)["data"][0]
        return int(item["value"]), str(item["value_classification"])
    except Exception:
        return None


def recent_hacks(hours: float = HACK_WINDOW_HOURS, min_usd: float = HACK_ALERT_USD, now: float | None = None) -> list[dict]:
    """Hacks DefiLlama logged in the last ``hours`` of at least ``min_usd``, largest first."""
    now = time.time() if now is None else now
    try:
        items = _get(HACKS_URL, 1800)
    except Exception:
        return []
    found = [item for item in items if now - float(item.get("date") or 0) <= hours * 3600
             and float(item.get("amount") or 0) >= min_usd]
    return sorted(found, key=lambda item: -float(item.get("amount") or 0))


def crowding(meta: dict, contexts: list[dict], count: int = CROWD_COUNT) -> tuple[int, int]:
    """Of the ``count`` most traded main-DEX alts (as the alt rally basket), how many have shorts paying funding."""
    from desk.alt_rally import EXCLUDED

    rows = []
    for asset, context in zip(meta.get("universe", []), contexts):
        name = str(asset.get("name", ""))
        if asset.get("isDelisted") or name in EXCLUDED or name.startswith("k"):
            continue
        try:
            rows.append((float(context.get("dayNtlVlm") or 0), float(context.get("funding") or 0)))
        except (TypeError, ValueError):
            continue
    top = sorted(rows, reverse=True)[:count]
    return sum(rate < 0 for _, rate in top), len(top)


def headlines(hours: float = NEWS_HOURS, now: float | None = None, limit: int = 12) -> list[dict]:
    """The news feeds' headlines from the last ``hours``, newest first: {time, source, title, link}."""
    now = time.time() if now is None else now
    found, seen = [], set()
    for source, url in NEWS_FEEDS.items():
        try:
            text = _get(url, 900)
            root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
        except Exception:
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            try:
                when = parsedate_to_datetime(item.findtext("pubDate") or "").timestamp()
            except (TypeError, ValueError):
                continue
            key = re.sub(r"\W+", " ", title.lower()).strip()
            if not title or key in seen or now - when > hours * 3600 or when > now + 600:
                continue
            seen.add(key)
            found.append({"time": when, "source": source, "title": title, "link": (item.findtext("link") or "").strip()})
    return sorted(found, key=lambda item: -item["time"])[:limit]


# ------------------------------------------------------------------ for the alerts
def _usd(value: float) -> str:
    from desk.numbers import usd

    return usd(value)


def summary(mood: tuple[int, str] | None, crowd: tuple[int, int] | None, hacks: list[dict]) -> str:
    """One line of context for an alert: "mood extreme fear 20; shorts pay on 12 of 40 alts; hack: Bybit $1.4B"."""
    parts = []
    if mood:
        parts.append(f"mood {mood[1].lower()} {mood[0]}")
    if crowd and crowd[1]:
        parts.append(f"shorts pay on {crowd[0]} of {crowd[1]} alts")
    if hacks:
        top = hacks[0]
        parts.append(f"hack: {top.get('name')} {_usd(float(top.get('amount') or 0))}")
    return "; ".join(parts)


def hover(items: list[dict], now: float | None = None) -> str:
    """The headlines as hover text, one per line: "3 h · The Block · Bitcoin jumps as ..."."""
    now = time.time() if now is None else now
    lines = [f"{max(0, int((now - item['time']) // 3600))} h · {item['source']} · {item['title']}" for item in items]
    return "\n".join(lines) if lines else "No headlines in the news feeds for this window."


def hack_alert(hack: dict, now: float | None = None) -> tuple[str, str]:
    """(title, text) for a hack: "Major hack" (critical) from MAJOR_HACK_USD, else "Hack"; the signal first."""
    now = time.time() if now is None else now
    amount = float(hack.get("amount") or 0)
    chains = ", ".join(hack.get("chain") or []) or "chain unknown"
    kind = hack.get("classification") or hack.get("technique") or ""
    age = max(0, int((now - float(hack.get("date") or now)) // 3600))
    title = "Major hack" if amount >= MAJOR_HACK_USD else "Hack"
    text = f"{hack.get('name')} {_usd(amount)} ({chains}{'; ' + kind if kind else ''}), {age} h ago"
    return title, text


def record_event(kind: str, move: float, context: dict, path: Path | None = None) -> None:
    """Append one big-move event with its context (desk/event_watch.py keeps the log and its debriefs)."""
    from desk import event_watch

    try:
        event_watch.record(kind, {"move": move, **context}, context.get("prices") or {}, path=path)
    except OSError:
        pass
