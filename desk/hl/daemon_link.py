"""Talk to the HL trading daemon (``execution/hl_daemon.py``) over its local socket.

The daemon keeps HL account and market data live and holds the signing clients, so it answers
in milliseconds what a fresh read takes seconds and dozens of HL requests to learn. This module
has no third-party dependencies, so any desk process (the IB executor, the dashboard, a
notebook) can ask it, and falls back to its own reads when no daemon is running.

Protocol: one JSON request line; the daemon answers with JSON lines, each ``{"t": kind, ...}``:
``out`` (text to print), ``ask`` (a prompt to answer with ``{"t": "answer", "text": ...}``),
``data`` (a result) and finally ``end`` (with an exit ``code``).
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from desk.privacy import mask  # noqa: F401  (the daemon masks what it prints and sends)

RUN_DIR = Path(os.environ.get("HL_DAEMON_DIR") or Path.home() / ".trading-desk")
SOCKET_PATH = RUN_DIR / "hl_daemon.sock"


def connect(timeout: float | None = 2.0) -> socket.socket | None:
    """A connected socket to the daemon, or None when none is listening."""
    if not SOCKET_PATH.exists():
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(SOCKET_PATH))
    except OSError:
        sock.close()
        return None
    return sock


def send(sock: socket.socket, message: dict[str, Any]) -> None:
    sock.sendall((json.dumps(message) + "\n").encode())


def query(payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any] | None:
    """Send a data request and return its ``data`` answer; None when there is no usable answer."""
    sock = connect(timeout)
    if sock is None:
        return None
    try:
        send(sock, payload)
        data = None
        with sock.makefile("r", encoding="utf-8") as reader:
            for line in reader:
                message = json.loads(line)
                if message.get("t") == "data":
                    data = message.get("data")
                elif message.get("t") == "ask":  # nobody to type an answer: decline
                    send(sock, {"t": "answer", "text": ""})
                elif message.get("t") == "end":
                    return data if message.get("code") == 0 else None
    except (OSError, ValueError):
        return None
    finally:
        sock.close()
    return None


def hl_book(wallets: list[str], timeout: float = 2.0) -> dict[str, Any] | None:
    """Equity, open risk, margin and position count across HL wallets, from the daemon's live data.

    None when no daemon is running, it does not track every one of the wallets, or its data is
    stale; the caller then reads HL itself.
    """
    return query({"cmd": "book", "wallets": [wallet.lower() for wallet in wallets]}, timeout)
