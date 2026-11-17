"""Learning from use: every recorded action is fitted into a small model, and the model adapts the dashboard (front end)
and the alerts (back end).

Sources (all one log of actions):
* the dashboard's own events (data/feedback.jsonl on the Mac, ~/.trading-desk/feedback.jsonl on the server): a highlight
  marked read or not important, a tile hidden, minimized, moved or opened, hover and click lines per tile;
* the desk's trades (the journal) and the requests made in chat (research/usage_replay.py reads the session transcripts).

Model (deliberately small, so what it says can be checked):
* highlight areas: keep = (reads + 1) / (reads + dismissed + 2), the posterior mean of "this is worth reading". An area
  dismissed three times or more with a keep below 25% is **quiet**: the dashboard folds it away (never a critical one) and
  the daemon may rank its alerts lower. A single click moves nothing: three signals, and it is reversible from the menu.
* tiles: use = clicks + minutes the pointer rested; a tile never used in two weeks is offered for hiding (front end).

``fit()`` returns the model, ``refresh()`` writes it to ``usage_model.json`` (the local dashboard and the web relay serve it).
The daemon refreshes it hourly with the research feed. Hypotheses about what to change stay hypotheses until they show t >= 2 in
both halves of the log (see research/feedback_summary.py); this model only folds away what the reader has said, repeatedly,
is noise.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOME = Path.home() / ".trading-desk"
FEEDBACK_FILES = [REPO / "data" / "feedback.jsonl", HOME / "feedback.jsonl"]
MODEL_FILE = HOME / "usage_model.json"
LOCAL_MODEL_FILE = REPO / "data" / "usage_model.json"
QUIET_MIN_DISMISSED = 3
QUIET_MAX_KEEP = 0.25


def load_events(files: list[Path] | None = None) -> list[dict]:
    events, seen = [], set()
    for path in files or FEEDBACK_FILES:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            key = (event.get("at"), event.get("kind"), event.get("action"), event.get("target"), event.get("detail"))
            if key not in seen:      # the same event can reach both files (local and server)
                seen.add(key)
                events.append(event)
    return events


def fit(events: list[dict], now: float | None = None) -> dict:
    now = time.time() if now is None else now
    areas: dict[str, dict] = {}
    tiles: dict[str, dict] = {}
    for event in events:
        kind, action, target = event.get("kind"), event.get("action"), str(event.get("target") or "")
        if kind == "highlight" and action in ("read", "useful", "not important"):
            area = areas.setdefault(target, {"read": 0, "dismissed": 0})
            if action == "not important":
                area["dismissed"] += 1
            else:   # read, or marked useful (a thumbs-up counts twice: it is a judgement, not just a glance)
                area["read"] += 2 if action == "useful" else 1
        elif kind == "use":
            tile = tiles.setdefault(target, {"lines": 0, "hidden_unused": 0})
            tile["lines"] += 1
            tile["hidden_unused"] += action == "hide unused"
        elif kind == "section" and action in ("hide", "minimize"):
            tile = tiles.setdefault(target, {"lines": 0, "hidden_unused": 0})
            tile["hidden"] = tile.get("hidden", 0) + 1
    for area in areas.values():
        area["keep"] = (area["read"] + 1) / (area["read"] + area["dismissed"] + 2)
        area["quiet"] = area["dismissed"] >= QUIET_MIN_DISMISSED and area["keep"] < QUIET_MAX_KEEP
    return {"fitted": now, "events": len(events), "areas": areas, "tiles": tiles,
            "quiet": sorted(name for name, area in areas.items() if area["quiet"])}


def refresh(files: list[Path] | None = None, paths: list[Path] | None = None) -> dict:
    """Fit the model from every recorded event and write it where the dashboards read it."""
    model = fit(load_events(files))
    for path in paths or [MODEL_FILE, LOCAL_MODEL_FILE]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(model))
        except OSError:
            continue
    return model


def current() -> dict:
    """The last fitted model (an empty one before any feedback)."""
    for path in (MODEL_FILE, LOCAL_MODEL_FILE):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return {"areas": {}, "tiles": {}, "quiet": []}


if __name__ == "__main__":
    result = refresh()
    print(f"{result['events']} events; quiet areas: {result['quiet'] or 'none yet'}")
    for name, area in sorted(result["areas"].items(), key=lambda item: item[1]["keep"]):
        print(f"  {name}: read {area['read']}, dismissed {area['dismissed']}, keep {area['keep']:.0%}{'  QUIET' if area['quiet'] else ''}")
