"""Move the desk from the Mac and Cloudflare to the desk server. Run on the Mac, from the repository.

    python deploy/migrate.py --host 5.161.1.2 --prepare   # relay config and stored data; trading untouched
    python deploy/migrate.py --host 5.161.1.2 --state     # the cutover: daemon state, journal, data folder

``--prepare`` writes the relay's config on the server (the HL wallets from accounts.json and the relay
token the Mac already pushes with, so nothing changes on the Mac's side), stores that token for the
server's own Python, and copies what the Cloudflare relay holds: IB, alerts, research, regime, wallet
performance and each wallet's saved daily history. Run it any time before the cutover; it is safe to
repeat.

``--state`` copies what the trading daemon runs on: guards, ladders and their settings, held
take-profits, the margin and regime guards, alerts, the paper book, the trade journal, the data folder
and accounts.json. It refuses while the daemon still runs on the Mac, so the two never trade at once.

The HL API key is not copied: type it on the server (deploy/README.md).
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

CLOUDFLARE = "https://desk-relay.grownism.workers.dev"
RUN_DIR = Path.home() / ".trading-desk"
SERVER_REPO = "/home/desk/trading-desk"
SERVER_RUN = "/home/desk/.trading-desk"
STATE_FILES = ["hl_guards.json", "held_targets.json", "ladders.json", "ladder.json", "margin_guard.json",
               "regime_guard.json", "daemon_alerts.json"]
RELAY_KEYS = ["ib", "alerts", "research", "regime", "walletperf", "sync"]


def ssh(host: str, command: str, stdin: str | None = None) -> str:
    result = subprocess.run(["ssh", f"desk@{host}", command], input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"ssh failed ({command.split()[0]}): {result.stderr.strip()}")
    return result.stdout


def put_file(host: str, remote: str, text: str) -> None:
    """Write ``text`` to ``remote`` on the server, readable only by the desk user."""
    folder = shlex.quote(str(Path(remote).parent))
    ssh(host, f"umask 077; mkdir -p {folder} && cat > {shlex.quote(remote)}.tmp && mv {shlex.quote(remote)}.tmp {shlex.quote(remote)}",
        stdin=text)


def cloudflare(path: str):
    response = requests.get(CLOUDFLARE + path, timeout=60)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def prepare(host: str) -> None:
    from desk.accounts import accounts_for, load_accounts
    from desk.privacy import short_id
    from desk.web_sync import ingest_token

    wallets = {short_id(account.id): account.id for account in accounts_for("hl", load_accounts())}
    token = ingest_token()
    if not token:
        raise SystemExit("No relay token in the Mac's Keychain: run python -m desk.web_sync --store-token | pbcopy first.")
    existing = ssh(host, f"cat {SERVER_RUN}/relay.json 2>/dev/null || echo '{{}}'")
    config = json.loads(existing or "{}")
    config.update({"wallets": wallets, "ingest_token": token})
    config.setdefault("user", "desk")
    put_file(host, f"{SERVER_RUN}/relay.json", json.dumps(config, indent=1))
    print(f"relay config: {len(wallets)} wallets ({', '.join(wallets)}), the Mac's relay token"
          + ("" if config.get("password_scrypt") else "; no dashboard password yet (deploy/README.md)"))
    ssh(host, f"cd {SERVER_REPO} && set -a && . deploy/desk.env && set +a && .venv/bin/python -m desk.web_sync --set-token",
        stdin=token)
    print("relay token stored for the server's Python")

    store = f"{SERVER_RUN}/relay"
    for key in RELAY_KEYS:
        body = cloudflare("/" + key)
        if body is None:
            print(f"  {key}: nothing on Cloudflare")
            continue
        put_file(host, f"{store}/{urllib.parse.quote(key, safe='')}.json", json.dumps(body))
        print(f"  {key}: copied")
    for short in wallets:
        points = (cloudflare(f"/hl/{short}/history") or {}).get("points", [])
        saved = {}
        for t, value, pnl in points:  # the Worker keeps one point per UTC day: {day: [t, value, pnl]}
            day = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")
            saved[day] = [t, value, pnl]
        put_file(host, f"{store}/{urllib.parse.quote('history:' + short, safe='')}.json", json.dumps(saved))
        print(f"  history:{short}: {len(saved)} days copied")
    ssh(host, "sudo /usr/bin/systemctl restart desk-relay")
    print("relay restarted with the copied data")


def daemon_running_here() -> bool:
    return subprocess.run(["pgrep", "-f", "hl_daemon.py serve"], capture_output=True).returncode == 0


def state(host: str) -> None:
    if daemon_running_here():
        raise SystemExit("The trading daemon still runs on this Mac. Stop it first "
                         "(printf 'STOP DAEMON\\n' | .venv/bin/python execution/hl_daemon.py stop), so the two never trade at once.")
    if ssh(host, "systemctl is-active desk-daemon || true").strip() == "active":
        raise SystemExit("The daemon already runs on the server; nothing copied.")
    files = [str(RUN_DIR / name) for name in STATE_FILES if (RUN_DIR / name).exists()]
    subprocess.run(["rsync", "-a", *files, f"desk@{host}:{SERVER_RUN}/"], check=True)
    if (RUN_DIR / "paper").exists():
        subprocess.run(["rsync", "-a", str(RUN_DIR / "paper"), f"desk@{host}:{SERVER_RUN}/"], check=True)
    print(f"daemon state copied: {', '.join(Path(item).name for item in files)}")
    subprocess.run(["rsync", "-a", "--exclude", "api_cache/", str(REPO / "data") + "/", f"desk@{host}:{SERVER_REPO}/data/"],
                   check=True)
    subprocess.run(["rsync", "-a", str(REPO / "accounts.json"), f"desk@{host}:{SERVER_REPO}/accounts.json"], check=True)
    # macOS's rsync has no --chmod: make the copied state readable by the desk user only.
    ssh(host, f"chmod 600 {SERVER_RUN}/*.json {SERVER_REPO}/accounts.json; chmod -R go-rwx {SERVER_RUN} {SERVER_REPO}/data")
    print("data folder (journal, histories, research state) and accounts.json copied")
    print("Next: store the HL key on the server, then start the daemon there (deploy/README.md).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="the server's IPv4 address")
    parser.add_argument("--prepare", action="store_true", help="relay config, token and stored data (trading untouched)")
    parser.add_argument("--state", action="store_true", help="the cutover: copy the daemon's state (daemon stopped here)")
    args = parser.parse_args()
    if not (args.prepare or args.state):
        parser.error("give --prepare or --state")
    if args.prepare:
        prepare(args.host)
    if args.state:
        state(args.host)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
