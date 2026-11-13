"""Rebuild the daily value of the HL wallets' tokens on other chains, before recording began.

The desk records each wallet's on-chain value from run to run (``onchain_value_history_<wallet>.csv``),
which starts the day the recording was added. This module fills the days before that once:

* Chains with a public RPC (Base) are read exactly: each token's balance at every day's close,
  through ``eth_call`` at a past block. Only tokens with a balance at a monthly probe are read
  daily, so a token held for less than about a month between probes can be missed.
* Other chains are rebuilt backward from today's balances with the saved transfers
  (``onchain_activity_<wallet>.csv``): a token's balance on a past day is today's balance minus
  every net transfer since. Gas and rebasing are not transfers, so these are approximate, and a
  balance is never taken below zero.
* Every balance is valued at DefiLlama's price at that day's close; tokens it cannot price
  are left out, as in the live snapshot.

Run once per wallet (``python -m desk.venues.onchain_history``); the result is saved to
``onchain_value_backfill_<wallet>.csv`` and read by the dashboard wherever no recorded value
exists. Read-only: nothing here signs or sends a transaction.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import requests

from desk.accounts import Account
from desk.privacy import account_file
from desk.venues import onchain
from desk.venues.onchain import CHAINS, NATIVE, PUBLIC_RPC, STAKING_REWARDS, USER_AGENT, WELL_KNOWN_TOKENS
from desk.numbers import usd as _usd

LLAMA_CHART_URL = "https://coins.llama.fi/chart/"
LLAMA_PAGE_DAYS = 500  # DefiLlama serves at most 500 points per request
BLOCK_SECONDS = {"base": 2}  # Base produces a block every two seconds, without gaps
PROBE_DAYS = 30  # tokens are read daily only if they had a balance at one of these probes
BACKFILL_COLUMNS = ["date", "value_usd"]
PRICE_GAP_DAYS = 3


def backfill_path(data_dir: Path, wallet: str) -> Path:
    return account_file(data_dir, "onchain_value_backfill", wallet)


def day_closes(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """UTC dates from ``start`` to ``end`` (inclusive), each standing for that day's close."""
    return pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="1D")


def _close_time(day: pd.Timestamp) -> pd.Timestamp:
    """The moment a UTC date closes: the next midnight."""
    day = pd.Timestamp(day)
    day = day.tz_localize("UTC") if day.tzinfo is None else day.tz_convert("UTC")
    return day.normalize() + pd.Timedelta(days=1)


# ---------------------------------------------------------------------------
# Chains with a public RPC: exact balances at past blocks
# ---------------------------------------------------------------------------

def _rpc(url: str, method: str, params: list) -> str:
    for attempt in range(4):  # public nodes drop connections now and then
        try:
            response = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                     timeout=30, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            return response.json()["result"]
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def _block_stamps(url: str, numbers: list[int]) -> list[int]:
    """The real timestamps of these blocks."""
    calls = [{"jsonrpc": "2.0", "id": index, "method": "eth_getBlockByNumber", "params": [hex(number), False]}
             for index, number in enumerate(numbers)]
    results = onchain._rpc_batch(url, calls)
    return [int(results[index]["timestamp"], 16) for index in range(len(numbers))]


BLOCK_TOLERANCE_SECONDS = 30


def blocks_at(chain: str, moments: list[pd.Timestamp]) -> list[int]:
    """The block at or just before each moment (within ``BLOCK_TOLERANCE_SECONDS``), found by searching
    real block timestamps.

    A chain's block time drifts (Base's went from two seconds to a fraction of one), so a block counted
    back from the latest at the nominal time landed weeks from its day. The search keeps a bracket of
    blocks around each moment, guesses by interpolating inside it and bisects every fourth round, so it
    always converges; all moments share each round's batched request.
    """
    url = PUBLIC_RPC[chain]
    latest = _rpc(url, "eth_getBlockByNumber", ["latest", False])
    number, stamp = int(latest["number"], 16), int(latest["timestamp"], 16)
    first = _block_stamps(url, [0])[0]
    targets = [int(moment.timestamp()) for moment in moments]
    low, low_stamp = [0] * len(targets), [first] * len(targets)
    high, high_stamp = [number] * len(targets), [stamp] * len(targets)
    result: list[int | None] = [None] * len(targets)
    for index, target in enumerate(targets):
        if target >= stamp:
            result[index] = number
        elif target <= first:
            result[index] = 0
    for round_number in range(80):
        pending = [index for index, found in enumerate(result) if found is None]
        if not pending:
            break
        guesses = {}
        for index in pending:
            if high[index] - low[index] <= 1:
                result[index] = low[index]
                continue
            if round_number % 4 == 3 or high_stamp[index] <= low_stamp[index]:
                guess = (low[index] + high[index]) // 2
            else:
                share = (targets[index] - low_stamp[index]) / (high_stamp[index] - low_stamp[index])
                guess = low[index] + int(share * (high[index] - low[index]))
            guesses[index] = min(high[index] - 1, max(low[index] + 1, guess))
        if not guesses:
            continue
        order = list(guesses)
        for index, found in zip(order, _block_stamps(url, [guesses[index] for index in order])):
            if found <= targets[index]:
                low[index], low_stamp[index] = guesses[index], found
                if targets[index] - found <= BLOCK_TOLERANCE_SECONDS:
                    result[index] = guesses[index]
            else:
                high[index], high_stamp[index] = guesses[index], found
    return [low[index] if found is None else found for index, found in enumerate(result)]


def _balance_calls(chain: str, wallet: str, token: str, block: int, call_id: int) -> dict:
    padded = wallet.lower()[2:].rjust(64, "0")
    if token == NATIVE:
        return {"jsonrpc": "2.0", "id": call_id, "method": "eth_getBalance", "params": [wallet, hex(block)]}
    if token.startswith("rewards:"):
        contract = token.removeprefix("rewards:staking:")
        selector, _ = STAKING_REWARDS[(chain, contract)]
        return {"jsonrpc": "2.0", "id": call_id, "method": "eth_call",
                "params": [{"to": contract, "data": selector + padded}, hex(block)]}
    return {"jsonrpc": "2.0", "id": call_id, "method": "eth_call",
            "params": [{"to": token, "data": "0x70a08231" + padded}, hex(block)]}  # balanceOf


def _decimals(chain: str, tokens: list[str]) -> dict[str, int]:
    calls = [{"jsonrpc": "2.0", "id": index, "method": "eth_call",
              "params": [{"to": token, "data": "0x313ce567"}, "latest"]}  # decimals()
             for index, token in enumerate(tokens)]
    results = onchain._rpc_batch(PUBLIC_RPC[chain], calls) if calls else {}
    return {token: int(results[index], 16) for index, token in enumerate(tokens)
            if results.get(index) not in (None, "0x")}


def rpc_balances(chain: str, wallet: str, days: pd.DatetimeIndex, tokens: list[str] | None = None,
                 probe_days: int = PROBE_DAYS) -> pd.DataFrame:
    """Balances (in tokens) at each day's close on a chain with a public RPC: dates x token.

    ``tokens`` defaults to the native coin, the chain's well-known tokens and its staking
    rewards (as ``rewards:staking:<contract>``). Tokens are first probed every ``probe_days``
    and at the last day; only those with a balance at some probe are read daily.
    """
    if tokens is None:
        tokens = ([NATIVE] + [token.lower() for token in WELL_KNOWN_TOKENS.get(chain, ())]
                  + [f"rewards:staking:{contract}" for (name, contract) in STAKING_REWARDS if name == chain])
    erc20 = [token for token in tokens if token != NATIVE and not token.startswith("rewards:")]
    decimals = {NATIVE: 18, **_decimals(chain, erc20)}
    for token in tokens:
        if token.startswith("rewards:"):
            decimals[token] = 18
    tokens = [token for token in tokens if token in decimals]

    def read(sample: pd.DatetimeIndex, names: list[str]) -> pd.DataFrame:
        blocks = blocks_at(chain, [_close_time(day) for day in sample])
        calls, keys = [], []
        for row, block in enumerate(blocks):
            for token in names:
                calls.append(_balance_calls(chain, wallet, token, block, len(calls)))
                keys.append((sample[row], token))
        results = onchain._rpc_batch(PUBLIC_RPC[chain], calls) if calls else {}
        frame = pd.DataFrame(0.0, index=sample, columns=names)
        for call_id, (day, token) in enumerate(keys):
            raw = results.get(call_id)
            if raw and raw != "0x":
                frame.loc[day, token] = int(raw, 16) / 10 ** decimals[token]
        return frame

    probes = days[::probe_days].union(days[-1:])
    probed = read(probes, tokens)
    held = [token for token in tokens if (probed[token] > 0).any()]
    return read(days, held) if held else pd.DataFrame(index=days)


# ---------------------------------------------------------------------------
# Other chains: today's balances rolled back through the saved transfers
# ---------------------------------------------------------------------------

def transfer_balances(activity: pd.DataFrame, current: dict[tuple[str, str], float],
                      days: pd.DatetimeIndex, chains: set[str]) -> pd.DataFrame:
    """Balances at each day's close, rebuilt backward from ``current`` with ``activity``.

    ``current`` maps (chain, token) to today's amount. Columns are ``chain:token``; a
    balance is floored at zero because gas and other untracked flows can push it negative.
    """
    frame = activity[activity["chain"].isin(chains)].copy() if len(activity) else activity
    columns = {(chain, token) for chain, token in current if chain in chains}
    if len(frame):
        frame["token"] = frame["token"].str.lower()
        frame["time"] = pd.to_datetime(frame["time"], utc=True, format="mixed")
        frame["signed"] = frame["amount"].where(frame["direction"] == "in", -frame["amount"])
        frame = frame[frame["direction"].isin(["in", "out"])]
        columns |= set(zip(frame["chain"], frame["token"]))
    result = pd.DataFrame(index=days)
    closes = [_close_time(day) for day in days]
    for chain, token in sorted(columns):
        flows = frame[(frame["chain"] == chain) & (frame["token"] == token)] if len(frame) else frame
        flows = flows.sort_values("time")
        now = current.get((chain, token), 0.0)
        after = [flows.loc[flows["time"] >= close, "signed"].sum() if len(flows) else 0.0 for close in closes]
        result[f"{chain}:{token}"] = [max(0.0, now - moved) for moved in after]
    return result


# ---------------------------------------------------------------------------
# Prices and value
# ---------------------------------------------------------------------------

def llama_closes(key: str, days: pd.DatetimeIndex) -> pd.Series:
    """DefiLlama prices at each day's close (the next midnight UTC) for one ``chain:address`` key."""
    start = _close_time(days[0])
    end = _close_time(days[-1])
    points = []
    while start <= end:
        span = min(LLAMA_PAGE_DAYS, int((end - start).days) + 1)
        response = onchain._get(LLAMA_CHART_URL + key,
                                params={"start": int(start.timestamp()), "span": span, "period": "1d"})
        coin = (response.json().get("coins", {}).get(key) if response.ok else None) or {}
        if float(coin.get("confidence") or 1) >= onchain.MIN_PRICE_CONFIDENCE:
            points += coin.get("prices", [])
        start += pd.Timedelta(days=span)
    if not points:
        return pd.Series(index=days, dtype=float)
    prices = pd.Series({pd.Timestamp(point["timestamp"], unit="s", tz="UTC"): float(point["price"])
                        for point in points}).sort_index()
    moments = pd.DatetimeIndex([_close_time(day) for day in days])
    # DefiLlama's daily points land a few seconds either side of midnight, so take the nearest
    # within 12 hours; a missing day carries the last price for up to three days, no longer, so
    # a delisted token is not valued on.
    aligned = prices[~prices.index.duplicated(keep="last")].reindex(
        moments, method="nearest", tolerance=pd.Timedelta(hours=12))
    return pd.Series(aligned.to_numpy(), index=days).ffill(limit=PRICE_GAP_DAYS)


def price_key(chain: str, token: str) -> str:
    """DefiLlama key for a balance column's token; staking rewards are priced as the reward token."""
    if token.startswith("rewards:staking:"):
        token = STAKING_REWARDS[(chain, token.removeprefix("rewards:staking:"))][1]
    return onchain.price_key(CHAINS[chain], token)


def value_balances(balances: pd.DataFrame, price_of=llama_closes) -> tuple[pd.Series, list[str]]:
    """Daily USD value of ``chain:token`` balances, and the tokens left unpriced."""
    total = pd.Series(0.0, index=balances.index)
    unpriced = []
    for column in balances.columns:
        if not (balances[column] > 0).any():
            continue
        chain, token = column.split(":", 1)
        prices = price_of(price_key(chain, token), balances.index)
        if prices.isna().all():
            unpriced.append(column)
            continue
        total += (balances[column] * prices).fillna(0.0)
    return total, unpriced


def current_balances(snapshot) -> dict[tuple[str, str], float]:
    """(chain, token) -> amount from a wallet snapshot's positions (``ref`` is ``chain:token``)."""
    amounts = {}
    for position in snapshot.positions:
        chain, token = position.ref.split(":", 1)
        amounts[(chain, token.lower())] = position.quantity
    return amounts


def wallet_balances(account: Account, data_dir: Path, days: pd.DatetimeIndex, snapshot=None,
                    notes: list[str] | None = None) -> pd.DataFrame:
    """Each ``chain:token`` balance of one wallet at every day's close: read exactly on public-RPC chains,
    rebuilt backward from today's balances and the saved transfers elsewhere."""
    notes = [] if notes is None else notes
    if snapshot is None:
        snapshot, read_notes = onchain.wallet_snapshot(account)
        notes += [f"current balances: {note}" for note in read_notes]
    failed = {chain for chain in CHAINS for note in snapshot.notes if note.startswith(f"{chain} unavailable")}
    parts = []
    for chain in PUBLIC_RPC:
        balances = rpc_balances(chain, account.id, days)
        parts.append(balances.rename(columns=lambda token, c=chain: f"{c}:{token}"))
    path = account_file(data_dir, "onchain_activity", account.id)
    activity = pd.read_csv(path) if path.exists() else pd.DataFrame()
    other = set(CHAINS) - set(PUBLIC_RPC) - failed
    if failed:
        notes.append(f"{', '.join(sorted(failed))}: today's balances could not be read, so their past "
                     "balances are left out")
    parts.append(transfer_balances(activity, current_balances(snapshot), days, other))
    return pd.concat(parts, axis=1).fillna(0.0)


def rebuild_wallet_values(account: Account, data_dir: Path, start: pd.Timestamp, end: pd.Timestamp,
                          snapshot=None, notes: list[str] | None = None) -> tuple[pd.Series, list[str]]:
    """Daily value of one wallet's tokens outside HL from ``start`` to ``end``, with notes."""
    notes = [] if notes is None else notes
    balances = wallet_balances(account, data_dir, day_closes(start, end), snapshot, notes)
    value, unpriced = value_balances(balances)
    if unpriced:
        notes.append(f"no DefiLlama price history for {len(unpriced)} token(s): {', '.join(unpriced)}")
    return value.round(2), notes


def save_backfill(account: Account, data_dir: Path, start: pd.Timestamp, end: pd.Timestamp | None = None,
                  ) -> tuple[Path, pd.Series, list[str]]:
    """Rebuild one wallet's daily values up to the day before recording began, and save them."""
    from desk.overview import wallet_history_path

    recorded = wallet_history_path(data_dir, account.id)
    if end is None:
        if recorded.exists() and len(saved := pd.read_csv(recorded)):
            end = pd.to_datetime(saved["ts"], utc=True, format="mixed").min().normalize() - pd.Timedelta(days=1)
        else:
            end = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1)
    started = time.time()
    value, notes = rebuild_wallet_values(account, data_dir, start, end)
    path = backfill_path(data_dir, account.id)
    frame = pd.DataFrame({"date": [day.strftime("%Y-%m-%d") for day in value.index], "value_usd": value.to_numpy()})
    frame.to_csv(path, index=False)
    notes.append(f"rebuilt {len(frame)} days in {time.time() - started:.0f}s")
    return path, value, notes


def load_backfill(data_dir: Path, wallet: str) -> pd.Series:
    """Saved rebuilt values for one wallet (empty if never rebuilt), indexed by UTC date."""
    path = backfill_path(data_dir, wallet)
    if not path.exists():
        return pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    saved = pd.read_csv(path)
    return pd.Series(saved["value_usd"].to_numpy(), index=pd.to_datetime(saved["date"], utc=True))


# ---------------------------------------------------------------------------
# Performance: what the tokens held each morning earned from price moves
# ---------------------------------------------------------------------------

RETURN_COLUMNS = ["date", "pnl_usd", "base_usd"]


def returns_path(data_dir: Path, wallet: str) -> Path:
    return account_file(data_dir, "onchain_returns", wallet)


def price_moves(balances: pd.DataFrame, price_of=llama_closes) -> tuple[pd.DataFrame, list[str]]:
    """Daily ``pnl_usd`` and ``base_usd`` of the tokens held at the previous close.

    A day's PnL is the previous close's balance times the day's price change; its base is that balance's
    value at the previous close. Deposits, withdrawals and swaps change a balance only from the next day, so
    they never count as performance. A token without a price on either day adds nothing that day.
    """
    pnl = pd.Series(0.0, index=balances.index)
    base = pd.Series(0.0, index=balances.index)
    unpriced = []
    for column in balances.columns:
        if not (balances[column] > 0).any():
            continue
        chain, token = column.split(":", 1)
        prices = price_of(price_key(chain, token), balances.index)
        if prices.isna().all():
            unpriced.append(column)
            continue
        held = balances[column].shift(1)
        before = prices.shift(1)
        known = prices.notna() & before.notna()
        pnl += (held * (prices - before)).where(known, 0.0).fillna(0.0)
        base += (held * before).where(known, 0.0).fillna(0.0)
    return pd.DataFrame({"pnl_usd": pnl, "base_usd": base}).iloc[1:], unpriced


def update_wallet_returns(account: Account, data_dir: Path, start: pd.Timestamp | None = None,
                          ) -> tuple[pd.DataFrame, list[str]]:
    """Extend ``onchain_returns_<wallet>.csv`` with the days up to yesterday and return all saved days.

    The first run starts where the value rebuild starts; later runs read from the last saved day, so a
    daily run costs a few requests. Read-only: nothing here signs or sends a transaction.
    """
    path = returns_path(data_dir, account.id)
    saved = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=RETURN_COLUMNS)
    yesterday = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1)
    if len(saved):
        begin = pd.Timestamp(saved["date"].max(), tz="UTC")
    else:
        rebuilt = load_backfill(data_dir, account.id)
        begin = start if start is not None else (rebuilt.index[0] if len(rebuilt) else yesterday - pd.Timedelta(days=30))
    if begin >= yesterday:
        return saved, []
    notes: list[str] = []
    balances = wallet_balances(account, data_dir, day_closes(begin, yesterday), None, notes)
    moves, unpriced = price_moves(balances)
    if unpriced:
        notes.append(f"no DefiLlama price history for {len(unpriced)} token(s): {', '.join(unpriced)}")
    fresh = moves.reset_index(names="date").assign(date=lambda frame: frame["date"].dt.strftime("%Y-%m-%d"))
    merged = pd.concat([saved, fresh[RETURN_COLUMNS]]).drop_duplicates("date", keep="last").sort_values("date")
    merged.to_csv(path, index=False)
    return merged, notes


def load_returns(data_dir: Path, wallet: str) -> pd.DataFrame:
    """Saved daily ``pnl_usd`` and ``base_usd`` (empty if never built), indexed by UTC date."""
    path = returns_path(data_dir, wallet)
    if not path.exists():
        return pd.DataFrame(columns=RETURN_COLUMNS[1:], index=pd.DatetimeIndex([], tz="UTC"))
    saved = pd.read_csv(path)
    return saved.set_index(pd.to_datetime(saved["date"], utc=True))[RETURN_COLUMNS[1:]]


if __name__ == "__main__":
    import argparse

    from desk.accounts import ROOT, load_accounts

    parser = argparse.ArgumentParser(description="Rebuild each HL wallet's on-chain value before recording began.")
    parser.add_argument("--start", default=None, help="First date (YYYY-MM-DD); default one year ago.")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--returns", action="store_true",
                        help="extend each wallet's daily price-move performance (onchain_returns_<wallet>.csv) to yesterday")
    args = parser.parse_args()
    if args.returns:
        from desk.privacy import mask_output

        mask_output()
        for account in load_accounts():
            if account.venue == "hl" and account.onchain:
                started = time.time()
                frame, notes = update_wallet_returns(account, Path(args.data_dir), pd.Timestamp(args.start, tz="UTC") if args.start else None)
                print(f"{account.name}: {len(frame)} days to {frame['date'].max() if len(frame) else 'n/a'} "
                      f"({time.time() - started:.0f}s)")
                for note in notes:
                    print(f"  {note}")
        raise SystemExit(0)
    start = pd.Timestamp(args.start, tz="UTC") if args.start else pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=365)
    for account in load_accounts():
        if account.venue != "hl" or not account.onchain:
            continue
        path, value, notes = save_backfill(account, Path(args.data_dir), start)
        print(f"{account.name}: {len(value)} days to {value.index[-1]:%Y-%m-%d}, last {_usd(value.iloc[-1])} -> {path}")
        for note in notes:
            print(f"  {note}")
