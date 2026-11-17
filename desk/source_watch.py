"""Watching the sources the desk reads: what they add, remove and change, so the data, signals and models adapt.

A snapshot of what each source exposes is kept (``~/.trading-desk/source_watch.json``) and compared with the last one:
* Hyperliquid markets: listed, delisted, leverage or size-precision changes, on the main DEX and every HIP-3 DEX
  (xyz and the others), and new DEXes;
* the shape of the responses the desk parses (the field names of ``meta``, asset contexts, candles): a field appearing
  or disappearing is the first sign a parser or a model input is about to change;
* the series the research reads from outside (Yahoo daily closes for SPY, QQQ, IWM, SMH): still answering, with data.

``run()`` returns the findings in words and raises them as one dashboard alert (the daemon runs it daily with the research
feed). A new market is not a trade: it enters the screener's universe on its own volume and history rules, and the finding
says so; a delisted one that the desk holds is the one finding that is urgent.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

HL_INFO = "https://api.hyperliquid.xyz/info"
SNAPSHOT_FILE = Path.home() / ".trading-desk" / "source_watch.json"
YAHOO_TICKERS = ("SPY", "QQQ", "IWM", "SMH")


def _post(body: dict) -> object:
    response = requests.post(HL_INFO, json=body, timeout=30)
    response.raise_for_status()
    return response.json()


def _markets(dex: str = "") -> tuple[dict, list[str]]:
    meta, contexts = _post({"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})})
    markets = {asset["name"]: {"maxLeverage": asset.get("maxLeverage"), "szDecimals": asset.get("szDecimals"),
                               "delisted": bool(asset.get("isDelisted"))} for asset in meta["universe"]}
    return markets, sorted(set(meta["universe"][0]) | set(contexts[0])) if meta["universe"] and contexts else []


def snapshot() -> dict:
    markets, fields = _markets()
    out = {"at": time.time(), "markets": {"": markets}, "fields": {"meta_and_context": fields}, "yahoo": {}}
    try:
        dexes = [item["name"] for item in _post({"type": "perpDexs"}) if item]
    except Exception:
        dexes = []
    out["dexes"] = sorted(dexes)
    for dex in dexes:
        try:
            out["markets"][dex] = _markets(dex)[0]
        except Exception:
            out["markets"][dex] = {}
    candles = _post({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1h", "startTime": int(time.time() * 1000) - 7_200_000,
                                                      "endTime": int(time.time() * 1000)}})
    out["fields"]["candle"] = sorted(candles[0]) if candles else []
    try:
        from desk import market_regime

        for ticker in YAHOO_TICKERS:
            out["yahoo"][ticker] = int(len(market_regime.yahoo_series(ticker, 30)))
    except Exception:
        pass
    return out


def diff(old: dict, new: dict, held: set[str] | None = None) -> list[str]:
    """The changes between two snapshots, one sentence each; ``held`` are coins the desk holds (a delisting there is urgent)."""
    held = held or set()
    findings = []
    for dex in sorted(set(old.get("markets", {})) | set(new.get("markets", {}))):
        before, after = old.get("markets", {}).get(dex, {}), new.get("markets", {}).get(dex, {})
        if dex and dex not in old.get("markets", {}):
            findings.append(f"New HIP-3 DEX {dex}: {len(after)} markets.")
            continue
        label = f"{dex} " if dex else ""
        added = sorted(name for name in after if name not in before and not after[name]["delisted"])
        gone = sorted(name for name in before if name not in after or (after[name]["delisted"] and not before[name]["delisted"]))
        if added:
            findings.append(f"New {label}markets: {', '.join(added[:12])}{' and more' if len(added) > 12 else ''}; "
                            "they enter the screener on its own volume and history rules.")
        for name in gone:
            urgent = name in held or name.split(":")[-1] in held
            findings.append(f"{'HELD ' if urgent else ''}{label}market delisted: {name}.")
        for name in sorted(set(before) & set(after)):
            if before[name]["maxLeverage"] != after[name]["maxLeverage"] and not after[name]["delisted"]:
                findings.append(f"{name} max leverage {before[name]['maxLeverage']}x to {after[name]['maxLeverage']}x (position sizing reads it).")
            if before[name]["szDecimals"] != after[name]["szDecimals"] and not after[name]["delisted"]:
                findings.append(f"{name} size precision changed ({before[name]['szDecimals']} to {after[name]['szDecimals']} decimals).")
    for group, fields in new.get("fields", {}).items():
        old_fields = old.get("fields", {}).get(group)
        if old_fields is None:
            continue
        added, removed = sorted(set(fields) - set(old_fields)), sorted(set(old_fields) - set(fields))
        if added:
            findings.append(f"HL {group} response has new fields: {', '.join(added)}.")
        if removed:
            findings.append(f"HL {group} response lost fields the desk may read: {', '.join(removed)}.")
    for ticker, rows in new.get("yahoo", {}).items():
        if old.get("yahoo", {}).get(ticker) and not rows:
            findings.append(f"Yahoo stopped answering for {ticker}: the stock factors and benchmarks fall back to saved prices.")
    return findings


def run(held: set[str] | None = None, path: Path | None = None) -> list[str]:
    """Take a snapshot, compare it with the last one, keep it, and return the findings (none on the first run)."""
    path = path or SNAPSHOT_FILE
    new = snapshot()
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        old = None
    findings = diff(old, new, held) if old else []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new))
    return findings


if __name__ == "__main__":
    result = run()
    print("\n".join(result) if result else "No changes since the last snapshot (or the first one was just taken).")
