"""Tiles from notebooks onto the dashboard: publish a chart or a table, say why it matters, and it appears at the top of
the dashboard (local and web) as a tile that ranks with the built-in ones by relevance, until it expires or is hidden.

    from desk.tile_feed import publish
    publish(fig, "ETH ladder fills", reason="first 100 fills are in: spread against markout", relevance=0.9, hours=48)
    publish(frame, "Closes by book", reason="stocks longs lose", formats={"Return": "pct", "PnL": "usd"})

A figure is Plotly's (its data and axes are kept; the dashboard supplies the theme, so the tile follows the look
chosen); a table is a DataFrame. Tiles are files in ~/.trading-desk/tiles (the local dashboard reads them) and are
pushed to the web relay (``/ingest/tiles``) when the Mac holds the relay token. ``withdraw(key)`` removes one.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

TILES_DIR = Path.home() / ".trading-desk" / "tiles"
MAX_BYTES = 400_000                  # the relay refuses larger bodies
DROPPED_LAYOUT = ("template", "title", "paper_bgcolor", "plot_bgcolor", "font", "width", "height", "margin", "autosize")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "tile"


def _chart(figure) -> dict:
    import plotly.io as pio

    document = json.loads(pio.to_json(figure))
    layout = {key: value for key, value in (document.get("layout") or {}).items() if key not in DROPPED_LAYOUT}
    return {"kind": "chart", "data": document.get("data", []), "layout": layout}


def _table(frame, formats: dict[str, str] | None) -> dict:
    import numpy as np
    import pandas as pd

    formats = formats or {}
    columns = []
    for name in frame.columns:
        numeric = pd.api.types.is_numeric_dtype(frame[name])
        columns.append({"key": str(name), "fmt": formats.get(str(name), "num" if numeric else "text")})
    rows = []
    for record in frame.to_dict("records"):
        rows.append({str(key): (None if (isinstance(value, float) and not np.isfinite(value)) else
                                (value.item() if hasattr(value, "item") else str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value))
                     for key, value in record.items()})
    return {"kind": "table", "columns": columns, "rows": rows}


def publish(content, title: str, reason: str = "", relevance: float = 0.7, hours: float = 24.0, key: str | None = None,
            formats: dict[str, str] | None = None, push: bool = True, directory: Path | None = None,
            attention: list | None = None, era: str | None = None) -> dict:
    """Put a Plotly figure or a DataFrame on the dashboard. ``relevance`` (0 to 1) is how much it matters now: it ranks
    against the built-in tiles; ``reason`` says why it is shown; it expires after ``hours``. Publishing the same ``key``
    again replaces the tile. ``attention`` rows ([title, text]) are added to both dashboards' attention list while the
    tile is live. ``era`` ("managed" or "inception") says which analytics window the tile belongs to: the page shows it
    only under that choice, and so only that choice's attention rows."""
    directory = directory or TILES_DIR
    body = _chart(content) if hasattr(content, "to_plotly_json") or hasattr(content, "to_dict") and hasattr(content, "layout") \
        else _table(content, formats)
    now = time.time()
    tile = {"key": key or _slug(title), "title": title, "reason": reason, "relevance": max(0.0, min(1.0, float(relevance))),
            "created": now, "expires": now + hours * 3600, **body}
    if era:
        tile["era"] = era
    if attention:
        tile["attention"] = [[str(title), str(text)] + ([era] if era else []) for title, text in attention]
    text = json.dumps(tile)
    if len(text) > MAX_BYTES:
        raise ValueError(f"the tile is {len(text) // 1000} KB; the limit is {MAX_BYTES // 1000} KB (fewer points or rows)")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{tile['key']}.json").write_text(text)
    if push:
        push_to_web(directory)
    return {"key": tile["key"], "bytes": len(text)}


def tiles(directory: Path | None = None, now: float | None = None) -> list[dict]:
    """The tiles that have not expired, newest first (expired files are removed)."""
    directory = directory or TILES_DIR
    now = time.time() if now is None else now
    found = []
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            tile = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if float(tile.get("expires", 0)) < now:
            path.unlink(missing_ok=True)
            continue
        found.append(tile)
    return sorted(found, key=lambda tile: -float(tile.get("created", 0)))


def withdraw(key: str, push: bool = True, directory: Path | None = None) -> bool:
    directory = directory or TILES_DIR
    path = directory / f"{key}.json"
    existed = path.exists()
    path.unlink(missing_ok=True)
    if push:
        push_to_web(directory)
    return existed


def push_to_web(directory: Path | None = None) -> bool:
    """Send the current tiles to the web relay (replacing what it holds); False when the Mac has no relay token."""
    from desk import web_sync

    try:
        web_sync._post("/ingest/tiles", {"tiles": tiles(directory)})
        return True
    except Exception as exc:
        print(f"tile feed: not sent to the web dashboard ({str(exc)[:120]}); the local dashboard has it")
        return False
