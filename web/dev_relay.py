"""Local stand-in for the desk Worker (web/worker.js), for trying the web dashboard on this machine.

Like the Worker, it serves the dashboard (web/site, assembled by web/build_site.sh) and the
relay's endpoints from one address, http://127.0.0.1:8787, with the wallet addresses taken
from accounts.json and the same scrubbing, so the page behaves as it will online:

    bash web/build_site.sh && .venv/bin/python web/dev_relay.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk.accounts import accounts_for, load_accounts  # noqa: E402
from desk.privacy import short_id  # noqa: E402
from desk.venues.onchain import (  # noqa: E402
    CHAINS, blockscout_holdings, rpc_holdings, staking_reward_holdings,
)

HL_INFO = "https://api.hyperliquid.xyz/info"
PORT = 8787
SITE = Path(__file__).resolve().parent / "site"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}
CACHE_SECONDS = 20
WALLETS = {short_id(account.id): account.id for account in accounts_for("hl", load_accounts())}
_cache: dict[str, tuple[float, bytes]] = {}
_dexes: list[str] = []


def hl(body: dict):
    response = requests.post(HL_INFO, json=body, timeout=30)
    response.raise_for_status()
    return response.json()


def hl_account(user: str) -> dict:
    global _dexes
    if not _dexes:
        _dexes = [""] + [item["name"] for item in hl({"type": "perpDexs"}) if item and item.get("name")]
    fees = hl({"type": "userFees", "user": user})
    states = [hl({"type": "clearinghouseState", "user": user, **({"dex": dex} if dex else {})}) for dex in _dexes]
    return {
        "abstraction": hl({"type": "userAbstraction", "user": user}),
        "spot": hl({"type": "spotClearinghouseState", "user": user}).get("balances", []),
        "fees": {"maker": float(fees["userAddRate"]), "taker": float(fees["userCrossRate"])},
        "perp": [[dex, {"marginSummary": state["marginSummary"], "assetPositions": state["assetPositions"]}]
                 for dex, state in zip(_dexes, states)],
    }


def wallet_holdings(user: str) -> dict:
    holdings, problems = [], []
    for name, read in (("base", lambda: rpc_holdings(CHAINS["base"], user) + staking_reward_holdings(CHAINS["base"], user)),
                       ("ethereum", lambda: blockscout_holdings(CHAINS["ethereum"], user))):
        try:
            for item in read():
                token = item.token
                rewards = token.startswith("rewards:")
                holdings.append({"chain": item.chain, "token": token.removeprefix("rewards:"), "symbol": item.symbol,
                                 "amount": item.amount, **({"rewards": True} if rewards else {})})
        except Exception as exc:
            problems.append(f"{name}: {exc}")
    return {"holdings": holdings, "problems": problems}


def scrub(value, address: str):
    hidden = re.compile(address[2:], re.IGNORECASE)

    def clean(item):
        if isinstance(item, list):
            return [clean(inner) for inner in item]
        if isinstance(item, dict):
            return {key: clean(inner) for key, inner in item.items() if key not in ("hash", "user", "users")}
        return hidden.sub("", item) if isinstance(item, str) else item

    return clean(value)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = [part for part in self.path.split("?")[0].split("/") if part]
        kind, wallet, what = (parts + [None, None, None])[:3]
        page = SITE / (parts[0] if len(parts) == 1 and "." in parts[0] else "index.html" if not parts else "")
        if page.is_file() and page.parent == SITE:
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(page.suffix, "application/octet-stream"))
            self.end_headers()
            self.wfile.write(page.read_bytes())
            return
        if kind == "wallets" and wallet is None:
            return self.reply(200, json.dumps({"wallets": sorted(WALLETS)}).encode())
        address = WALLETS.get(wallet or "")
        if not address:
            return self.reply(404, b'{"error": "unknown wallet id"}')
        hit = _cache.get(self.path)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return self.reply(200, hit[1])
        try:
            if kind == "hl" and what == "account":
                body = hl_account(address)
            elif kind == "hl" and what == "portfolio":
                body = hl({"type": "portfolio", "user": address})
            elif kind == "wallet" and what is None:
                body = wallet_holdings(address)
            else:
                return self.reply(404, b'{"error": "unknown request"}')
        except Exception as exc:
            return self.reply(502, json.dumps({"error": scrub(str(exc), address)}).encode())
        data = json.dumps(scrub(body, address)).encode()
        _cache[self.path] = (time.time(), data)
        self.reply(200, data)


if __name__ == "__main__":
    print(f"dev relay on http://127.0.0.1:{PORT} for wallets {', '.join(sorted(WALLETS))}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
