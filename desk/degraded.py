"""Data that could not be built, shown as a chip in the dashboard's account status.

A fallback that quietly blanks a column (24-hour moves, funding, the stress profile) hides a failure.
Each such fallback calls ``report(name, error)``; the dashboard clears the list at the start of a refresh
and shows what was reported as chips next to the unreadable accounts. A later success needs no call.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_problems: dict[str, str] = {}


def report(name: str, error: BaseException | str) -> None:
    text = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    with _lock:
        _problems.setdefault(name, text)   # the first failure explains the rest


def clear() -> None:
    with _lock:
        _problems.clear()


def rows(seen: dict[str, str] | None = None, now: str = "") -> list[dict]:
    """Account-status rows for what failed in this refresh; ``seen`` remembers when each last worked."""
    with _lock:
        problems = dict(_problems)
    return [{"name": name, "venue": "data", "ok": False, "label": "failed", "problem": problem,
             "last_seen": (seen or {}).get(name)} for name, problem in problems.items()]
