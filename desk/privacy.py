"""Account identifiers on the desk: only the last four characters are ever shown or saved.

The full wallet addresses and IB account numbers live in ``accounts.json`` (local, never in git)
because the desk needs them to read the accounts. Everything else uses the last four
characters: file names (``hl_account_history_07f2.csv``), tables, messages and logs
(``...07f2``).

* ``short_id`` gives the four characters.
* ``account_file`` names a per-account data file, and renames a file still carrying the full
  identifier the first time it is used.
* ``mask`` rewrites text that may hold a full identifier (an error from an API, a log line);
  ``mask_output`` applies it to everything a script prints.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

# A 20-byte EVM address (not part of a longer hex string such as a transaction hash).
_ADDRESS = re.compile(r"(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{36}([0-9a-fA-F]{4})(?![0-9a-fA-F])")
# IB account numbers: U1234567, paper DU1234567.
_IB_ACCOUNT = re.compile(r"\b(?:DU|U)\d{3,}(\d{4})\b")


def short_id(identifier: str) -> str:
    """The last four characters of a wallet address or account number, lower case."""
    return str(identifier).strip()[-4:].lower()


def shown(identifier: str) -> str:
    """How an identifier appears in text: ``...07f2``."""
    return "..." + short_id(identifier)


def mask(text: str) -> str:
    """Replace every full wallet address and IB account number in ``text`` with ``...`` and its last four."""
    if not isinstance(text, str) or not text:
        return text
    text = _ADDRESS.sub(lambda match: "..." + match.group(1).lower(), text)
    return _IB_ACCOUNT.sub(lambda match: "..." + match.group(1), text)


def account_file(directory: Path | str, prefix: str, identifier: str, suffix: str = ".csv") -> Path:
    """``<directory>/<prefix>_<last four><suffix>``; a file still named with the full identifier is renamed to it."""
    directory = Path(directory)
    path = directory / f"{prefix}_{short_id(identifier)}{suffix}"
    if not path.exists():
        for legacy_name in dict.fromkeys((str(identifier), str(identifier).lower())):
            legacy = directory / f"{prefix}_{legacy_name}{suffix}"
            if legacy != path and legacy.exists():
                legacy.rename(path)
                break
    return path


def check_unique(identifiers: list[str], what: str = "accounts") -> None:
    """Refuse two identifiers sharing their last four characters: their files would collide."""
    seen: dict[str, str] = {}
    for identifier in identifiers:
        key = short_id(identifier)
        if key in seen and seen[key].lower() != str(identifier).lower():
            raise ValueError(f"two {what} end in {key!r}; the desk names files and labels by the last four "
                             "characters, so rename one account in accounts.json or use another wallet")
        seen[key] = str(identifier)


class MaskedStream(io.TextIOBase):
    """A text stream that masks full identifiers before writing to the stream it wraps."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, text: str) -> int:
        self.stream.write(mask(text))
        return len(text)

    def flush(self) -> None:
        self.stream.flush()

    def isatty(self) -> bool:
        return self.stream.isatty()

    def fileno(self) -> int:  # faulthandler and subprocesses write to the real stream
        return self.stream.fileno()

    @property
    def encoding(self):  # some libraries read it
        return getattr(self.stream, "encoding", "utf-8")


def mask_output() -> None:
    """Mask full identifiers in everything this process prints (stdout and stderr)."""
    if not isinstance(sys.stdout, MaskedStream):
        sys.stdout = MaskedStream(sys.stdout)
    if not isinstance(sys.stderr, MaskedStream):
        sys.stderr = MaskedStream(sys.stderr)
