"""What the reader does with the dashboard, read back: which highlights get marked read, which are called not important,
which sections are hidden or moved, which look is used. The dashboard sends each action as a JSON line to
data/feedback.jsonl (local dashboard) or ~/.trading-desk/feedback.jsonl on the server (web dashboard).

Run: PYTHONPATH=. python research/feedback_summary.py [path ...]      (default: the local file)
     ssh desk 'cat ~/.trading-desk/feedback.jsonl' | PYTHONPATH=. python research/feedback_summary.py -
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

LOCAL = Path(__file__).resolve().parents[1] / "data" / "feedback.jsonl"


def load(sources: list[str]) -> pd.DataFrame:
    rows = []
    for source in sources:
        text = sys.stdin.read() if source == "-" else Path(source).read_text() if Path(source).exists() else ""
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["at"] = pd.to_datetime(frame.get("at", frame.get("received")), utc=True, errors="coerce")
    return frame


def summary(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Counts by kind: highlights by area (read vs not important), sections by action, the look by choice."""
    if frame.empty:
        return {}
    out = {}
    highlights = frame[frame["kind"] == "highlight"]
    if len(highlights):
        table = highlights.pivot_table(index="target", columns="action", values="at", aggfunc="count", fill_value=0)
        table["total"] = table.sum(axis=1)
        out["Highlights by area (what the reader answered)"] = table.sort_values("total", ascending=False)
    used = frame[frame["kind"] == "use"]
    if len(used):   # what is clicked and rested on, by tile, and the tiles marked unused
        out["Use by tile (hourly lines)"] = used.groupby(["target", "action"]).size().rename("lines").reset_index().sort_values("lines", ascending=False)
    for kind, title in (("section", "Sections"), ("tile", "Key-number tiles"), ("asset", "Assets opened"), ("look", "Look")):
        part = frame[frame["kind"] == kind]
        if len(part):
            out[title] = part.groupby(["target", "action"]).size().rename("count").reset_index().sort_values("count", ascending=False)
    return out


def main(argv: list[str]) -> None:
    frame = load(argv or [str(LOCAL)])
    tables = summary(frame)
    if not tables:
        print("No feedback yet.")
        return
    print(f"{len(frame)} events, {frame['at'].min():%b %d} to {frame['at'].max():%b %d}")
    pd.set_option("display.width", 200)
    for title, table in tables.items():
        print(f"\n{title}\n{table.to_string()}")


if __name__ == "__main__":
    main(sys.argv[1:])
