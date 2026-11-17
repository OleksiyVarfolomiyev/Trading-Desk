"""The analytics a notebook can put on the dashboard: every section of desk_crypto.ipynb and desk_stocks.ipynb is a
candidate tile. The catalog is read from the notebooks themselves (a ``## Section`` heading and the ``book.show_...()``
call under it), so a new notebook section is offered by the dashboard with no other change.

``entries()`` needs only the standard library (the web build writes it to web/site/catalog.json). ``pull(id)`` runs the
section's call on a Book (the notebook's own setup), captures the charts and tables it would have drawn, and publishes
them as tiles (desk/tile_feed.py), local and web.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NOTEBOOKS = {"desk_crypto": "Crypto", "desk_stocks": "Stocks", "desk_overview": "Desk"}
# desk_overview is mostly tables the dashboard already has: only these sections are offered (its calls are ``overview.``).
DESK_SECTIONS = ("Exposure by asset class",)
# What each section answers, for matching against what the dashboard is flagging right now.
# Sections that change with the market (positions, exposure, risk, today's PnL) are live: they can sit at the top. The rest
# (closed-trade returns, factor models, tie-outs, performance history) are report material: they go to the bottom.
LIVE_SECTIONS = ("Positions", "Exposure", "Open risk", "PnL by market", "Market making", "Highlights", "Wallet tokens")
KEYWORDS = {"Performance": "return performance drawdown underperform", "Open risk": "risk cap margin leverage liquidation",
            "Exposure": "exposure beta leverage net gross", "Picks": "picks benchmark alpha selection", "Risk and beta": "beta risk volatility",
            "Factor model": "alpha factor beta", "PnL by market": "pnl loss winners losers market", "Returns on closed": "closed trades win loss",
            "Fills per day": "overtrading fills activity", "Market making": "ladder market making spread", "Wallet tokens": "wallet token",
            "Books tie out": "reconcile tie out", "Highlights": "alerts highlights", "Positions": "positions funding bounce drop momentum"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def entries() -> list[dict]:
    """[{id, notebook, book, title, call, description, keywords}] for every notebook section that has a ``book.`` call."""
    found = []
    for name, book in NOTEBOOKS.items():
        path = REPO / f"{name}.ipynb"
        if not path.exists():
            continue
        section, description = None, ""
        for cell in json.loads(path.read_text()).get("cells", []):
            text = "".join(cell.get("source", []))
            if cell["cell_type"] == "markdown" and text.startswith("## "):
                head, _, rest = text[3:].partition("\n")
                section, description = head.strip(), " ".join(rest.split())[:200]
            elif cell["cell_type"] == "code" and section and text.strip().startswith(("book.", "overview.")) \
                    and (name != "desk_overview" or section in DESK_SECTIONS):
                keywords = " ".join(words for key, words in KEYWORDS.items() if section.startswith(key))
                found.append({"id": f"{name}:{_slug(section)}", "notebook": name, "book": book, "title": f"{book}: {section}",
                              "section": section, "call": text.strip(), "description": description, "keywords": keywords,
                              "live": section.startswith(LIVE_SECTIONS)})
                section = None
    return found


# ---------------------------------------------------------------------------- pulling (the local dashboard runs this)
_BOOKS: dict[str, tuple[float, object]] = {}
STATUS: dict[str, str] = {}
BOOK_TTL = 15 * 60


def _book(name: str):
    """The notebook's own Book (its setup cell's ``Book(...)`` expression), kept for a few minutes between pulls."""
    cached = _BOOKS.get(name)
    if cached and time.time() - cached[0] < BOOK_TTL:
        return cached[1]
    if name == "desk_overview":
        from desk.overview import DeskOverview

        overview = DeskOverview(data_dir=str(REPO / "data"))
        overview.fetch_data()
        _BOOKS[name] = (time.time(), overview)
        return overview
    cells = json.loads((REPO / f"{name}.ipynb").read_text())["cells"]
    setup = next("".join(c["source"]) for c in cells if c["cell_type"] == "code" and "Book(" in "".join(c["source"]))
    expression = re.search(r"Book\(.*\)", setup).group(0)
    from desk.books import Book

    book = eval(expression, {"Book": Book})
    book = book.fetch_data() if hasattr(book, "fetch_data") and not expression.endswith("fetch_data()") else book
    _BOOKS[name] = (time.time(), book)
    return book


def capture(call: str, book) -> list[tuple[str, object]]:
    """Run one ``book.show_...()`` call and return what it would have drawn: [("chart", figure) | ("table", (title, frame))]."""
    from unittest import mock

    import desk.books
    import desk.charts
    import desk.hl.account_analytics as analytics

    out: list[tuple[str, object]] = []
    import desk.overview

    def table(frame, formatters=None, **kwargs):
        out.append(("table", (str(kwargs.get("title") or ""), frame, formatters or {})))

    with mock.patch.object(desk.charts, "show", lambda figure, *a, **k: out.append(("chart", figure))), \
            mock.patch.object(desk.books, "show_chart", lambda figure, *a, **k: out.append(("chart", figure))), \
            mock.patch.object(analytics, "display_interactive_table", table), \
            mock.patch.object(desk.overview, "display_interactive_table", table):
        eval(call, {"book": book, "overview": book})
    return out


def _formatted(frame, formatters: dict):
    """The frame as the notebook shows it: each formatter applied to its column, the rest left as they are."""
    shown = frame.copy()
    for column, function in formatters.items():
        if column in shown and callable(function):
            shown[column] = [function(value) if value == value else "" for value in shown[column]]
    return shown


def pull(entry_id: str) -> str:
    """Build the section's tiles from live data and publish them. Returns a one-line result; STATUS holds progress."""
    from desk import tile_feed

    entry = next((e for e in entries() if e["id"] == entry_id), None)
    if entry is None:
        return "unknown section"
    STATUS[entry_id] = "running"
    try:
        made = capture(entry["call"], _book(entry["notebook"]))
        published = 0
        for index, (kind, content) in enumerate(made):
            key = entry_id.replace(":", "-") + (f"-{index + 1}" if index else "")
            reason = f"Pulled from {entry['notebook']}.ipynb: {entry['section']}"
            relevance = 0.6 if entry["live"] else 0.3   # report material sits at the bottom
            if kind == "chart":
                tile_feed.publish(content, entry["title"], reason, relevance=relevance, hours=24, key=key, push=False)
            else:
                title, frame, formatters = content
                tile_feed.publish(_formatted(frame, formatters), f"{entry['title']}" + (f": {title.title()}" if title else ""),
                                  reason, relevance=relevance, hours=24, key=key, push=False)
            published += 1
        if published:
            tile_feed.push_to_web()
        STATUS[entry_id] = f"done: {published} tile{'s' if published != 1 else ''}" if published else "nothing to draw"
    except Exception as exc:   # shown to the reader, not raised into the dashboard
        STATUS[entry_id] = f"failed: {type(exc).__name__}: {str(exc)[:120]}"
    return STATUS[entry_id]


def start_pull(entry_id: str) -> None:
    if STATUS.get(entry_id) == "running":
        return
    STATUS[entry_id] = "running"
    threading.Thread(target=pull, args=(entry_id,), name=f"tile-pull-{entry_id}", daemon=True).start()


if __name__ == "__main__":
    print(json.dumps({"entries": entries()}))
