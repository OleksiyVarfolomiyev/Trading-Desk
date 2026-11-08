"""Historical net and gross exposure of the HL book, per leg, rebuilt from fills and the ledger.

Perp positions come from fills: every fill reports the position before it, so the path is
exact for as far back as fills reach. Spot balances are rebuilt backward from today's balances
(spot plus staked HYPE) using spot fills and token transfers, so anything held before the
first recorded event counts as held from the start. Stablecoins are cash and carry no exposure.

Legs: BTC (perp BTC and wrapped BTC), Alts (every other crypto perp or token, including
crypto HIP-3 markets) and TradFi (HIP-3 stocks, commodities and indices).
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

LEGS = ("BTC", "Alts", "TradFi")
STABLECOINS = {"USDC", "USDT", "USDT0", "USDE", "USDH", "FEUSD", "USDHL", "USR"}
# Unit tokens bridged onto Hyperliquid, priced as the coin they wrap.
WRAPPED = {"UBTC": "BTC", "UETH": "ETH", "USOL": "SOL", "UFART": "FARTCOIN", "UPUMP": "PUMP"}
DEFAULT_FLAT_THRESHOLD = 0.10
# Gross exposure at or above this multiple of equity marks a regime as levered.
DEFAULT_LEVERAGE = 1.0
# Gold and other real-world assets listed on the main dex are TradFi, not alts.
TRADFI_MAIN_DEX = {"PAXG", "XAUT"}


def _ts(milliseconds: Any) -> pd.Timestamp:
    return pd.Timestamp(int(milliseconds), unit="ms", tz="UTC")


def perp_position_path(fills: Iterable[dict[str, Any]], wallet: str) -> pd.DataFrame:
    """Position after each perp fill: columns ts, wallet, kind, asset, size (signed units)."""
    rows = []
    for fill in fills:
        coin = str(fill.get("coin", ""))
        if not coin or coin.startswith("@") or fill.get("time") is None:
            continue
        signed = float(fill["sz"]) if fill.get("side") == "B" else -float(fill["sz"])
        rows.append({"ts": _ts(fill["time"]), "wallet": wallet, "kind": "perp", "asset": coin,
                     "size": float(fill.get("startPosition", 0.0)) + signed,
                     "start": float(fill.get("startPosition", 0.0))})
    frame = pd.DataFrame(rows, columns=["ts", "wallet", "kind", "asset", "size", "start"])
    return frame.sort_values("ts", kind="stable").reset_index(drop=True)


def _token_deltas(
    wallet: str,
    fills: Iterable[dict[str, Any]],
    ledger: Iterable[dict[str, Any]],
    spot_token: Callable[[str], str],
) -> pd.DataFrame:
    """Signed changes to each non-stable token balance: columns ts, asset, delta."""
    me = wallet.lower()
    rows = []
    for fill in fills:
        coin = str(fill.get("coin", ""))
        if not coin.startswith("@") or fill.get("time") is None:
            continue
        token = spot_token(coin)
        signed = float(fill["sz"]) if fill.get("side") == "B" else -float(fill["sz"])
        if str(fill.get("feeToken", "")) == token:
            signed -= float(fill.get("fee") or 0.0)
        rows.append({"ts": _ts(fill["time"]), "asset": token, "delta": signed})
    for event in ledger:
        delta = event.get("delta", {})
        token = delta.get("token")
        # Staking moves HYPE between spot and staking, both of which are counted as held.
        if not token or delta.get("type") == "cStakingTransfer":
            continue
        sender = str(delta.get("user", "")).lower()
        receiver = str(delta.get("destination", "")).lower()
        if sender == receiver:
            continue
        amount = float(delta.get("amount") or 0.0)
        if receiver == me:
            rows.append({"ts": _ts(event["time"]), "asset": token, "delta": amount})
        elif sender == me:
            rows.append({"ts": _ts(event["time"]), "asset": token, "delta": -amount})
    frame = pd.DataFrame(rows, columns=["ts", "asset", "delta"])
    return frame[~frame["asset"].isin(STABLECOINS)].reset_index(drop=True)


def spot_balance_path(
    wallet: str,
    fills: Iterable[dict[str, Any]],
    ledger: Iterable[dict[str, Any]],
    balances: dict[str, float],
    spot_token: Callable[[str], str],
) -> pd.DataFrame:
    """Token balance after each change, rebuilt backward from ``balances`` (today's holdings).

    Returns ts, wallet, kind, asset, size, start; ``start`` on the first row of an asset is
    the balance before any recorded change.
    """
    deltas = _token_deltas(wallet, fills, ledger, spot_token)
    rows = []
    assets = (set(deltas["asset"]) | {a for a, v in balances.items() if v}) - STABLECOINS
    for asset in sorted(assets):
        changes = deltas[deltas["asset"] == asset].sort_values("ts", kind="stable")
        after = float(balances.get(asset, 0.0)) - changes["delta"][::-1].cumsum()[::-1].shift(-1, fill_value=0.0)
        before = after - changes["delta"]
        # Rounding and dust leave tiny negative balances; a spot balance cannot be short.
        for ts, start, size in zip(changes["ts"], before.clip(lower=0), after.clip(lower=0)):
            rows.append({"ts": ts, "wallet": wallet, "kind": "spot", "asset": asset, "size": size, "start": start})
        if changes.empty:
            rows.append({"ts": pd.Timestamp.now(tz="UTC"), "wallet": wallet, "kind": "spot", "asset": asset,
                         "size": float(balances[asset]), "start": float(balances[asset])})
    return pd.DataFrame(rows, columns=["ts", "wallet", "kind", "asset", "size", "start"])


def daily_holdings(paths: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Units held at the end of each day, summed over wallets: columns (kind, asset).

    ``days`` are day starts (UTC midnight). Before an asset's first change the ``start``
    balance applies, so a position opened before the fill history is still counted.
    """
    end_of_day = days + pd.Timedelta(days=1)
    columns = {}
    for (wallet, kind, asset), path in paths.groupby(["wallet", "kind", "asset"], sort=False):
        path = path.sort_values("ts", kind="stable")
        series = pd.Series(path["size"].to_numpy(), index=path["ts"])
        series = series[~series.index.duplicated(keep="last")]
        held = series.reindex(series.index.union(end_of_day)).ffill().reindex(end_of_day)
        held = held.fillna(float(path["start"].iloc[0])).to_numpy()
        key = (kind, asset)
        columns[key] = columns[key] + held if key in columns else held
    frame = pd.DataFrame(columns, index=days)
    if frame.empty:
        frame = pd.DataFrame(index=days, columns=pd.MultiIndex.from_tuples([], names=["kind", "asset"]))
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["kind", "asset"])
    return frame


def price_symbol(kind: str, asset: str) -> str:
    """The perp market used to price a holding (wrapped tokens use the coin they wrap)."""
    return WRAPPED.get(asset, asset) if kind == "spot" else asset


def leg_of(kind: str, asset: str, market_group: Callable[[str], str]) -> str:
    """BTC, Alts or TradFi for one holding."""
    symbol = price_symbol(kind, asset)
    if symbol == "BTC":
        return "BTC"
    if symbol in TRADFI_MAIN_DEX:
        return "TradFi"
    if kind == "spot":
        return "Alts"
    return "Alts" if market_group(symbol) == "Perp Crypto" else "TradFi"


def exposure_by_leg(
    holdings: pd.DataFrame,
    closes: pd.DataFrame,
    equity: pd.Series,
    market_group: Callable[[str], str],
) -> pd.DataFrame:
    """Daily net and gross exposure per leg, as a fraction of book equity.

    ``closes`` is days x price symbol. Returns columns ``Net <leg>``, ``Gross <leg>``,
    ``Net total``, ``Gross total`` and ``Equity``; days with no positive equity are dropped.
    """
    values = pd.DataFrame(index=holdings.index)
    legs = {}
    for kind, asset in holdings.columns:
        symbol = price_symbol(kind, asset)
        price = closes[symbol] if symbol in closes else pd.Series(np.nan, index=holdings.index)
        values[(kind, asset)] = holdings[(kind, asset)] * price.reindex(holdings.index).ffill()
        legs[(kind, asset)] = leg_of(kind, asset, market_group)
    out = pd.DataFrame(index=holdings.index)
    for leg in LEGS:
        members = [column for column, name in legs.items() if name == leg]
        leg_values = values[members].fillna(0.0) if members else pd.DataFrame(0.0, index=holdings.index, columns=[0])
        out[f"Net {leg}"] = leg_values.sum(axis=1)
        out[f"Gross {leg}"] = leg_values.abs().sum(axis=1)
    out["Net total"] = out[[f"Net {leg}" for leg in LEGS]].sum(axis=1)
    out["Gross total"] = out[[f"Gross {leg}" for leg in LEGS]].sum(axis=1)
    equity = equity.reindex(holdings.index).ffill()
    usable = equity > 0
    out = out[usable].div(equity[usable], axis=0)
    out["Equity"] = equity[usable]
    return out


def weekly_exposure(daily: pd.DataFrame, rule: str = "W-SUN") -> pd.DataFrame:
    """Average daily exposure within each week (equity is the week's last value)."""
    weekly = daily.drop(columns="Equity").resample(rule).mean()
    weekly["Equity"] = daily["Equity"].resample(rule).last()
    weekly.index = weekly.index.normalize()
    return weekly.dropna(subset=["Equity"])


def regime_label(
    row: pd.Series, threshold: float = DEFAULT_FLAT_THRESHOLD, leverage: float = DEFAULT_LEVERAGE,
) -> str:
    """Name a period by each leg's net sign (a leg under ``threshold`` is flat) and its leverage."""
    parts = []
    for leg in LEGS:
        net = row[f"Net {leg}"]
        if pd.notna(net) and abs(net) >= threshold:
            parts.append(f"{'long' if net > 0 else 'short'} {leg if leg != 'Alts' else 'alts'}")
    if not parts:
        return "Flat"
    label = ", ".join(parts)
    label = label[0].upper() + label[1:]
    return f"{label}, levered" if row["Gross total"] >= leverage else label


def label_regimes(
    weekly: pd.DataFrame,
    threshold: float = DEFAULT_FLAT_THRESHOLD,
    leverage: float = DEFAULT_LEVERAGE,
    min_run: int = 6,
) -> pd.Series:
    """Split the weeks into contiguous regimes and name each one with its dates.

    Each week gets a label from ``regime_label``. A run of the same label shorter than
    ``min_run`` weeks is folded into the regime before it (or, at the start, the one after),
    so a single odd week does not become a regime. Short runs after the last long one form
    the current regime, however short it is (runs up to the last one that returns to the
    previous regime's label still belong to that regime). Each regime is named from its
    average net and gross exposure.
    """
    labels = weekly.apply(regime_label, axis=1, threshold=threshold, leverage=leverage)
    if labels.empty:
        return labels.rename("Regime")
    runs = []  # [label, first position, last position]
    for position, label in enumerate(labels):
        if runs and runs[-1][0] == label:
            runs[-1][2] = position
        else:
            runs.append([label, position, position])
    long_runs = [index for index, run in enumerate(runs) if run[2] - run[1] + 1 >= min_run]
    segments = []  # [label, first, last]
    if not long_runs:
        segments.append([labels.iloc[-1], 0, len(labels) - 1])
    else:
        # Trailing short runs up to the last one that repeats the previous regime's label are a
        # detour within that regime, not the start of a new one.
        last_long = long_runs[-1]
        repeats = [index for index in range(last_long + 1, len(runs)) if runs[index][0] == runs[last_long][0]]
        current_from = (repeats[-1] if repeats else last_long) + 1
        for index, run in enumerate(runs):
            if index in long_runs:
                start = 0 if not segments else run[1]
                segments.append([run[0], start, run[2]])
            elif index >= current_from:
                if segments[-1][2] < run[1] - 1 or segments[-1][0] != "__current__":
                    segments.append(["__current__", run[1], run[2]])
                else:
                    segments[-1][2] = run[2]
            elif segments:
                segments[-1][2] = run[2]
    names = pd.Series(index=labels.index, dtype=object)
    for _, first, last in segments:
        # Name each regime from its average exposure, so "levered" means the regime as a whole
        # ran above ``leverage`` x equity, not just its latest week.
        label = regime_label(weekly.iloc[first:last + 1].mean(numeric_only=True), threshold, leverage)
        start, end = labels.index[first], labels.index[last]
        names.iloc[first:last + 1] = f"{label} ({start:%b %Y}–{end:%b %Y})"
    return names.rename("Regime")

def fetch_book_exposure(
    wallets: Iterable[str], equity: pd.Series, start: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Daily exposure per leg for the given wallets, plus notes on anything left unpriced.

    ``equity`` is the book's daily value (all wallets), indexed by UTC day. Fills, the ledger,
    today's balances and staked HYPE come from the public HL info API; prices are cached
    daily perp candles.
    """
    from desk.hl.account_analytics import fetch_user_fills, perp_market_group, post, spot_base_token_names
    from desk.hl.trade_analytics import fetch_daily_candles

    equity = equity.copy()
    equity.index = pd.to_datetime(equity.index, utc=True).normalize()
    first = equity.index.min() if start is None else pd.Timestamp(start).tz_convert("UTC").normalize()
    days = pd.date_range(first, pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1), freq="D")
    spot_token = spot_base_token_names()
    paths, notes = [], []
    for wallet in wallets:
        fills = fetch_user_fills(wallet)
        if len(fills) >= 10_000:
            notes.append(f"{wallet[-4:]}: the API returns only the latest 10,000 fills; earlier positions "
                         "are inferred from the oldest fill's starting position.")
        ledger = post({"type": "userNonFundingLedgerUpdates", "user": wallet, "startTime": 0}) or []
        state = post({"type": "spotClearinghouseState", "user": wallet}) or {}
        balances = {b["coin"]: float(b["total"]) for b in state.get("balances", [])}
        staking = post({"type": "delegatorSummary", "user": wallet}) or {}
        staked = sum(float(staking.get(key) or 0.0) for key in ("delegated", "undelegated", "totalPendingWithdrawal"))
        if staked:
            balances["HYPE"] = balances.get("HYPE", 0.0) + staked
        paths.append(perp_position_path(fills, wallet))
        paths.append(spot_balance_path(wallet, fills, ledger, balances, spot_token))
    paths = pd.concat([p for p in paths if not p.empty], ignore_index=True) if paths else pd.DataFrame()
    holdings = daily_holdings(paths, days) if not paths.empty else pd.DataFrame(index=days)
    held = [column for column in holdings.columns if (holdings[column].abs() > 0).any()]
    holdings = holdings[held]
    closes, unpriced = {}, []
    lookback = int((pd.Timestamp.now(tz="UTC") - first).days) + 5
    for symbol in sorted({price_symbol(kind, asset) for kind, asset in held}):
        try:
            candles = fetch_daily_candles(symbol, lookback_days=lookback)
            closes[symbol] = pd.Series(candles["close"].astype(float).to_numpy(),
                                       index=pd.to_datetime(candles["time"], utc=True).dt.normalize())
        except Exception as exc:
            unpriced.append(f"{symbol} ({type(exc).__name__})")
    if unpriced:
        notes.append("No HL price history, so left out of exposure: " + ", ".join(unpriced) + ".")
    closes = pd.DataFrame(closes).reindex(days).ffill()
    return exposure_by_leg(holdings, closes, equity.reindex(days).ffill(), perp_market_group), notes
