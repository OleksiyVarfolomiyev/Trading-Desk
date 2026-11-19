"""The desk's build history: every commit labelled ``effect(purpose)`` by the trading result it served, for the dashboard.

    python -m desk.build_history update      # label commits not yet in desk/build_history.csv (new commits, no history rewrite)
    python -m desk.build_history relabel     # rebuild every row from git and the labels (after editing desk/build_labels.csv)
    python -m desk.build_history notes       # copy the labels to local git notes (refs/notes/labels; never pushed)
    python -m desk.build_history publish     # the "Build history" tiles onto the dashboard (desk/tile_feed.py)
    git log --notes=labels --oneline         # the labels beside each commit

Labels follow the labelled-commits rule. ``purpose`` is the result a commit serves, not the tool it touched:
research (finding and testing edge), strategy (what to trade, entries and exits, candidate and thesis rules), execution
(getting orders done: fills, fees, order handling, daemon actions), risk (sizing policy, exposure, leverage, drawdown,
guards, margin, and seeing risk to act on it) or overhead (none of these: cosmetics, tooling, ops that change no
result). ``effect`` is new, improve or fix. A dashboard commit is labelled by the decision it supports.

Where a label comes from, in order: ``desk/build_labels.csv`` (labels settled by reading each commit's subject, message and
diff, with a one-line reason; every commit up to 2026-11-19 is there), a subject that already starts ``effect(purpose):``,
then ``classify``'s word and file rules (low or medium confidence; review and move them into the label table). History
is never rewritten and the notes are never pushed.
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
CSV_PATH = REPO / "desk" / "build_history.csv"
LABELS_PATH = REPO / "desk" / "build_labels.csv"
NOTES_REF = "labels"
EFFECTS = ("new", "improve", "fix")
PURPOSES = ("research", "strategy", "execution", "risk", "overhead")
RESULT_PURPOSES = PURPOSES[:-1]   # the four that move a trading result
COLUMNS = ["hash", "date_et", "effect", "purpose", "confidence", "why", "subject", "files", "added", "removed"]
CODE = (".py", ".js", ".mjs", ".css", ".html", ".sh", ".service", ".timer")   # lines of these count as churn (notebook outputs and data do not)
NOISE = ("__pycache__", ".DS_Store")

LABELLED = re.compile(r"^(new|improve|fix)\((research|strategy|execution|risk|overhead)\):")
OLD_LABEL = re.compile(r"^(feat|fix|research|infra|refactor|docs)\(\w+\):")   # the scheme before 2026-11-19; says nothing of purpose

# Rules for commits not in the label table, first match wins: (purpose, subject words, file paths).
PURPOSE_RULES = [
    ("research", r"\bstud(y|ies)\b|replay|backtest|validat|forward test|experiment|hypothes|attribution|benchmark|picks|factor model|"
                 r"\bdebrief|signal research|walk-forward|performance (table|window|by)", r"^research/|^desk/(books|book_factors|factor_risk|stock_risk|beta_attribution|benchmark_regimes|trade_debrief|book_signals)\.py$"),
    ("risk", r"\brisk\b|exposure|leverage|drawdown|margin|liquidat|\bstops?\b|stop-loss|guard|\bcap\b|stress|\bbeta\b|hedge|kill switch|flatten|"
             r"open positions|uPnL", r"^desk/(live_risk|stress|risk_replay|listings|reconcile)\.py$"),
    ("execution", r"\border|executor|execution|\bfills?\b|\bfees?\b|maker|taker|chase|batch|take-profit|trade bar|plan and confirm|"
                  r"build-ups?|latency|sizing rounds|signing|api wallet", r"^execution/|^desk/(execution_quality|kelly)\.py$"),
    ("strategy", r"candidates?|screener|ladder|market[- ]making|regime|momentum|squeeze|alt rally|thesis|conviction|fade|entries|"
                 r"funding watch|crowd|signals?\b|auto[- ]trad", r"^desk/(screener|market_making|market_regime|alt_rally|funding_watch|event_watch|market_context|journal)\.py$"),
]
FIX = re.compile(r"\bfix(ed|es)?\b|crash|\bbug\b|repair|restore|throws?\b|syntax error|retr(y|ies)\b|handle a |stop .* growing|typeerror|"
                 r"\bhid\b|blanked|stalled|no longer (reads|counts)|instead of crash|was squeezed|wrong|refused", re.I)
NEW = re.compile(r"^(add|new|introduce|create|start)\b|\badded\b", re.I)
COSMETIC = re.compile(r"colou?r|theme|icon|label|spacing|margin(s)? cut|layout|font|tooltip|hover|wording|decimals|readme|rename|"
                      r"gitignore|typo|\bdrag|resize|menu|swipe|tile|chart (title|labels?)", re.I)


def classify(hash_: str, subject: str, files: list[str], labels: dict[str, dict] | None = None) -> tuple[str, str, str, str]:
    """(effect, purpose, confidence, why) for one commit."""
    labels = load_labels() if labels is None else labels
    if hash_ in labels:
        entry = labels[hash_]
        return entry["effect"], entry["purpose"], entry["confidence"], entry["why"]
    match = LABELLED.match(subject)
    if match:
        return match.group(1), match.group(2), "high", "labelled in its subject"
    text = OLD_LABEL.sub("", subject).strip()
    effect = "fix" if FIX.search(text) else "new" if NEW.search(text) else "improve"
    real = [f for f in files if not any(part in f for part in NOISE)]
    votes: Counter = Counter()
    for purpose, words, paths in PURPOSE_RULES:
        if re.search(words, text, re.I):
            votes[purpose] += 2
        votes[purpose] += sum(1 for f in real if re.search(paths, f))
    if not votes or max(votes.values()) == 0:
        return effect, "overhead", "low", "rule: no research, strategy, execution or risk words or files"
    purpose, score = votes.most_common(1)[0]
    if COSMETIC.search(text) and score < 3:
        return effect, "overhead", "low", "rule: a presentation change"
    runner_up = votes.most_common(2)[1][1] if len(votes) > 1 else 0
    return effect, purpose, "medium" if score >= 3 and score > runner_up else "low", f"rule: {purpose} words or files"


def load_labels(path: Path = LABELS_PATH) -> dict[str, dict]:
    """The settled labels (hash -> effect, purpose, confidence, why)."""
    if not path.exists():
        return {}
    with path.open(newline="") as handle:
        return {row["hash"]: row for row in csv.DictReader(handle)}


def read_git(since: str | None = None) -> list[dict]:
    """Every commit oldest first with its files and the lines added and removed in code files."""
    command = ["git", "-C", str(REPO), "log", "--reverse", "--numstat", "--format=@@%H|%ct|%s"]
    if since:
        command.append(f"{since}..HEAD")
    output = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    commits, current = [], None
    for line in output.splitlines():
        if line.startswith("@@"):
            full, stamp, subject = line[2:].split("|", 2)
            current = {"hash": full[:7], "full": full, "time": int(stamp), "subject": subject, "files": [], "added": 0, "removed": 0}
            commits.append(current)
        elif line.strip() and current is not None:
            added, removed, path = (line.split("\t") + ["", "", ""])[:3]
            if any(part in path for part in NOISE):
                continue
            current["files"].append(path)
            if path.endswith(CODE) and added.isdigit() and removed.isdigit():
                current["added"] += int(added)
                current["removed"] += int(removed)
    return commits


def rows_from(commits: list[dict], labels: dict[str, dict] | None = None) -> list[dict]:
    labels = load_labels() if labels is None else labels
    eastern = ZoneInfo("America/New_York")
    rows = []
    for commit in commits:
        effect, purpose, confidence, why = classify(commit["hash"], commit["subject"], commit["files"], labels)
        rows.append({"hash": commit["hash"], "date_et": datetime.fromtimestamp(commit["time"], eastern).strftime("%Y-%m-%d %H:%M"),
                     "effect": effect, "purpose": purpose, "confidence": confidence, "why": why, "subject": commit["subject"],
                     "files": ";".join(commit["files"]), "added": commit["added"], "removed": commit["removed"]})
    return rows


def read_csv(path: Path = CSV_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(rows: list[dict], path: Path = CSV_PATH) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def update(path: Path = CSV_PATH) -> int:
    """Label the commits the file does not hold yet (all of them the first time); returns how many were added. A file in
    the old kind(area) shape is rebuilt whole."""
    have = read_csv(path)
    if have and "purpose" not in have[0]:
        have = []
    known = {row["hash"] for row in have}
    rows = [row for row in rows_from(read_git()) if row["hash"] not in known]
    write_csv(have + rows, path)
    return len(rows)


def relabel(path: Path = CSV_PATH) -> int:
    """Every row again from git and the current labels (after desk/build_labels.csv changed)."""
    rows = rows_from(read_git())
    write_csv(rows, path)
    return len(rows)


def write_notes(path: Path = CSV_PATH) -> int:
    """Local git notes (refs/notes/labels): ``git log --notes=labels`` shows each label. Never pushed."""
    count = 0
    for row in read_csv(path):
        subprocess.run(["git", "-C", str(REPO), "notes", "--ref", NOTES_REF, "add", "-f", "-m",
                        f"{row['effect']}({row['purpose']}): {row['why']}", row["hash"]], capture_output=True, check=False)
        count += 1
    return count


# ---------------------------------------------------------------------------------------------- summaries
def churn(row: dict) -> int:
    return int(row["added"]) + int(row["removed"])


def per_day(rows: list[dict]) -> dict[str, dict[str, int]]:
    days: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        days[row["date_et"][:10]][row["purpose"]] += 1
    return {day: dict(counts) for day, counts in sorted(days.items())}


def by_purpose(rows: list[dict]) -> list[dict]:
    """Per purpose: commits, their share, the share of code churn, and the share that were fixes (a lower bound on rework)."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["purpose"]].append(row)
    total, total_churn = len(rows), sum(churn(row) for row in rows) or 1
    table = []
    for purpose in PURPOSES:
        items = groups.get(purpose, [])
        if not items:
            continue
        table.append({"Purpose": purpose, "Commits": len(items), "Commits %": len(items) / total,
                      "Churn %": sum(churn(row) for row in items) / total_churn,
                      "Fix share": sum(1 for row in items if row["effect"] == "fix") / len(items)})
    return table


def by_effect(rows: list[dict]) -> list[dict]:
    """Purpose by effect: how much was new, improved or repaired in each."""
    counts = Counter((row["purpose"], row["effect"]) for row in rows)
    return [{"Purpose": purpose, **{effect.capitalize(): counts.get((purpose, effect), 0) for effect in EFFECTS}}
            for purpose in PURPOSES if any(counts.get((purpose, e)) for e in EFFECTS)]


def hotspots(rows: list[dict], limit: int = 8) -> list[dict]:
    """The code files changed most often, with how many of those changes were fixes and what they mostly served."""
    touched: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        for path in row["files"].split(";"):
            if path.endswith(CODE) and not path.startswith("tests/"):
                touched[path].append(row)
    table = [{"File": path, "Commits": len(items), "Fixes": sum(1 for r in items if r["effect"] == "fix"),
              "Mostly": Counter(r["purpose"] for r in items).most_common(1)[0][0],
              "Last fix": max((r["date_et"][:10] for r in items if r["effect"] == "fix"), default="")}
             for path, items in touched.items()]
    return sorted(table, key=lambda row: (-row["Commits"], -row["Fixes"]))[:limit]


NOT_ADOPTED = re.compile(r"not adopted|failed|no signal|did not|dropped|not established|stays? off|retired|worse|did not predict", re.I)


def research_outcomes(rows: list[dict], limit: int = 10) -> list[dict]:
    """The latest research commits that added something (effect new), with their verdict: not adopted when the subject or
    the label's reason says so, otherwise unstated (the commit does not say whether the idea worked)."""
    items = [row for row in rows if row["purpose"] == "research" and row["effect"] == "new"][-limit:][::-1]
    # The verdict is the label in front of the study, so it is read first.
    return [{"Date": row["date_et"][:10],
             "Research": ("Not adopted · " if NOT_ADOPTED.search(row["subject"] + " " + row.get("why", "")) else "Unstated · ")
             + row["subject"]}
            for row in items]


def summary(rows: list[dict]) -> dict:
    return {"commits": len(rows), "first": rows[0]["date_et"][:10] if rows else "", "last": rows[-1]["date_et"][:10] if rows else "",
            "purposes": by_purpose(rows), "effects": by_effect(rows), "hotspots": hotspots(rows), "research": research_outcomes(rows)}


def publish(path: Path = CSV_PATH, hours: float = 24 * 14, push: bool = True, directory: Path | None = None) -> list[str]:
    """Five tiles, ranked low so they sit with the report material: commits per day by purpose, effort by purpose, purpose
    by effect, the hotspots and the latest research outcomes."""
    import plotly.graph_objects as go

    from desk import tile_feed

    rows = read_csv(path)
    if not rows:
        return []
    import pandas as pd

    days = per_day(rows)
    # Commits per day by purpose as lines over every calendar day (a day without commits is a point at nothing done).
    calendar = [d.strftime("%Y-%m-%d") for d in pd.date_range(min(days), max(days), freq="D")]
    daily = go.Figure([go.Scatter(name=purpose, x=calendar, y=[days.get(day, {}).get(purpose, 0) for day in calendar], mode="lines")
                       for purpose in PURPOSES if any(days[day].get(purpose) for day in days)])
    daily.update_layout(yaxis={"tickformat": "d", "rangemode": "tozero"}, hovermode="x unified")
    # Effort by purpose: share of commits, share of churn and the fix share, one group per purpose.
    purposes = by_purpose(rows)
    effort = go.Figure([go.Bar(name=name, x=[row["Purpose"] for row in purposes], y=[row[key] or None for row in purposes])
                        for key, name in (("Commits %", "Commits"), ("Churn %", "Churn"), ("Fix share", "Fixes"))])
    effort.update_layout(barmode="group", yaxis={"tickformat": ".0%"})
    # New, improve, fix as shares of each purpose: every bar is 100%.
    effects = by_effect(rows)
    mix = go.Figure()
    for effect in EFFECTS:
        name = effect.capitalize()
        shares = [row[name] / (sum(row[e.capitalize()] for e in EFFECTS) or 1) for row in effects]
        mix.add_bar(name=name, x=[row["Purpose"] for row in effects], y=[share or None for share in shares], textposition="none")
    mix.update_layout(barmode="stack", yaxis={"tickformat": ".0%", "range": [0, 1]})
    common = {"hours": hours, "relevance": 0.1, "push": False, "directory": directory}
    keys = []
    for key, content, title, formats in [
        ("build-history-days", daily, "Build history: commits per day by purpose", None),
        ("build-history-purposes", effort, "Build history: effort by purpose", None),
        ("build-history-effects", mix, "Build history: new, improved, fixed", None),
        ("build-history-hotspots", _frame(hotspots(rows)), "Build history: hotspots", {"Commits": "num", "Fixes": "num"}),
        ("build-history-research", _frame(research_outcomes(rows)), "Build history: research outcomes", None),
    ]:
        if not hasattr(content, "to_plotly_json"):
            content = _blank_zeros(content)   # absent is absent: no 0 counts, no 0% shares
        tile_feed.publish(content, title, reason="how the desk was built", key=key, formats=formats, **common)
        keys.append(key)
    tile_feed.withdraw("build-history-areas", push=False, directory=directory)   # the kind(area) tile this replaced
    try:   # the build-vs-risk/reward tiles, from the saved result (python -m desk.commit_risk_reward run refreshes it)
        from desk import commit_risk_reward

        keys += commit_risk_reward.publish_cached(hours=hours, push=False, directory=directory)
    except Exception as exc:
        print(f"  build vs risk/reward tiles not published ({type(exc).__name__}: {exc})")
    if push:
        tile_feed.push_to_web(directory)
    return keys


def _frame(table: list[dict]):
    import pandas as pd

    return pd.DataFrame(table)


def _blank_zeros(frame):
    from desk.commit_risk_reward import blank_zeros

    return blank_zeros(frame)


def main(argv: list[str] | None = None) -> int:
    command = (argv if argv is not None else sys.argv[1:] or ["update"])[0]
    if command == "update":
        print(f"labelled {update()} new commits -> {CSV_PATH.relative_to(REPO)}")
    elif command == "relabel":
        print(f"relabelled {relabel()} commits -> {CSV_PATH.relative_to(REPO)}")
    elif command == "notes":
        print(f"wrote {write_notes()} notes under refs/notes/{NOTES_REF} (local only)")
    elif command == "publish":
        print("published " + ", ".join(publish()))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
