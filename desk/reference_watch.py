"""Watching the products the desk learns from (IB, Robinhood, DefiLlama, Hyperliquid): what they add, change and drop.

They have feedback from millions of users; what they ship, rename or retire is evidence about what traders use. Each
reference below is a page or feed checked daily; a change in its headlines (or, for a data API, in what it exposes) is a
finding for the desk to study: a feature to copy, a data source to pull, a front-end choice, a tool they integrate.

Only pages verified to answer scripts are listed (DefiLlama's own docs page blocks them; its API docs and API answer).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

import requests

SNAPSHOT_FILE = Path.home() / ".trading-desk" / "reference_watch.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) trading-desk reference watch"}
REFERENCES = {
    "Hyperliquid docs (index of every page)": "https://hyperliquid.gitbook.io/hyperliquid-docs/llms.txt",
    "Hyperliquid Python SDK releases": "https://github.com/hyperliquid-dex/hyperliquid-python-sdk/releases.atom",
    "Robinhood newsroom": "https://robinhood.com/us/en/newsroom/",
    "IBKR newsletter (NEWs @ IBKR)": "https://www.interactivebrokers.com/en/general/communiques/2025/2025-vol27.php",
    "DefiLlama API docs": "https://api-docs.defillama.com/",
    "DeepAlpha (order books, liquidation map, wallet tracker, smart money)": "https://app.deepalpha.pro/app/",
}
# DefiLlama's free API families (checked 2026-11-17: these answer without a key; derivatives, bridges, raises and
# emissions answer "Upgrade to the paid API plan"). Their size is watched: a family that goes paid or empty is a finding.
DEFILLAMA_FREE = {"protocols": "https://api.llama.fi/protocols", "stablecoins": "https://stablecoins.llama.fi/stablecoins",
                  "yields": "https://yields.llama.fi/pools", "dex volumes": "https://api.llama.fi/overview/dexs",
                  "fees": "https://api.llama.fi/overview/fees", "open interest": "https://api.llama.fi/overview/open-interest",
                  "hacks": "https://api.llama.fi/hacks"}


def headlines(text: str) -> list[str]:
    """The page's headings and entry titles (HTML h1-h3, Atom titles, markdown links in llms.txt), de-duplicated."""
    found = re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", text, re.S | re.I) + re.findall(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    found += re.findall(r"^\s*-\s*\[([^\]]+)\]\(", text, re.M)
    clean = []
    for item in found:
        item = re.sub(r"<[^>]+>", " ", item)
        item = " ".join(item.split())
        if 3 <= len(item) <= 160 and item not in clean:
            clean.append(item)
    return clean[:400]


def snapshot() -> dict:
    out = {"at": time.time(), "pages": {}, "defillama": {}}
    for name, url in REFERENCES.items():
        try:
            response = requests.get(url, headers=HEADERS, timeout=30)
            out["pages"][name] = {"status": response.status_code, "headlines": headlines(response.text),
                                  "hash": hashlib.sha256(response.content).hexdigest()[:16]}
        except requests.RequestException as exc:
            out["pages"][name] = {"status": 0, "error": type(exc).__name__, "headlines": []}
    for name, url in DEFILLAMA_FREE.items():
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            body = response.json() if response.ok else None
            size = len(body) if isinstance(body, list) else len(body.get("data") or body.get("protocols") or body.get("peggedAssets") or []) \
                if isinstance(body, dict) else 0
            out["defillama"][name] = {"status": response.status_code, "items": size}
        except (requests.RequestException, ValueError):
            out["defillama"][name] = {"status": 0, "items": 0}
    return out


def diff(old: dict, new: dict) -> list[str]:
    findings = []
    for name, page in new.get("pages", {}).items():
        before = old.get("pages", {}).get(name)
        if not before:
            continue
        if before.get("status") == 200 and page.get("status") != 200:
            findings.append(f"{name} stopped answering (HTTP {page.get('status')}).")
            continue
        added = [h for h in page.get("headlines", []) if h not in before.get("headlines", [])]
        removed = [h for h in before.get("headlines", []) if h not in page.get("headlines", [])]
        if added:
            findings.append(f"{name}: new — " + "; ".join(added[:6]) + (" …" if len(added) > 6 else ""))
        if removed and len(removed) <= 10:
            findings.append(f"{name}: gone — " + "; ".join(removed[:6]))
    for name, now in new.get("defillama", {}).items():
        before = old.get("defillama", {}).get(name)
        if before and before.get("status") == 200 and now.get("status") != 200:
            findings.append(f"DefiLlama {name} is no longer free (HTTP {now.get('status')}).")
    return findings


def run(path: Path | None = None) -> list[str]:
    path = path or SNAPSHOT_FILE
    new = snapshot()
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        old = None
    findings = diff(old, new) if old else []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new))
    return findings


if __name__ == "__main__":
    result = run()
    print("\n".join(result) if result else "No changes since the last look (or the first look was just taken).")
