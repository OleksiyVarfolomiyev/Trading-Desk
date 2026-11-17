"""Phone push for new desk alerts, through any ntfy-style endpoint: ``DESK_PUSH_URL`` (for example a private topic on
ntfy.sh or a self-hosted ntfy). Unset, nothing is sent. The text is already masked (wallets show their last four)."""

from __future__ import annotations

import os
import threading
import time
import urllib.request

MIN_GAP_SECONDS = 20   # a burst of alerts becomes a few pushes, not a buzzing phone
_last = 0.0
_lock = threading.Lock()


def url() -> str | None:
    return (os.environ.get("DESK_PUSH_URL") or "").strip() or None


def _post(endpoint: str, title: str, text: str) -> bool:
    request = urllib.request.Request(endpoint, data=text.encode("utf-8"), method="POST",
                                     headers={"Title": title.encode("ascii", "ignore").decode() or "Desk", "Priority": "default"})
    try:
        with urllib.request.urlopen(request, timeout=8):
            return True
    except Exception:
        return False


def send(title: str, text: str) -> bool:
    """Queue one push (a background thread, never blocking the daemon); False when unset or sent too soon after another."""
    global _last
    endpoint = url()
    if not endpoint:
        return False
    with _lock:
        if time.time() - _last < MIN_GAP_SECONDS:
            return False
        _last = time.time()
    threading.Thread(target=_post, args=(endpoint, title, text[:400]), daemon=True).start()
    return True
