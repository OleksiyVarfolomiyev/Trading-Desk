"""Where the desk keeps its re-downloadable caches (HL candles, Yahoo closes, the Binance archive).

Caches are rewritten constantly. Inside a Dropbox folder every rewrite is watched, hashed and synced, which slows the
notebooks and floods the sync queue with files that can be fetched again. So on a Mac whose project sits in Dropbox
the caches live on the local disk (``~/Library/Caches/trading-desk``); anywhere else (the desk server) they stay under
the project's ``data/``. Account records (value history, fills, wallet records) are small, matter, and stay in
``data/``, where Dropbox keeps them backed up. ``DESK_CACHE_DIR`` overrides the choice.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = Path.home() / "Library" / "Caches" / "trading-desk"


def cache_root() -> Path:
    """The folder that holds the caches: ``DESK_CACHE_DIR``, else the local cache folder when the project is inside
    Dropbox, else the project's own ``data/``."""
    chosen = os.environ.get("DESK_CACHE_DIR")
    if chosen:
        return Path(chosen).expanduser()
    return LOCAL if "dropbox" in str(ROOT).lower() else ROOT / "data"


def cache_dir(name: str) -> Path:
    """A named cache folder (``api_cache``, ``binance``), moved over from the project's ``data/`` the first time it is
    asked for from the local cache folder (a rename on the same disk is instant; across disks it is copied)."""
    target = cache_root() / name
    old = ROOT / "data" / name
    if target != old and old.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(old), str(target))
        except OSError:
            return old   # could not move it: keep using it where it is
    return target
