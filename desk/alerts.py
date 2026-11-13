"""Problems the trading daemon reports, for the dashboards' attention list.

The daemon (execution/hl_daemon.py) keeps the current alerts in ``~/.trading-desk/daemon_alerts.json``
with a heartbeat, and sends them to the web relay; the dashboards turn them into attention rows titled by the alert.
An old heartbeat is itself an alert: the daemon has stopped, so nothing is guarded or quoted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

ALERTS_FILE = Path.home() / ".trading-desk" / "daemon_alerts.json"
STALE_SECONDS = 15 * 60


def read(path: Path = ALERTS_FILE) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def rows(state: dict | None, now: float | None = None) -> list[tuple[str, str]]:
    """(title, text) attention rows: each current alert, or a stopped daemon."""
    if not state:
        return []
    now = time.time() if now is None else now
    age = now - float(state.get("heartbeat", 0))
    if age > STALE_SECONDS:
        return [("Trading daemon not reporting",
                 f"No word from the trading daemon for {age / 60:.0f} minutes: guards, the margin guard and the ladders "
                 "are not running. Start it: .venv/bin/python execution/hl_daemon.py serve")]
    def eastern(stamp: str) -> str:
        try:
            return f"{pd.Timestamp(stamp, tz='UTC').tz_convert('America/New_York'):%Y-%m-%d %H:%M} ET"
        except (ValueError, TypeError):
            return "?"

    title = lambda text: text[:1].upper() + text[1:]  # "funding adverse" reads "Funding adverse"
    return [(title(item['title']), f"{item['text']} (since {eastern(item.get('since', ''))})")
            for item in state.get("alerts", []) if float(item.get("expires", 0) or 0) == 0 or float(item["expires"]) > now]
