"""Send the desk's Mac-only data to the web dashboard's relay (web/worker.js).

The web dashboard reads HL itself, but IB is only reachable through TWS on this Mac, and HL keeps
daily history detail for about a month. So this Mac sends the relay:

* the IB account: balances, positions (with their 24-hour moves), open risk and the daily NAV
  since the account opened (Flex history, and the activity-statement rebuild before it);
* once, to seed the relay's store: the daily HL value and PnL points saved in
  ``data/hl_portfolio_history_<last4>.csv``.

Only the last four characters of any account reach the relay; it refuses anything else. Writes
need the relay's ``INGEST_TOKEN`` secret, kept here in the Keychain. The local dashboard sends IB
at most every 10 minutes while it runs.

    python -m desk.web_sync --store-token | pbcopy    # make a token: Keychain + clipboard (paste into Cloudflare)
    python -m desk.web_sync --seed-history            # once: send the saved daily HL history
    python -m desk.web_sync --push-ib                 # send IB now
"""

from __future__ import annotations

import argparse
import math
import os
import secrets
import sys
from pathlib import Path

import pandas as pd
import requests

from desk.accounts import accounts_for, ib_data_dir, load_accounts
from desk.privacy import account_file, short_id

RELAY_URL = os.environ.get("DESK_RELAY_URL", "https://desk-relay.grownism.workers.dev")
KEYCHAIN_SERVICE = "Desk relay ingest"
KEYCHAIN_USER = "default"
PUSH_EVERY_SECONDS = 600


def ingest_token() -> str:
    import keyring

    return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_USER) or ""


def store_new_token() -> str:
    """Make a random token, keep it in the Keychain and return it (for the relay's INGEST_TOKEN secret)."""
    import keyring

    token = secrets.token_urlsafe(32)
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_USER, token)
    return token


def _post(path: str, body: dict) -> dict:
    token = ingest_token()
    if not token:
        raise RuntimeError("no relay token in the Keychain (python -m desk.web_sync --store-token)")
    response = requests.post(RELAY_URL.rstrip("/") + path, json=body, timeout=60,
                             headers={"Authorization": f"Bearer {token}"})
    try:
        answer = response.json()
    except ValueError:
        answer = {"error": response.text[:200]}
    if not response.ok:
        raise RuntimeError(f"relay answered HTTP {response.status_code}: {answer.get('error', answer)}")
    return answer


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def ib_nav(account_id: str) -> pd.DataFrame:
    """Daily ``date, nav, flows``: the activity-statement rebuild before the Flex history, then Flex."""
    folder = ib_data_dir()
    nav = pd.read_csv(account_file(folder, "ib_nav_history", account_id)) if account_file(
        folder, "ib_nav_history", account_id).exists() else pd.DataFrame(columns=["date", "nav", "flows"])
    backfill = account_file(folder, "ib_nav_backfill", account_id)
    if backfill.exists():
        earlier = pd.read_csv(backfill)
        if len(nav):
            earlier = earlier[pd.to_datetime(earlier["date"]) < pd.to_datetime(nav["date"]).min()]
        nav = pd.concat([earlier, nav]) if len(nav) else earlier
    return nav.drop_duplicates("date", keep="last").sort_values("date")


def ib_payload(snapshot, book_entry=None, moves: dict | None = None) -> dict:
    """The IB account as the web dashboard needs it, with only its last four characters."""
    ident = short_id(snapshot.account)
    nav = ib_nav(snapshot.account)
    positions = [{
        "symbol": position.symbol, "asset_class": position.asset_class, "quantity": position.quantity,
        "avg_price": _clean(position.avg_price * position.fx_to_usd),
        "mark_price": _clean(position.mark_price * position.fx_to_usd),
        "multiplier": position.multiplier, "unrealized": _clean(position.unrealized_usd),
        "change24h": _clean((moves or {}).get((snapshot.label, position.symbol), float("nan"))),
    } for position in snapshot.positions]
    return {
        "id": ident, "label": f"IB {ident}", "as_of": pd.Timestamp.now(tz="UTC").isoformat(),
        "account": {"equity": snapshot.equity_usd, "cash": snapshot.cash_usd, "margin_used": snapshot.margin_used_usd,
                    "available": snapshot.available_usd, "unrealized": _clean(snapshot.unrealized_usd)},
        "risk": ({"equity": book_entry.equity_usd, "risk": book_entry.risk_usd, "positions": book_entry.positions}
                 if book_entry is not None else None),
        "positions": positions,
        "nav": [[row.date, float(row.nav), float(row.flows)] for row in nav.itertuples()],
    }


def push_ib(snapshot, book_entry=None, moves: dict | None = None) -> dict:
    return _post("/ingest/ib", ib_payload(snapshot, book_entry, moves))


def seed_history(data_dir: Path | str = "data") -> dict[str, int]:
    """Send each HL wallet's saved daily points to the relay's store."""
    result = {}
    for account in accounts_for("hl", load_accounts()):
        path = account_file(data_dir, "hl_portfolio_history", account.id)
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        frame["ts"] = pd.to_datetime(frame["ts"], utc=True, format="mixed")
        daily = frame.sort_values("ts").groupby(frame["ts"].dt.strftime("%Y-%m-%d")).last()
        points = [[int(row.ts.timestamp() * 1000), float(row.accountValue), float(row.pnl)] for row in daily.itertuples()]
        result[short_id(account.id)] = _post(f"/ingest/history/{short_id(account.id)}", {"points": points}).get("days", 0)
    return result


def main(argv: list[str] | None = None) -> int:
    from desk.privacy import mask_output

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--store-token", action="store_true",
                        help="make a new relay token, keep it in the Keychain and print it for pbcopy")
    parser.add_argument("--seed-history", action="store_true", help="send the saved daily HL history to the relay")
    parser.add_argument("--push-ib", action="store_true", help="read IB through TWS and send it to the relay now")
    args = parser.parse_args(argv)
    if args.store_token:
        if sys.stdout.isatty():
            parser.error("--store-token prints the secret token; pipe it into pbcopy instead.")
        sys.stdout.write(store_new_token())
        return 0
    mask_output()
    if args.seed_history:
        print("Saved daily points on the relay:", seed_history())
    if args.push_ib:
        from desk.day_change import position_changes
        from desk.risk import ib_book_entry
        from desk.venues import ib as ib_venue

        for account in accounts_for("ib", load_accounts()):
            def read(account=account):
                entry, snapshot = ib_book_entry(account)
                return entry, snapshot

            entry, snapshot = ib_venue.run_off_event_loop(read)
            answer = push_ib(snapshot, entry, position_changes([snapshot]))
            print(f"Sent {snapshot.label} ({len(snapshot.positions)} positions, NAV history included): {answer}")
    if not (args.seed_history or args.push_ib):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
