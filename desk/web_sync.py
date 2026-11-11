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

Without any dashboard running, a launchd job runs ``--daily`` at 08:00 and 23:30 (or when the Mac
next wakes): it saves the HL history and the IB Flex NAV to the local data files and sends IB to
the relay. With TWS closed it still sends the new daily NAV; the positions keep their last time.
The relay saves HL's daily history on its own schedule (web/worker.js), Mac or not.

    python -m desk.web_sync --store-token | pbcopy    # make a token: Keychain + clipboard (paste into Cloudflare)
    python -m desk.web_sync --seed-history            # once: send the saved daily HL history
    python -m desk.web_sync --push-ib                 # send IB now
    python -m desk.web_sync --daily                   # what the daily job runs
    python -m desk.web_sync --install-daily           # start the daily job (launchd); --uninstall-daily stops it
"""

from __future__ import annotations

import argparse
import math
import os
import plistlib
import secrets
import subprocess
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
REPO = Path(__file__).resolve().parents[1]
JOB_LABEL = "com.trading-desk.daily-sync"
JOB_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{JOB_LABEL}.plist"
JOB_LOG = Path.home() / ".trading-desk" / "daily_sync.log"
JOB_TIMES = [(8, 0), (23, 30)]  # local time; IB's Flex NAV for a day is ready the next morning


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


RISK_CLOSE_DAYS = 100


def ib_closes(positions) -> dict[str, list]:
    """Recent daily closes of the IB stocks (Yahoo Finance), so the web page can put them in its
    holdings risk; a symbol Yahoo does not know is left out."""
    from desk.live_risk import yahoo_stock_closes

    closes = {}
    for position in positions:
        if position.asset_class != "equity" or position.symbol in closes:
            continue
        try:
            series = yahoo_stock_closes(position.symbol, RISK_CLOSE_DAYS)
        except Exception:
            continue
        closes[position.symbol] = [[day.strftime("%Y-%m-%d"), round(float(close), 6)] for day, close in series.items()]
    return closes


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
        "closes": ib_closes(snapshot.positions),
        "nav": [[row.date, float(row.nav), float(row.flows)] for row in nav.itertuples()],
    }


def wallet_payload(data_dir: Path | str = REPO / "data") -> dict:
    """The wallets' token performance for the web page: per wallet ``[close ms, value, cumulative PnL, base]``."""
    from desk.desk_accounts import wallet_performance

    series = {}
    for name, frame in wallet_performance(load_accounts(), data_dir).items():
        series[name] = [[int(row.ts.timestamp() * 1000), _clean(float(row.accountValue)), _clean(float(row.pnl)),
                         _clean(float(row.base))] for row in frame.itertuples()]
    return {"as_of": pd.Timestamp.now(tz="UTC").isoformat(), "series": series}


def push_wallets(data_dir: Path | str = REPO / "data") -> dict:
    return _post("/ingest/wallets", wallet_payload(data_dir))


def push_ib(snapshot, book_entry=None, moves: dict | None = None) -> dict:
    return _post("/ingest/ib", ib_payload(snapshot, book_entry, moves))


def push_nav_only(account_id: str) -> dict | None:
    """With TWS closed: replace the NAV in the relay's last IB payload, keeping its positions and time."""
    response = requests.get(RELAY_URL.rstrip("/") + "/ib", timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    nav = ib_nav(account_id)
    payload["nav"] = [[row.date, float(row.nav), float(row.flows)] for row in nav.itertuples()]
    payload["nav_as_of"] = pd.Timestamp.now(tz="UTC").isoformat()
    return _post("/ingest/ib", payload)


def read_ib(account):
    """The account's open risk and snapshot through TWS (desk.risk), off any running event loop."""
    from desk.risk import ib_book_entry
    from desk.venues import ib as ib_venue

    return ib_venue.run_off_event_loop(lambda: ib_book_entry(account))


def daily_sync(data_dir: Path | str = REPO / "data") -> list[str]:
    """Save HL history and IB's Flex NAV locally, then send IB to the relay. One line per step."""
    from desk.day_change import position_changes
    from desk.hl.account_analytics import update_portfolio_history
    from desk.venues import ib_flex

    report = []
    from desk.venues.onchain_history import update_wallet_returns

    for account in accounts_for("hl", load_accounts()):
        try:
            update_portfolio_history(account.id, data_dir)
            report.append(f"HL {short_id(account.id)}: history saved")
        except Exception as exc:
            report.append(f"HL {short_id(account.id)}: history not saved ({type(exc).__name__}: {exc})")
        if account.onchain:
            try:
                days = len(update_wallet_returns(account, Path(data_dir))[0])
                report.append(f"HL {short_id(account.id)} wallet: performance saved ({days} days)")
            except Exception as exc:
                report.append(f"HL {short_id(account.id)} wallet: performance not saved ({type(exc).__name__}: {exc})")
    for account in accounts_for("ib", load_accounts()):
        # accounts.json may leave the IB number out (TWS supplies it): then the Flex file names it.
        ident, flex_note = account.id, ""
        try:
            path = ib_flex.update_nav_history(account, ib_data_dir())
            ident = ident or Path(path).stem.rsplit("_", 1)[-1]
            flex_note = "Flex NAV saved"
        except Exception as exc:
            flex_note = f"Flex NAV not saved ({str(exc) or type(exc).__name__})"
        if not ident:
            saved = sorted(ib_data_dir().glob("ib_nav_history_*.csv"))
            ident = saved[0].stem.rsplit("_", 1)[-1] if len(saved) == 1 else ""
        label = f"IB {short_id(ident)}" if ident else "IB"
        report.append(f"{label}: {flex_note}")
        try:
            entry, snapshot = read_ib(account)
            push_ib(snapshot, entry, position_changes([snapshot]))
            report.append(f"{label}: sent to the relay ({len(snapshot.positions)} positions)")
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            try:
                sent = push_nav_only(ident) if ident else None
                report.append(f"{label}: TWS not readable ({reason}); "
                              + ("daily NAV sent, positions kept from the last send" if sent else "nothing on the relay to update"))
            except Exception as inner:
                report.append(f"{label}: TWS not readable ({reason}) and NAV not sent ({inner})")
    try:
        push_wallets(data_dir)
        report.append("wallets: performance sent to the relay")
    except Exception as exc:
        report.append(f"wallets: performance not sent ({type(exc).__name__}: {exc})")
    return report


def job_plist() -> dict:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    env.update({key: value for key, value in os.environ.items() if key.startswith("DESK_")})
    return {
        "Label": JOB_LABEL,
        # The interpreter that installs the job runs it: it needs the notebooks' packages (plotly, ib_async).
        "ProgramArguments": [sys.executable, "-m", "desk.web_sync", "--daily"],
        "WorkingDirectory": str(REPO),
        "StartCalendarInterval": [{"Hour": hour, "Minute": minute} for hour, minute in JOB_TIMES],
        "StandardOutPath": str(JOB_LOG), "StandardErrorPath": str(JOB_LOG),
        "EnvironmentVariables": env,
    }


def install_job() -> None:
    JOB_LOG.parent.mkdir(parents=True, exist_ok=True)
    JOB_PLIST.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{JOB_LABEL}"], capture_output=True)
    JOB_PLIST.write_bytes(plistlib.dumps(job_plist()))
    subprocess.run(["launchctl", "bootstrap", domain, str(JOB_PLIST)], check=True)


def uninstall_job() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{JOB_LABEL}"], capture_output=True)
    JOB_PLIST.unlink(missing_ok=True)


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
    parser.add_argument("--push-wallets", action="store_true", help="send the wallets' token performance to the relay now")
    parser.add_argument("--push-ib", action="store_true", help="read IB through TWS and send it to the relay now")
    parser.add_argument("--daily", action="store_true", help="save HL and IB history locally and send IB (the daily job)")
    parser.add_argument("--install-daily", action="store_true", help="run --daily at 08:00 and 23:30 through launchd")
    parser.add_argument("--uninstall-daily", action="store_true", help="stop and remove the daily job")
    args = parser.parse_args(argv)
    if args.store_token:
        if sys.stdout.isatty():
            parser.error("--store-token prints the secret token; pipe it into pbcopy instead.")
        sys.stdout.write(store_new_token())
        return 0
    mask_output()
    if args.install_daily:
        install_job()
        times = " and ".join(f"{hour:02d}:{minute:02d}" for hour, minute in JOB_TIMES)
        print(f"Daily job installed: {times} (or at the next wake), log in {JOB_LOG}")
    if args.uninstall_daily:
        uninstall_job()
        print("Daily job removed.")
    if args.daily:
        print(pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"), "daily sync")
        for line in daily_sync():
            print("  " + line)
    if args.seed_history:
        print("Saved daily points on the relay:", seed_history())
    if args.push_wallets:
        print("Sent wallet performance:", push_wallets())
    if args.push_ib:
        from desk.day_change import position_changes

        for account in accounts_for("ib", load_accounts()):
            entry, snapshot = read_ib(account)
            answer = push_ib(snapshot, entry, position_changes([snapshot]))
            print(f"Sent {snapshot.label} ({len(snapshot.positions)} positions, NAV history included): {answer}")
    if not any((args.seed_history, args.push_ib, args.push_wallets, args.daily, args.install_daily, args.uninstall_daily)):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
