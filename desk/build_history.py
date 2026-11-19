"""The desk's build history: every commit labelled ``kind(area)``, summarised for the dashboard.

    python -m desk.build_history update      # label commits not yet in desk/build_history.csv (new commits, no history rewrite)
    python -m desk.build_history notes       # copy the labels to local git notes (refs/notes/labels; never pushed)
    python -m desk.build_history publish     # the "Build history" tiles onto the dashboard (desk/tile_feed.py)
    git log --notes=labels --oneline         # the labels beside each commit

Labels follow the labelled-commits rule: kind = feat | fix | research | infra | refactor | docs; area = ui | web | daemon |
relay | analytics | factor | signals | journal | ib | hl. Commits made before the rule have no label in their subject, so
``classify`` reads the subject and the files changed; ``confidence`` says how sure it is, ``OVERRIDES`` holds the labels
a person settled by hand. A commit whose subject already starts ``kind(area):`` keeps that label. History is never
rewritten and the notes are never pushed.
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
NOTES_REF = "labels"
KINDS = ("feat", "fix", "research", "infra", "refactor", "docs")
AREAS = ("ui", "web", "daemon", "relay", "analytics", "factor", "signals", "journal", "ib", "hl")
COLUMNS = ["hash", "date_et", "kind", "area", "confidence", "subject", "files", "added", "removed"]
CODE = (".py", ".js", ".mjs", ".css", ".html", ".sh", ".service", ".timer")   # lines of these count as churn (notebook outputs and data do not)
NOISE = ("__pycache__", ".DS_Store")

# File path -> (area, weight). First match wins. Data files and caches carry no weight.
AREA_RULES = [
    (r"^desk/dashboard_static/", "ui", 1.2), (r"^desk/(dashboard|tile_catalog|tile_feed|usage_model|push|numbers|privacy|charts|emphasis)\.py$", "ui", 1.0),
    (r"^web/(worker\.js|server\.mjs)$", "relay", 1.0), (r"^(deploy/|wrangler\.jsonc)", "relay", 1.0),
    (r"^web/", "web", 1.0), (r"^desk/web_sync\.py$", "web", 1.0),
    (r"^execution/ib_", "ib", 1.0), (r"^desk/ib/", "ib", 1.0), (r"^desk/ib_", "ib", 1.0),
    (r"^execution/", "daemon", 1.0), (r"^desk/(alerts|trade_rules|degraded)\.py$", "daemon", 1.0),
    (r"^desk/(journal|trade_debrief)\.py$", "journal", 1.0), (r"^research/(trade|journal)", "journal", 0.8),
    (r"^desk/(factor_risk|stock_risk|book_factors|crypto_sectors)", "factor", 1.0), (r"^research/(factor|stock_risk|portfolio_risk)", "factor", 1.0),
    (r"^desk/(screener|book_signals|alt_rally|funding_watch|market_regime|event_watch|market_context|reference_watch|source_watch|market_making|return_distribution|market_caps)\.py$", "signals", 1.0),
    (r"^research/.*(signal|pump|dump|replay|regime|mm_|funding|market)", "signals", 0.9),
    (r"^desk/hl/", "hl", 1.0), (r"^(hl_|hyperliquid)[^/]*\.(ipynb|py|csv)$", "hl", 0.6), (r"^hl_.*\.ipynb$", "hl", 0.6),
    (r"\.ipynb$", "analytics", 0.5), (r"^(crypto_portfolio|crypto_read)", "analytics", 0.8),
    (r"^desk/", "analytics", 1.0), (r"^research/.*\.py$", "analytics", 0.8), (r"^tests/run_affected", "analytics", 0.5),
    (r"^tests/", "", 0.2), (r"^(README|\.gitignore|requirements)", "", 0.0),
]
# Words in a subject that say what part of the desk a commit is about (one vote of 0.8 per area that matches).
SUBJECT_AREAS = {
    "ui": r"dashboard|\btiles?\b|chart view|\bcharts?\b|\btables?\b|highlights?|price strip|pulse|theme|key number|switch|menu|layout|columns?\b|drag|hover|tooltip|icon|colou?r|label",
    "daemon": r"daemon|ladder|margin guard|auto[- ]?(trad|flatten)|take-profit|\bstops?\b|executor|build-ups?|unprotect|chase|killswitch|guard|phone push|thesis entries|risk cap|batch execution|order",
    "signals": r"momentum|squeeze|alt rally|stock dip|extraordinary|regime|screener|candidates|funding|short-book watch|\bwatch\b|market context|reference watch|crowding|kelly|order-book|spoof|\bsignals?\b",
    "journal": r"journal|debrief|strategy tags?",
    "factor": r"factor (model|risk)|style betas|sector blocks|risk model",
    "ib": r"\bIB\b|activity statements?|\bTWS\b",
    "web": r"web dashboard|web relay|live\.js|\bweb\b",
    "relay": r"\brelay\b|hetzner|cloudflare|\bdeploy|\b429",
    "analytics": r"notebooks?|picks|benchmark|attribution|performance|closed positions|\bbooks?\b|factor models?|beta|alpha",
    "hl": r"hyperliquid|\bHL\b|wallets?",
}
VAGUE = re.compile(r"^(v\.? ?\d|fixes|small fixes|cosmetics|viz|data upd|dashboard upd|logging update|renaming|upd |pdated|execution fix|readme upd|added readme|fixed data)", re.I)
FIX = re.compile(r"\bfix(ed|es)?\b|crash|\bbug\b|repair|restore|throws?\b|syntax error|retr(y|ies)\b|handle a |stop .* growing|typeerror|"
                 r"\bhid\b|blanked|stalled|not blink|no longer (reads|counts)|instead of crash|was squeezed|back to |original", re.I)
RESEARCH = re.compile(r"\bstud(y|ies)\b|replay|backtest|validat|forward test|experiment|hypothes|signal research|binance replay|"
                      r"pump and dump|dump-phase|market data through|factor model|factor analysis|research data", re.I)
INFRA = re.compile(r"\bdeploy|systemd|cloudflare|hetzner|\bbackup|\btimer\b|daily (sync|job)|run_affected|"
                   r"gitignore|dropbox|project moved|launchd|ignore local|sudo rules|desk server", re.I)
REFACTOR = re.compile(r"refactor|renam|simplif|drop unused|remove unused|reorganis|put into relevant|instead of .* seconds|loads? in about", re.I)
DOCS = re.compile(r"readme|\bdocs?\b|documentation", re.I)
LABELLED = re.compile(r"^(feat|fix|research|infra|refactor|docs)\((ui|web|daemon|relay|analytics|factor|signals|journal|ib|hl)\):")

# Settled by hand: the commits whose subject and files do not say what they were (the first weeks, one-word messages).
OVERRIDES: dict[str, tuple[str, str]] = {
    # the first weeks: one-word messages, read from the files and what the notebook did next
    "0c0833e": ("fix", "analytics"), "0a0da27": ("feat", "analytics"), "b60f39c": ("feat", "hl"), "1403726": ("feat", "analytics"),
    "f04b7ed": ("docs", "analytics"), "5189d4d": ("refactor", "analytics"), "775b468": ("feat", "analytics"), "354b412": ("docs", "analytics"),
    "cc1174a": ("feat", "hl"), "4b2a6f6": ("docs", "analytics"), "3ed7787": ("infra", "analytics"), "56b52c2": ("feat", "analytics"),
    "83c8e79": ("feat", "daemon"), "1ee9eda": ("fix", "daemon"), "2103a55": ("feat", "ui"), "39273bd": ("feat", "hl"),
    "50c4a6c": ("feat", "ui"), "599943f": ("fix", "ui"), "c31f120": ("fix", "analytics"), "3857724": ("feat", "ui"),
    "e4f9d98": ("feat", "ui"), "d4217fc": ("feat", "ui"), "eb0967f": ("feat", "ui"), "cdcdcd7": ("feat", "daemon"),
    "0739c4d": ("feat", "analytics"), "c1a7c91": ("research", "factor"), "31167df": ("research", "signals"),
    "449b73f": ("feat", "hl"), "decad0a": ("feat", "ui"), "d422505": ("feat", "analytics"), "7c9d7f2": ("feat", "daemon"),
    "3243f00": ("research", "factor"), "f83e7f6": ("research", "factor"), "fc18852": ("feat", "analytics"),
    # a fix or an infra word in the subject that is not what the commit did
    "01ff048": ("feat", "ib"), "cd87c49": ("feat", "ui"), "0c41a91": ("feat", "daemon"), "e2057a9": ("feat", "signals"),
    "c1d2675": ("feat", "analytics"), "0457164": ("feat", "daemon"), "d4a52ad": ("infra", "relay"), "7badad4": ("infra", "relay"),
    "0e78b5a": ("refactor", "analytics"), "9c40a65": ("feat", "analytics"), "3e5a0de": ("feat", "daemon"), "8e9a247": ("feat", "daemon"),
    "380dcec": ("feat", "factor"), "6a4c370": ("fix", "factor"), "9969af5": ("feat", "ui"), "21762ac": ("feat", "ui"),
    "2d7ed2d": ("feat", "ui"), "da4d750": ("feat", "ui"), "08d95e0": ("infra", "analytics"), "8c1b298": ("infra", "analytics"),
    "c8ec7d3": ("infra", "ib"), "5ecb920": ("infra", "ib"), "3ca1cec": ("feat", "signals"), "b81f119": ("feat", "signals"),
    "05186a0": ("infra", "signals"), "6438a0d": ("feat", "signals"), "a11bc59": ("feat", "signals"),
}


def area_of(files: list[str], subject: str = "") -> tuple[str, float]:
    """The area a commit is about and its share: each file votes for an area by path (weights in AREA_RULES, summing to
    1) and each area a subject keyword names adds 0.8."""
    score: Counter = Counter()
    for path in files:
        if any(part in path for part in NOISE):
            continue
        for pattern, area, weight in AREA_RULES:
            if re.search(pattern, path):
                if area and weight:
                    score[area] += weight
                break
    file_total = sum(score.values())
    for area in list(score):
        score[area] = score[area] / file_total
    for area, pattern in SUBJECT_AREAS.items():
        if re.search(pattern, subject, re.I):
            score[area] += 0.8
    total = sum(score.values())
    if not total:
        return "", 0.0
    area, value = score.most_common(1)[0]
    return area, value / total


def kind_of(subject: str, files: list[str]) -> tuple[str, bool]:
    """(kind, explicit): explicit is False when the kind is only the default for an addition."""
    real = [f for f in files if not any(part in f for part in NOISE)]
    if real and all(f.lower().endswith((".md",)) for f in real):
        return "docs", True
    if real and all(f.startswith(("deploy/", "tests/", ".gitignore", "wrangler")) or f.endswith(".md") for f in real) and not FIX.search(subject):
        return "infra", True
    if FIX.search(subject):
        return "fix", True
    if real and all(f.startswith("research/") or f.startswith("tests/") for f in real) and any(f.startswith("research/") for f in real):
        return "research", True
    if RESEARCH.search(subject) and not any("dashboard_static" in f for f in real):
        return "research", True
    if REFACTOR.search(subject):
        return "refactor", True
    if INFRA.search(subject):
        return "infra", True
    if DOCS.search(subject):
        return "docs", True
    return "feat", False


def classify(hash_: str, subject: str, files: list[str]) -> tuple[str, str, str]:
    """(kind, area, confidence) for one commit."""
    if hash_ in OVERRIDES:
        return (*OVERRIDES[hash_], "manual")
    match = LABELLED.match(subject)
    if match:
        return match.group(1), match.group(2), "high"
    kind, explicit = kind_of(subject, files)
    area, share = area_of(files, subject)
    if not area:
        area = "analytics"
        confidence = "low"
    elif share >= 0.6:
        confidence = "high" if explicit or len(subject) > 25 else "medium"
    else:
        confidence = "medium"
    if VAGUE.search(subject) or len(subject.split()) <= 2:
        confidence = "low"
    return kind, area, confidence


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


def rows_from(commits: list[dict]) -> list[dict]:
    eastern = ZoneInfo("America/New_York")
    rows = []
    for commit in commits:
        kind, area, confidence = classify(commit["hash"], commit["subject"], commit["files"])
        rows.append({"hash": commit["hash"], "date_et": datetime.fromtimestamp(commit["time"], eastern).strftime("%Y-%m-%d %H:%M"),
                     "kind": kind, "area": area, "confidence": confidence, "subject": commit["subject"],
                     "files": ";".join(commit["files"]), "added": commit["added"], "removed": commit["removed"]})
    return rows


def read_csv(path: Path = CSV_PATH) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(rows: list[dict], path: Path = CSV_PATH) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def update(path: Path = CSV_PATH) -> int:
    """Label the commits the file does not hold yet (all of them the first time); returns how many were added."""
    have = read_csv(path)
    known = {row["hash"] for row in have}
    rows = [row for row in rows_from(read_git()) if row["hash"] not in known]
    write_csv(have + rows, path)
    return len(rows)


def write_notes(path: Path = CSV_PATH) -> int:
    """Local git notes (refs/notes/labels): ``git log --notes=labels`` shows each label. Never pushed."""
    count = 0
    for row in read_csv(path):
        subprocess.run(["git", "-C", str(REPO), "notes", "--ref", NOTES_REF, "add", "-f", "-m", f"{row['kind']}({row['area']})", row["hash"]],
                       capture_output=True, check=False)
        count += 1
    return count


# ---------------------------------------------------------------------------------------------- summaries
def per_day(rows: list[dict]) -> dict[str, dict[str, int]]:
    days: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        days[row["date_et"][:10]][row["kind"]] += 1
    return {day: dict(counts) for day, counts in sorted(days.items())}


def by_area(rows: list[dict]) -> list[dict]:
    """Commits, the share that were fixes (a fix share is a lower bound on rework) and code lines changed, per area."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["area"]].append(row)
    table = []
    for area, items in groups.items():
        fixes = sum(1 for row in items if row["kind"] == "fix")
        table.append({"Area": area, "Commits": len(items), "Fix share": fixes / len(items),
                      "Lines changed": sum(int(row["added"]) + int(row["removed"]) for row in items)})
    return sorted(table, key=lambda row: -row["Commits"])


def hotspots(rows: list[dict], limit: int = 8) -> list[dict]:
    """The code files changed most often, with how many of those changes were fixes (the repeat-fix files)."""
    touched: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        for path in row["files"].split(";"):
            if path.endswith(CODE) and not path.startswith("tests/"):
                touched[path].append(row)
    table = [{"File": path, "Commits": len(items), "Fixes": sum(1 for r in items if r["kind"] == "fix"),
              "Last fix": max((r["date_et"][:10] for r in items if r["kind"] == "fix"), default="")}
             for path, items in touched.items()]
    return sorted(table, key=lambda row: (-row["Commits"], -row["Fixes"]))[:limit]


NOT_ADOPTED = re.compile(r"not adopted|failed|no signal|did not|dropped|not established|stays? off|retired|worse", re.I)
# Verdicts the subject does not carry, settled from the studies' own write-ups (research/*.md, the desk memories).
VERDICTS = {"ba0e51c": "not adopted", "5480383": "not adopted", "1db25ab": "not adopted", "c7f42ed": "not adopted",
            "0bcf5b1": "not adopted"}


def research_outcomes(rows: list[dict], limit: int = 10) -> list[dict]:
    """The latest research commits with their verdict: not adopted when the subject says so or the study's write-up did,
    otherwise unstated (the commit does not say whether the idea worked)."""
    items = [row for row in rows if row["kind"] == "research"][-limit:][::-1]
    return [{"Date": row["date_et"][:10], "Research": row["subject"],
             "Outcome": VERDICTS.get(row["hash"]) or ("not adopted" if NOT_ADOPTED.search(row["subject"]) else "unstated")}
            for row in items]


def summary(rows: list[dict]) -> dict:
    return {"commits": len(rows), "first": rows[0]["date_et"][:10] if rows else "", "last": rows[-1]["date_et"][:10] if rows else "",
            "kinds": dict(Counter(row["kind"] for row in rows)), "areas": by_area(rows), "hotspots": hotspots(rows),
            "research": research_outcomes(rows)}


def publish(path: Path = CSV_PATH, hours: float = 24 * 14, push: bool = True, directory: Path | None = None) -> list[str]:
    """Four tiles on the dashboard, ranked low so they sit with the report material: commits per day by kind, the areas,
    the hotspots (what keeps breaking) and the latest research outcomes."""
    import plotly.graph_objects as go

    from desk import tile_feed

    rows = read_csv(path)
    if not rows:
        return []
    days = per_day(rows)
    figure = go.Figure([go.Bar(name=kind, x=list(days), y=[days[day].get(kind, 0) for day in days]) for kind in KINDS
                        if any(days[day].get(kind) for day in days)])
    figure.update_layout(barmode="stack")
    common = {"hours": hours, "relevance": 0.1, "push": False, "directory": directory}
    keys = []
    for key, content, title, formats in [
        ("build-history-days", figure, "Build history: commits per day", None),
        ("build-history-areas", _frame(by_area(rows)), "Build history: by area", {"Fix share": "pct", "Commits": "num", "Lines changed": "num"}),
        ("build-history-hotspots", _frame(hotspots(rows)), "Build history: hotspots", {"Commits": "num", "Fixes": "num"}),
        ("build-history-research", _frame(research_outcomes(rows)), "Build history: research outcomes", None),
    ]:
        tile_feed.publish(content, title, reason="how the desk was built", key=key, formats=formats, **common)
        keys.append(key)
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


def main(argv: list[str] | None = None) -> int:
    command = (argv if argv is not None else sys.argv[1:] or ["update"])[0]
    if command == "update":
        print(f"labelled {update()} new commits -> {CSV_PATH.relative_to(REPO)}")
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
