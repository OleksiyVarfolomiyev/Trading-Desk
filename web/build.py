"""Copy the relay's WALLETS map to the clipboard without showing it.

The relay (web/worker.js) is the only place outside accounts.json that holds the full wallet
addresses: its WALLETS map gives each wallet's last four characters and its address. On the desk
server it is the "wallets" entry of ~/.trading-desk/relay.json (deploy/migrate.py --prepare
writes it).

    /Users/admin/anaconda3/envs/py314/bin/python web/build.py --wallets-secret | pbcopy

The dashboard itself is assembled by web/build_site.sh, which the server runs on every pull.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from desk.accounts import accounts_for, load_accounts  # noqa: E402
from desk.privacy import short_id  # noqa: E402


def wallets_secret() -> str:
    """The Worker's WALLETS secret: each HL wallet's last four characters -> its full address."""
    return json.dumps({short_id(account.id): account.id for account in accounts_for("hl", load_accounts())})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallets-secret", action="store_true",
                        help="print the WALLETS secret for piping into pbcopy (it holds full addresses)")
    args = parser.parse_args()
    if not args.wallets_secret:
        parser.error("use --wallets-secret | pbcopy (the site is built by web/build_site.sh).")
    if sys.stdout.isatty():
        parser.error("--wallets-secret holds full addresses; pipe it into pbcopy instead of printing it.")
    sys.stdout.write(wallets_secret())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
