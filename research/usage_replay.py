"""Replay of how the desk is used: what is asked, how the answers are reacted to, when, and what gets traded, read from
the Claude Code session transcripts (~/.claude/projects/<this project>/*.jsonl) and the trade journal. The categories and
counts feed the picture of how the dashboard should serve its user (desk/usage_model.py holds the model that adapts it).

Run: PYTHONPATH=. python research/usage_replay.py
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path

CATEGORIES = {
    "trade order": r"\b(short|long|fade|close|trim|you run it|run it|put shorts|execute)\b",
    "dashboard and notebooks (UI)": r"dashboard|tile|chart|table|label|legend|color|colour|icon|switch|bar |menu|notebook",
    "explain / why": r"\bwhy\b|\bwhat (is|does|are)\b|how do|how is|\?$",
    "correction of format or policy": r"don'?t|not |remove|instead|should|policy|apples|terse|concise|no need|too ",
    "market reaction": r"bounce|squeeze|regime|alts|btc|red to green|sell.?off|funding|fear",
    "research and data": r"research|test|backtest|binance|data|replay|signal|model|factor|beta",
    "market making": r"\bmm\b|ladder|market making|neutral",
    "IB and stocks": r"\bib\b|stock|spy|nem|aem|scco|tradfi",
}


def user_messages(folder: Path | None = None) -> list[tuple[str, str]]:
    folder = folder or Path.home() / ".claude" / "projects" / "-Users-admin-Dropbox-Investing-trading-desk"
    found = []
    for path in sorted(folder.glob("*.jsonl")):
        for line in path.read_text(errors="ignore").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") != "user":
                continue
            content = record.get("message", {}).get("content")
            if isinstance(content, list):
                content = " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
            if not isinstance(content, str) or not content.strip() or content.startswith("<") or "system-reminder" in content[:40] \
                    or content.startswith("This session is being continued"):
                continue
            found.append((record.get("timestamp", ""), content.strip()))
    return sorted(found)


def profile(messages: list[tuple[str, str]]) -> dict:
    total = len(messages) or 1
    per_day = Counter(stamp[:10] for stamp, _ in messages)
    hours = Counter((dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")) - dt.timedelta(hours=5)).hour for stamp, _ in messages if stamp)
    return {"messages": len(messages), "per_day": dict(per_day),
            "categories": {name: sum(1 for _, text in messages if re.search(pattern, text, re.I)) / total for name, pattern in CATEGORIES.items()},
            "short_share": sum(1 for _, text in messages if len(text) < 40) / total, "hours_et": dict(sorted(hours.items()))}


if __name__ == "__main__":
    result = profile(user_messages())
    print(f"{result['messages']} requests over {len(result['per_day'])} days; {result['short_share']:.0%} are under 40 characters")
    for name, share in sorted(result["categories"].items(), key=lambda item: -item[1]):
        print(f"  {share:4.0%}  {name}")
    print("busiest hours (ET):", sorted(result["hours_et"].items(), key=lambda item: -item[1])[:5])
