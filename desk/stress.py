"""Scenario stress: what a correlated shock would cost the book, beside the 33% stop-risk cap.

The aggregate cap (desk/risk.py) adds each position's stop-based risk. That bounds the loss when
every stop holds, but it does not look at the book's direction: this book is short alts and long
TradFi, so one regime shift hurts both sides at once, and a gap, or a position with no stop, is not
bounded by it. Each scenario below moves every position's price at once, with no stop assumed to
help, and prices the loss on the whole desk (HL perps, IB, and the wallets' tokens, so the staked
VVV offsets the VVV short):

* alt rally: alts +25%, BTC and ETH +10%, stablecoins and everything else unchanged;
* equity sell-off: equities (IB stocks and HL equity perps) -10%, everything else unchanged.

Limits, as a share of desk equity: a scenario loss of 25% is a warning; at 35% the pre-flight
check blocks a new entry that would add to it (it shrinks the entry to fit, or refuses it).
"""

from __future__ import annotations

import re

WARN_PCT = 0.25
BLOCK_PCT = 0.35

SCENARIOS = {
    "alt rally": {"alt": 0.25, "major": 0.10},
    "equity sell-off": {"equity": -0.10},
}
MAJORS = {"BTC", "ETH", "WBTC", "CBBTC", "WETH", "CBETH", "STETH", "WSTETH"}
STABLES = {"USDC", "USDT", "USDE", "DAI", "USDH", "USDBC", "EURC", "GHO", "PYUSD", "FDUSD", "USDS", "SUSDE"}
COMMODITIES = {"GOLD", "SILVER", "BRENTOIL", "WTIOIL", "OIL", "CL", "COPPER", "NATGAS", "PLATINUM", "PALLADIUM",
               "URANIUM", "PAXG", "XAUT"}


def classify(asset_class: str, symbol: str) -> str:
    """"alt", "major", "stable", "equity" or "other" for a position, from its asset class and symbol."""
    base = re.split(r"[ (]", str(symbol).split(":")[-1].strip())[0].upper()
    if asset_class in ("crypto perp", "crypto spot"):
        if base in STABLES:
            return "stable"
        return "major" if base in MAJORS else "alt"
    if asset_class == "equity":
        return "equity"
    if asset_class == "tradfi perp" or ":" in str(symbol):  # HL HIP-3 markets (xyz:NVDA, xyz:GOLD)
        return "other" if base in COMMODITIES else "equity"
    return "other"


def scenario_losses(exposures) -> dict[str, float]:
    """Loss in USD (negative is a gain) of each scenario for ``(asset_class, symbol, signed_notional)`` rows."""
    losses = {}
    for name, shocks in SCENARIOS.items():
        total = 0.0
        for asset_class, symbol, notional in exposures:
            if notional == notional:
                total -= notional * shocks.get(classify(asset_class, symbol), 0.0)
        losses[name] = total
    return losses


def evaluate(exposures, equity: float) -> list[dict]:
    """Each scenario's loss, its share of ``equity`` and its status: ok, warn (25%) or block (35%)."""
    rows = []
    for name, loss in scenario_losses(exposures).items():
        share = loss / equity if equity and equity > 0 else float("nan")
        status = "block" if share >= BLOCK_PCT else "warn" if share >= WARN_PCT else "ok"
        rows.append({"scenario": name, "loss": loss, "share": share, "status": status})
    return rows


def notices(exposures, equity: float) -> list[tuple[str, str]]:
    """Dashboard attention rows for scenarios at warn or block."""
    rows = []
    for item in evaluate(exposures, equity):
        if item["status"] == "ok":
            continue
        shock = "alts +25% and BTC/ETH +10%" if item["scenario"] == "alt rally" else "equities -10%"
        limit = "adding entries blocked" if item["status"] == "block" else f"entries shrunk past {BLOCK_PCT:.0%}"
        rows.append((f"Stress: {item['scenario']} ({item['status']})",
                     f"{shock}: {-item['share']:.0%} of desk equity; {limit}."))
    return rows


def fit_entry(exposures, equity: float, asset_class: str, symbol: str, notional: float) -> tuple[float, str]:
    """How much of a new entry (signed ``notional``) fits under the block limit: ``(fraction, note)``.

    ``fraction`` is 1 when it fits, between 0 and 1 when it must be shrunk, 0 when it cannot be added. Only
    scenarios the entry makes worse count; the note names the binding scenario, or a warning above 25%.
    """
    kind = classify(asset_class, symbol)
    fraction, note = 1.0, ""
    base = scenario_losses(exposures)
    for name, shocks in SCENARIOS.items():
        extra = -notional * shocks.get(kind, 0.0)
        if extra <= 0:
            continue
        room = BLOCK_PCT * equity - base[name]
        if room < extra:
            allowed = max(0.0, room / extra)
            if allowed < fraction:
                fraction = allowed
                note = (f"{name} stress: the desk would lose {(base[name] + extra) / equity:.0%} of equity "
                        f"(limit {BLOCK_PCT:.0%}); " + ("entry refused" if allowed == 0 else f"entry cut to {allowed:.0%}"))
        elif not note and (base[name] + extra) / equity >= WARN_PCT:
            note = f"{name} stress: the desk would lose {(base[name] + extra) / equity:.0%} of equity (warning at {WARN_PCT:.0%})"
    return fraction, note


def position_exposures(snapshot) -> list[tuple[str, str, float]]:
    """``(asset_class, symbol, signed notional USD)`` for one venue snapshot's positions (desk.venues.base)."""
    return [(position.asset_class, position.symbol, position.notional_usd) for position in snapshot.positions]


def wallet_exposures() -> list[tuple[str, str, float]]:
    """The HL wallets' tokens on other chains (cached, priced at DefiLlama): they offset hedges such as VVV."""
    from desk.accounts import accounts_for
    from desk.venues import onchain

    rows = []
    for account in accounts_for("hl"):
        if account.onchain:
            try:
                snapshot, _ = onchain.wallet_snapshot_cached(account)
            except Exception:
                continue
            rows += position_exposures(snapshot)
    return rows


def desk_exposures(book) -> list[tuple[str, str, float]]:
    """The positions of the other desk venues (IB, as read for the risk book) and the wallets' tokens."""
    rows = []
    for _, snapshot in getattr(book, "ib_reads", []):
        rows += position_exposures(snapshot)
    return rows + wallet_exposures()
