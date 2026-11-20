"""Track explicitly linked suggestions using deployment and usage evidence.

Use `python -m desk.suggestion_funnel --list` to find a suggestion ID, then
`--link ID COMMIT` to associate it with a full commit. Add `--target TARGET`,
optionally `--action ACTION`, to match exactly one feedback target. Add
`--component daemon` for changes that require a daemon restart. The link
file is private. Deployment timestamps come from the desk server's log.
Use `--order-change CHANGE` to compare relay confirmation outcomes for an
explicitly selected action. Unknown outcomes are reported separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNNEL_KINDS = ("design", "process", "rule", "test", "size", "hold")   # plus the hand-logged advice rows (kind info with a ref), shown as 'advice'
MIN_N = 20
LINKS = ROOT / "data" / "suggestion_links.jsonl"
DEPLOYMENTS = ROOT / "data" / "deployments.jsonl"
MIN_USE_EVENTS = 3
USAGE_DATA_FROM = "2026-11-17"


def suggestion_id(row: dict) -> str:
    return hashlib.sha256(json.dumps([row["at"], row["text"]], ensure_ascii=False).encode()).hexdigest()


def _ts(iso: str) -> datetime:
    value = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


def eligible(row: dict) -> bool:
    return row["kind"] in FUNNEL_KINDS or bool(
        row["kind"] == "info" and row.get("ref") and row.get("fit") in ("process", "additional", "strategy")
    )


def deployments_by_commit(rows: list[dict]) -> dict:
    deployed = {}
    for row in rows:
        try:
            at = _ts(row["at"])
            key = (row["component"], row["commit"])
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        deployed[key] = min(deployed.get(key, at), at)
    return deployed


def linked_commits() -> dict:
    return {row["suggestion"]: row for row in _local_jsonl(LINKS)}


def usage(ship: dict, events: list[dict]) -> dict | None:
    if not ship.get("target") or not ship.get("deployed_at"):
        return None
    deployed = _ts(ship["deployed_at"])
    stamped = []
    for event in events:
        try:
            stamped.append((_ts(event["received"]), event))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    first = min((at for at, _ in stamped), default=deployed)
    last = max((at for at, _ in stamped), default=deployed)
    hits = [at for at, event in stamped if event.get("target") == ship["target"]
            and (not ship.get("action") or event.get("action") == ship["action"])]
    before = sum(at < deployed for at in hits)
    after = len(hits) - before
    days_before = (deployed - first).total_seconds() / 86400
    days_after = (last - deployed).total_seconds() / 86400
    return {"events_before": before, "events_after": after, "used": after >= MIN_USE_EVENTS,
            "per_day_before": before / days_before if days_before >= 1 else None,
            "per_day_after": after / days_after if days_after >= 1 else None}


def outcome(ship: dict, orders: list[dict]) -> dict:
    change = ship.get("order_change")
    if not change:
        return {"verdict": "not measurable", "note": "No suggestion-specific outcome series is recorded."}
    deployed = _ts(ship["deployed_at"])
    before, after = [], []
    for order in orders:
        if order.get("change") != change:
            continue
        try:
            at = _ts(order["at"])
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        (before if at < deployed else after).append(order)
    stats = {}
    for key, cohort in (("before", before), ("after", after)):
        known = [order for order in cohort if type(order.get("code")) is int and order.get("state") != "unknown"]
        stats[key] = {"confirmed": len(known), "unknown": len(cohort) - len(known),
                      "nonzero_share": sum(order["code"] != 0 for order in known) / len(known) if known else None}
    enough = min(stats["before"]["confirmed"], stats["after"]["confirmed"]) >= MIN_N
    return {"verdict": "descriptive" if enough else "not measurable",
            "note": "Before/after is observational and may include other releases." if enough else f"Need {MIN_N} known confirmations on each side.",
            **stats}


def funnel(rows: list[dict], links: dict, deployments: list[dict], events: list[dict], orders: list[dict] = ()) -> list[dict]:
    deployed = deployments_by_commit(deployments)
    out = []
    for row in rows:
        if not eligible(row):
            continue
        link = links.get(suggestion_id(row))
        ship = None
        if link:
            at = deployed.get((link["component"], link["commit"]))
            ship = {"hash": link["commit"], "target": link.get("target"), "action": link.get("action"), "order_change": link.get("order_change"),
                    "deployed_at": at.isoformat() if at and at >= _ts(row["at"]) else None}
        used = usage(ship, events) if ship else None
        shipped = bool(ship and ship["deployed_at"])
        out.append({"at": row["at"], "kind": "advice" if row["kind"] == "info" else row["kind"],
                    "fit": row.get("fit", "unknown"), "text": row["text"], "source": row.get("source"),
                    "shipped": shipped, "ship": ship, "used": bool(used and used["used"]), "usage": used,
                    "result": outcome(ship, orders) if shipped else None})
    return out


def table(records: list[dict], by) -> list[dict]:
    """The funnel by a grouping (kind or fit): suggested, shipped, used, result measurable, verdict counts, with percentages of the previous stage."""
    groups: dict = defaultdict(list)
    for rec in records:
        groups[by(rec)].append(rec)
    rows = []
    for name, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        shipped = [x for x in g if x["shipped"]]
        used = [x for x in shipped if x["used"]]
        measurable = [x for x in used if x["result"] and x["result"]["verdict"] != "not measurable"]
        verdicts = defaultdict(int)
        for x in used:
            verdicts[x["result"]["verdict"]] += 1
        rows.append({"group": name, "suggested": len(g), "shipped": len(shipped), "shipped_pct": len(shipped) / len(g) if g else None, "used": len(used),
                     "used_pct": (len(used) / len(shipped)) if shipped else None, "measurable": len(measurable),
                     "measurable_pct": (len(measurable) / len(used)) if used else None, "verdicts": dict(verdicts), "too_few": len(g) < MIN_N})
    return rows


def _ssh_cat(path: str) -> str:
    try:
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "desk", f"cat {path}"],
                                capture_output=True, text=True, timeout=20)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _jsonl(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _local_jsonl(path: Path) -> list[dict]:
    try:
        return _jsonl(path.read_text())
    except FileNotFoundError:
        return []


def build() -> dict:
    from desk import suggestions as s

    deployments = _local_jsonl(DEPLOYMENTS)
    deployments.extend(_jsonl(_ssh_cat("trading-desk/data/deployments.jsonl")))
    events = _local_jsonl(ROOT / "data" / "feedback.jsonl")
    events.extend(_jsonl(_ssh_cat("~/.trading-desk/feedback.jsonl")))
    events = list({(e.get("received"), e.get("action"), e.get("target"), e.get("kind")): e for e in events}.values())
    links = linked_commits()
    orders = _jsonl(_ssh_cat("~/.trading-desk/web_actions.log")) if any(link.get("order_change") for link in links.values()) else []
    records = funnel(s.load(), links, deployments, events, orders)
    return {"records": records, "by_kind": table(records, lambda r: r["kind"]), "by_fit": table(records, lambda r: r["fit"]),
            "all": table(records, lambda r: "all")[0] if records else None,
            "coverage": {"usage_from": USAGE_DATA_FROM, "events": len(events), "deployments": len(deployments), "links": len(links)}}


def funnel_frame(data: dict):
    import pandas as pd

    rows = []
    for tag, table_ in (("kind", data["by_kind"]), ("fit", data["by_fit"])):
        for r in table_:
            rows.append({"By": f"{tag}: {r['group']}" + (" (too few)" if r["too_few"] else ""), "Suggested": r["suggested"], "Shipped": r["shipped"], "Shipped %": r["shipped_pct"],
                         "Used": r["used"], "Used %": r["used_pct"], "Result measurable": r["measurable"],
                         "Descriptive comparisons": r["verdicts"].get("descriptive", 0)})
    return pd.DataFrame(rows)


def funnel_figure(data: dict):
    import plotly.graph_objects as go

    kinds = [r for r in data["by_kind"] if r["suggested"]]
    fig = go.Figure()
    for name, key, color in (("shipped", "shipped_pct", "#4c78a8"), ("used (of shipped)", "used_pct", "#d95926"), ("result measurable (of used)", "measurable_pct", "#59a14f")):
        fig.add_bar(name=name, x=[r["group"] + (" *" if r["too_few"] else "") for r in kinds], y=[r[key] or None for r in kinds], marker={"color": color},
                    text=[f"n {r['suggested']}" for r in kinds], hovertemplate="%{x}: %{y:.0%} (%{text})<extra>" + name + "</extra>", textposition="none")
    fig.update_layout(barmode="group", yaxis={"tickformat": ".0%", "range": [0, 1]}, showlegend=True, meta={"legend": True}, legend={"orientation": "h", "x": 0, "y": 1.12})
    return fig


def main() -> int:
    from desk import suggestions as s

    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--list", action="store_true")
    modes.add_argument("--link", nargs=2, metavar=("ID", "COMMIT"))
    modes.add_argument("--detail", action="store_true")
    parser.add_argument("--target")
    parser.add_argument("--order-change", choices=("long", "short", "add", "reduce", "close", "batch"))
    parser.add_argument("--action")
    parser.add_argument("--component", choices=("desk", "daemon"), default="desk")
    args = parser.parse_args()
    if args.list or args.link:
        rows = {suggestion_id(row): row for row in s.load() if eligible(row)}
        if args.list:
            for key, row in rows.items():
                print(key, row["at"], row["kind"], row["text"])
            return 0
        key, ref = args.link
        if key not in rows:
            parser.error("unknown suggestion ID; use --list")
        if args.action and not args.target:
            parser.error("--action requires --target")
        command = ["git", "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"]
        commit = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if commit.returncode:
            parser.error("unknown commit")
        row = {"suggestion": key, "commit": commit.stdout.strip(), "component": args.component,
               "target": args.target, "action": args.action, "order_change": args.order_change}
        LINKS.parent.mkdir(parents=True, exist_ok=True)
        with LINKS.open("a") as file:
            file.write(json.dumps(row) + "\n")
        return 0
    data = build()
    summary = {k: data[k] for k in ("by_kind", "by_fit", "all", "coverage")}
    if args.detail:
        summary["records"] = data["records"]
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
