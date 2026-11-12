"""Daily candidate screener and market-making coin selection for Hyperliquid perps.

The screener evaluates liquid perps against the signals tested in ``desk.hl.signal_research``:

* 7-day and 30-day relative strength vs BTC (``rel7``, ``rel30``);
* distance from the 60-day high (``from_high60``);
* current 8h funding rate (``funding8h``);
* liquidity (24h volume and open interest).

Each candidate displays the executor's planned size, 3 x ATR stop price, risk budget,
historical rule warnings (``desk.trade_rules``), and impact on the desk's scenario stress limits
(``desk.stress``).

It also selects range-bound, liquid candidates for market-making ladders (``hl_daemon.py ladder``),
evaluated on range position, volatility, and historical spread captured per dollar of volume.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from desk.accounts import load_accounts
from desk.hl import trade_analytics as ta
from desk.risk import HL_STOP_ATR_MULTIPLE as STOP_ATR_MULTIPLE, per_position_budget
from desk import stress, trade_rules

CANDIDATES_FILE = Path(__file__).resolve().parents[1] / "data" / "candidates.json"
EXCLUDED_COINS = {"BTC", "ETH", "USDC", "USDT", "USDE", "DAI", "PAXG", "XAUT"}
MIN_24H_VOLUME_USD = 10_000_000.0


@dataclass
class ScreenerCandidate:
    coin: str
    mark: float
    side: str
    rel7: float
    rel30: float
    from_high60: float
    funding8h: float
    volume24h: float
    open_interest: float
    atr14: float
    planned_size: float
    planned_notional: float
    stop_price: float
    risk_usd: float
    stress_status: str
    rule_notes: list[str]
    command: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def fetch_liquid_perps(min_volume: float = MIN_24H_VOLUME_USD) -> list[dict[str, Any]]:
    """Fetch all active main-DEX perps meeting minimum 24h volume."""
    meta, contexts = ta.post_info({"type": "metaAndAssetCtxs"})
    universe = meta["universe"]
    liquid = []
    for asset, ctx in zip(universe, contexts):
        coin = asset["name"]
        if coin in EXCLUDED_COINS or coin.startswith("@") or "/" in coin:
            continue
        volume = float(ctx.get("dayNtlVlm") or 0)
        if volume < min_volume:
            continue
        liquid.append({
            "name": coin,
            "sz_decimals": int(asset["szDecimals"]),
            "max_leverage": int(asset.get("maxLeverage", 20)),
            "mark": float(ctx.get("markPx") or ctx.get("midPx") or 0),
            "prev_day_px": float(ctx.get("prevDayPx") or 0),
            "funding_hourly": float(ctx.get("funding") or 0),
            "volume_24h": volume,
            "open_interest": float(ctx.get("openInterest") or 0) * float(ctx.get("markPx") or 0),
        })
    return liquid


def _calculate_metrics(coin: str, mark: float, btc_candles: pd.DataFrame) -> dict[str, float] | None:
    """Calculate rel7, rel30, from_high60, and ATR(14) using cached completed daily candles."""
    try:
        candles = ta.fetch_daily_candles(coin, lookback_days=65)
    except Exception:
        return None
    if len(candles) < 30:
        return None
    close = candles["close"].to_numpy()
    high = candles["high"].to_numpy()

    # Align with BTC dates
    merged = pd.merge(candles[["time", "close"]], btc_candles[["time", "close"]], on="time", suffixes=("_alt", "_btc"))
    if len(merged) < 30:
        return None

    alt_close = merged["close_alt"].to_numpy()
    btc_close = merged["close_btc"].to_numpy()

    ratio = alt_close / btc_close
    rel7 = float(ratio[-1] / ratio[-7] - 1) if len(ratio) >= 7 else 0.0
    rel30 = float(ratio[-1] / ratio[-30] - 1) if len(ratio) >= 30 else 0.0

    high60 = float(high[-min(60, len(high)):].max())
    from_high60 = float(mark / high60 - 1) if high60 > 0 else 0.0

    atr14 = float(ta.wilder_atr(candles, 14))

    return {
        "rel7": rel7,
        "rel30": rel30,
        "from_high60": from_high60,
        "atr14": atr14,
    }


def screen_candidates(
    desk_equity: float | None = None,
    current_exposures: list[tuple[str, str, float]] | None = None,
    min_volume: float = MIN_24H_VOLUME_USD,
) -> list[ScreenerCandidate]:
    """Rank liquid perps on momentum, high distance, funding, and compute risk-fitted sizing."""
    if desk_equity is None or current_exposures is None:
        from desk.overview import DeskOverview
        overview = DeskOverview(load_accounts())
        try:
            overview.fetch_data(include_risk=True)
            desk_equity = overview.desk_equity
            current_exposures = [
                row for snapshot in overview.snapshots for row in stress.position_exposures(snapshot)
            ]
        except Exception:
            desk_equity = desk_equity or 10_000.0
            current_exposures = current_exposures or []

    btc_candles = ta.fetch_daily_candles("BTC", lookback_days=65)
    liquid = fetch_liquid_perps(min_volume)
    candidates = []

    risk_budget = per_position_budget(desk_equity)

    for item in liquid:
        coin = item["name"]
        mark = item["mark"]
        if mark <= 0:
            continue
        m = _calculate_metrics(coin, mark, btc_candles)
        if m is None or m["atr14"] <= 0:
            continue

        rel7 = m["rel7"]
        rel30 = m["rel30"]
        from_high60 = m["from_high60"]
        atr14 = m["atr14"]
        funding8h = item["funding_hourly"] * 8 * 100

        # Side selection: weakness against BTC selects SHORT (desk edge); strong momentum selects LONG
        side = "SHORT" if (rel7 <= 0 or rel30 <= 0) else "LONG"

        stop_dist = STOP_ATR_MULTIPLE * atr14
        stop_price = mark + stop_dist if side == "SHORT" else mark - stop_dist

        step = 10 ** -item["sz_decimals"]
        raw_size = risk_budget / stop_dist
        planned_size = round(math.floor(raw_size / step + 1e-9) * step, item["sz_decimals"])
        planned_notional = planned_size * mark

        # Stress check impact
        signed_notional = -planned_notional if side == "SHORT" else planned_notional
        fraction, stress_note = stress.fit_entry(
            current_exposures, desk_equity, "crypto perp", coin, signed_notional
        )
        if fraction < 1.0:
            stress_status = f"shrunk to {fraction:.0%}" if fraction > 0 else "blocked by stress cap"
        else:
            stress_status = "ok"

        # Trade rules warnings
        notes = trade_rules.entry_notes(coin, side, planned_notional, "directional")

        cmd = f"python execution/hl_trade.py --signals \"[{{'coin': '{coin}', 'side': '{side}'}}]\""

        candidates.append(ScreenerCandidate(
            coin=coin,
            mark=mark,
            side=side,
            rel7=rel7,
            rel30=rel30,
            from_high60=from_high60,
            funding8h=funding8h,
            volume24h=item["volume_24h"],
            open_interest=item["open_interest"],
            atr14=atr14,
            planned_size=planned_size,
            planned_notional=planned_notional,
            stop_price=stop_price,
            risk_usd=planned_size * stop_dist,
            stress_status=stress_status,
            rule_notes=notes,
            command=cmd,
        ))

    # Rank: shorts by weakness (most negative rel30 & rel7, furthest from high),
    # longs by strength.
    candidates.sort(key=lambda c: (c.side == "SHORT", -abs(c.rel30) if c.side == "SHORT" else c.rel30), reverse=True)
    return candidates


def candidates_frame(candidates: list[ScreenerCandidate]) -> pd.DataFrame:
    """Turn candidates into a presentation DataFrame."""
    rows = []
    for c in candidates:
        rows.append({
            "Coin": c.coin,
            "Side": c.side,
            "Mark": c.mark,
            "rel7 vs BTC": c.rel7,
            "rel30 vs BTC": c.rel30,
            "From 60d High": c.from_high60,
            "8h Funding": c.funding8h,
            "24h Volume": c.volume24h,
            "Planned Size": c.planned_size,
            "Planned USD": c.planned_notional,
            "Stop Price": c.stop_price,
            "Risk (USD)": c.risk_usd,
            "Stress": c.stress_status,
            "Rule Warnings": "; ".join(c.rule_notes) if c.rule_notes else "none",
        })
    return pd.DataFrame(rows)


def show_candidates(top: int = 15) -> None:
    """Display the Candidates table in interactive format."""
    from desk.hl.account_analytics import display_interactive_table

    candidates = screen_candidates()
    if not candidates:
        print("No candidates found meeting volume and liquidity criteria.")
        return
    frame = candidates_frame(candidates[:top])

    money = lambda v: f"${v:,.2f}" if v == v else ""
    vol_money = lambda v: f"${v / 1e6:,.1f}M" if v == v else ""
    pct = lambda v: f"{v:.1%}" if v == v else ""
    funding_pct = lambda v: f"{v:.4f}%" if v == v else ""

    display_interactive_table(
        frame,
        title=f"DAILY CANDIDATE SCREENER (TOP {len(frame)} LIQUID PERPS)",
        formatters={
            "Mark": money,
            "rel7 vs BTC": pct,
            "rel30 vs BTC": pct,
            "From 60d High": pct,
            "8h Funding": funding_pct,
            "24h Volume": vol_money,
            "Planned USD": money,
            "Stop Price": money,
            "Risk (USD)": money,
        },
        key_columns={"rel30 vs BTC": "min"},
    )
    print("Candidates are sized using the executor's 3 x ATR stop budget. Stress status reflects the desk-wide alt-rally/equity-selloff cap.")


# ---------------------------------------------------------------------------
# Market-Making Ladder Coin Selection
# ---------------------------------------------------------------------------

@dataclass
class LadderCandidate:
    coin: str
    mark: float
    volume24h: float
    atr_pct: float
    range_pos60: float
    trend_rel30: float
    historical_spread_bps: float
    mm_score: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def ladder_candidates(min_volume: float = 15_000_000.0, top: int = 10) -> list[LadderCandidate]:
    """Score liquid coins for market-making ladders: range-bound, liquid, with good spread capture."""
    from desk import market_making

    btc_candles = ta.fetch_daily_candles("BTC", lookback_days=65)
    liquid = fetch_liquid_perps(min_volume)

    # Historical spread capture performance if available
    mm_history: dict[str, float] = {}
    try:
        past_ladders = market_making.market_making_frame()
        for _, row in past_ladders.iterrows():
            coin = row.get("Coin")
            vol = float(row.get("Volume") or 0)
            spread = float(row.get("Spread captured") or 0)
            if vol > 0 and coin:
                mm_history[coin] = (spread / vol) * 10_000  # bps
    except Exception:
        pass

    candidates = []
    for item in liquid:
        coin = item["name"]
        mark = item["mark"]
        if mark <= 0:
            continue
        try:
            candles = ta.fetch_daily_candles(coin, lookback_days=65)
        except Exception:
            continue
        if len(candles) < 30:
            continue

        high60 = float(candles["high"][-60:].max())
        low60 = float(candles["low"][-60:].min())
        range_span = high60 - low60
        if range_span <= 0:
            continue

        range_pos = (mark - low60) / range_span  # 0.0 at low, 1.0 at high
        atr14 = float(ta.wilder_atr(candles, 14))
        atr_pct = atr14 / mark if mark > 0 else 0.0

        # Relative trend vs BTC
        merged = pd.merge(candles[["time", "close"]], btc_candles[["time", "close"]], on="time", suffixes=("_alt", "_btc"))
        if len(merged) < 30:
            continue
        ratio = merged["close_alt"].to_numpy() / merged["close_btc"].to_numpy()
        trend_rel30 = float(ratio[-1] / ratio[-30] - 1) if len(ratio) >= 30 else 0.0

        # Range-bound suitability:
        # 1. Centered in 60d range (0.35 - 0.65 is ideal)
        center_score = 1.0 - 2.0 * abs(range_pos - 0.5)  # 1.0 at midpoint, 0.0 at edges
        # 2. Low directional trend vs BTC
        trend_penalty = min(1.0, abs(trend_rel30) * 2.0)
        # 3. Healthy volatility (2% to 6% daily ATR)
        vol_score = 1.0 if 0.02 <= atr_pct <= 0.06 else max(0.0, 1.0 - abs(atr_pct - 0.04) * 25.0)

        spread_bps = mm_history.get(coin, 5.0)  # default estimate 5 bps

        score = center_score * 0.4 + (1.0 - trend_penalty) * 0.3 + vol_score * 0.3

        if range_pos < 0.20 or range_pos > 0.80:
            reason = "near edge of 60d range (breakout risk)"
        elif abs(trend_rel30) > 0.30:
            reason = "strong directional trend"
        else:
            reason = "range-bound, stable volatility"

        candidates.append(LadderCandidate(
            coin=coin,
            mark=mark,
            volume24h=item["volume_24h"],
            atr_pct=atr_pct,
            range_pos60=range_pos,
            trend_rel30=trend_rel30,
            historical_spread_bps=spread_bps,
            mm_score=score,
            reason=reason,
        ))

    candidates.sort(key=lambda c: c.mm_score, reverse=True)
    return candidates[:top]


def ladder_candidates_frame(candidates: list[LadderCandidate]) -> pd.DataFrame:
    rows = []
    for c in candidates:
        rows.append({
            "Coin": c.coin,
            "Mark": c.mark,
            "24h Volume": c.volume24h,
            "ATR (% of px)": c.atr_pct,
            "60d Range Pos": c.range_pos60,
            "30d Trend vs BTC": c.trend_rel30,
            "Hist Spread (bps)": c.historical_spread_bps,
            "MM Score": c.mm_score,
            "Assessment": c.reason,
        })
    return pd.DataFrame(rows)


def show_ladder_candidates(top: int = 10) -> None:
    """Display the Market Making Ladder Candidates table."""
    from desk.hl.account_analytics import display_interactive_table

    candidates = ladder_candidates(top=top)
    if not candidates:
        print("No market-making ladder candidates found.")
        return
    frame = ladder_candidates_frame(candidates)

    display_interactive_table(
        frame,
        title="MARKET-MAKING LADDER COIN SELECTION (LIQUID & RANGE-BOUND)",
        formatters={
            "Mark": lambda v: f"${v:,.2f}" if v == v else "",
            "24h Volume": lambda v: f"${v / 1e6:,.1f}M" if v == v else "",
            "ATR (% of px)": lambda v: f"{v:.1%}" if v == v else "",
            "60d Range Pos": lambda v: f"{v:.0%}" if v == v else "",
            "30d Trend vs BTC": lambda v: f"{v:+.1%}" if v == v else "",
            "Hist Spread (bps)": lambda v: f"{v:.1f}" if v == v else "",
            "MM Score": lambda v: f"{v:.2f}" if v == v else "",
        },
        key_columns={"MM Score": "max"},
    )
    print("Ladder candidates are filtered for high liquidity (> $15M 24h volume) and ranked by range-bound stability and spread capture.")


def save_candidates(path: Path = CANDIDATES_FILE) -> None:
    """Save screened candidates to JSON for dashboard and web sync."""
    candidates = screen_candidates()
    ladders = ladder_candidates()
    data = {
        "as_of": pd.Timestamp.now(tz="UTC").isoformat(),
        "candidates": [c.as_dict() for c in candidates],
        "ladder_candidates": [l.as_dict() for l in ladders],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=1))
    temp.replace(path)
