"""Problems the trading daemon reports, for the dashboards' attention list.

The daemon (execution/hl_daemon.py) keeps the current alerts in ``~/.trading-desk/daemon_alerts.json``
with a heartbeat, and sends them to the web relay; the dashboards turn them into "Desk alert" rows.
An old heartbeat is itself an alert: the daemon has stopped, so nothing is guarded or quoted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

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
        return [("Desk alert: trading daemon not reporting",
                 f"No word from the trading daemon for {age / 60:.0f} minutes: guards, the margin guard and the ladders "
                 "are not running. Start it: .venv/bin/python execution/hl_daemon.py serve")]
    return [(f"Desk alert: {item['title']}", f"{item['text']} (since {item.get('since', '?')[:16].replace('T', ' ')} UTC)")
            for item in state.get("alerts", []) if float(item.get("expires", 0) or 0) == 0 or float(item["expires"]) > now]
