"""Candidate screener: which HL perps to trade next, ranked, with what the desk would size.

Two lists, from the saved daily candles (desk/hl/signal_research.py) and one live market snapshot:

* Short candidates, among the 40 most liquid main-DEX perps (BTC, ETH and stablecoins left out).
  The score uses only the signals the research harness validated, each as a cross-sectional
  z-score in the direction that held up out of sample. With none validated it falls back to the
  desk's own record (shorting alts already weak against BTC over 30 days won 93% of the time in
  its history), and says so.
* Market-making candidates: liquid coins that have been going nowhere, so a ladder's quotes on
  both sides keep filling. Ranked by liquidity and by a low efficiency ratio over 30 days (net move
  divided by the sum of daily moves: 1 is a straight line, near 0 is chop).

Each short candidate also shows how crowded the short is: the 7-day average funding from the short's side
(HL's base rate, about +11% APR, means longs and shorts are balanced; negative means shorts pay), the share of
the last 7 days' hours in which shorts paid, and open interest over 24-hour volume. A short where shorts paid in
most hours is flagged in Notes; the flag does not move the score, which only validated signals do.

Each candidate shows the executor's stop (3 x ATR), the size the 5% budget would buy at that stop,
what the alt-rally stress scenario would read with it added (desk/stress.py), and the warnings
from the desk's history (desk/trade_rules.py). Nothing here trades: pick one and run it as usual.

    python -m desk.screener          # rank now and save data/screener.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from desk.hl import signal_research as research
from desk.numbers import usd as _usd, pct as _pct

ROOT = Path(__file__).resolve().parents[1]
SCREENER_FILE = ROOT / "data" / "screener.json"
TOP_N = 10
MM_TOP_N = 8
MM_MAX_EFFICIENCY = 0.5                  # a coin moving more than half in a straight line is trending: no ladder
RISK_BUDGET_PCT = 0.80 * 0.05            # 5% of the 80% usable equity (desk/risk.py)
# Scores point the way a signal's long side buys, so shorts are the lowest scores. The fallback, the desk's record
# (short alts already weak against BTC over 30 days), ranks by that return: the weakest score lowest.
FALLBACK = {"rel30": 1.0}
CROWDED_LONG_APR = 0.365                 # longs paying 0.1% a day on average over 7 days: a crowded long (desk/funding_watch.py)
CROWDED_SHARE = 0.5                      # shorts paid in more than half the hours of the last 7 days: crowded
FUNDING_DAYS = 7
# Research columns only: the stop, the size and the margin are the executor's and the daemon's.
CANDIDATE_COLUMNS = ["Coin", "Score", "Basis", "vs BTC 30d", "vs BTC 7d", "From high 60d", "Funding 7d (APR)",
                     "OI / volume", "Volume 24h", "Notes"]
CANDIDATE_FORMATS = {"Score": "num2", "vs BTC 30d": "spct", "vs BTC 7d": "spct", "From high 60d": "spct",
                     "Funding 7d (APR)": "spct", "OI / volume": "num2", "Volume 24h": "usd"}
# Funding for a ladder: the 7-day time-weighted average from the long side (positive: longs pay). The ladder's
# position swings both ways, so what it pays is that average times its lean; a crowded side is flagged in Notes.
MM_COLUMNS = ["Coin", "Efficiency 30d", "Move 30d", "Range position 60d", "Daily range 30d", "Volume 24h", "Longs pay 7d (APR)",
              "Own ladder edge per $1k", "Notes"]
MM_FORMATS = {"Efficiency 30d": "num2", "Move 30d": "spct", "Range position 60d": "pct", "Daily range 30d": "pct",
              "Volume 24h": "usd", "Longs pay 7d (APR)": "spct", "Own ladder edge per $1k": "susd"}
MM_EDGE_ZONE = (0.2, 0.8)                # outside this part of the 60-day range a breakout is likelier: flagged
MAX_LEVERAGE = 4                         # the executor's cap on main-DEX perps


def live_markets() -> pd.DataFrame:
    """Main-DEX perps now: mark, 24h notional volume, hourly funding, open interest in $ (delisted left out)."""
    from desk.hl.trade_analytics import post_info

    meta, contexts = post_info({"type": "metaAndAssetCtxs"})
    rows = []
    for asset, context in zip(meta["universe"], contexts):
        if asset.get("isDelisted"):
            continue
        rows.append({"coin": asset["name"], "mark": float(context.get("markPx") or 0),
                     "volume24h": float(context.get("dayNtlVlm") or 0), "funding_hour": float(context.get("funding") or 0),
                     "open_interest": float(context.get("openInterest") or 0) * float(context.get("markPx") or 0),
                     "max_leverage": int(asset.get("maxLeverage") or 1)})
    return pd.DataFrame(rows).set_index("coin")


def recent_funding(coin: str, days: int = FUNDING_DAYS) -> list[float]:
    """The coin's hourly funding rates (paid by longs) over the last ``days`` days; empty when HL does not answer."""
    from desk.hl.trade_analytics import post_info

    try:
        rows = post_info({"type": "fundingHistory", "coin": coin, "startTime": int((time.time() - days * 86400) * 1000)})
        return [float(row["fundingRate"]) for row in rows or []]
    except Exception:
        return []


def crowding(rates: list[float]) -> tuple[float, float]:
    """A short's view of hourly funding rates: the average as an APR the short earns (negative: it pays), and the
    share of hours in which shorts paid."""
    if not rates:
        return float("nan"), float("nan")
    return float(np.mean(rates)) * 8760, float(np.mean([rate < 0 for rate in rates]))   # a positive rate: longs pay shorts


def latest_signals(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Every signal at the last saved close, one row per coin, plus ATR, efficiency and daily range."""
    close, high, low = data["close"], data["high"], data["low"]
    signals = research.signal_frames(data)
    frame = pd.DataFrame({name: values.iloc[-1] for name, values in signals.items()})
    frame["atr_pct"] = (research.atr(high, low, close) / close).iloc[-1]
    moves = close.diff().abs().rolling(30, min_periods=20).sum()
    frame["efficiency"] = ((close - close.shift(30)).abs() / moves).iloc[-1]
    frame["move30"] = close.pct_change(30, fill_method=None).iloc[-1]
    frame["range_pct"] = ((high - low) / close).rolling(30, min_periods=20).mean().iloc[-1]
    low60, high60 = low.rolling(60, min_periods=30).min(), high.rolling(60, min_periods=30).max()
    frame["range_position"] = ((close - low60) / (high60 - low60)).iloc[-1]
    return frame


def scoring(results: dict | None) -> tuple[dict[str, float], str]:
    """Signal -> weight (its validated direction), and the basis to show; the desk's record if nothing validated."""
    validated = {name: item["direction"] for name, item in ((results or {}).get("signals") or {}).items() if item.get("validated")}
    if validated:
        return validated, "validated: " + ", ".join(research.SIGNALS[name] for name in validated)  # names the signals
    return dict(FALLBACK), "desk record (no signal validated)"


def zscores(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    return (frame[columns] - frame[columns].mean()) / frame[columns].std(ddof=0).replace(0, np.nan)


def _stress_note(note: str) -> str:
    """stress.fit_entry's note in a few words, for an entry the size of the risk budget at the 3 x ATR stop."""
    if note.startswith("entry refused"):
        return "refused: stress over limit"
    if note.startswith("entry cut to"):
        return note.split(":")[0].removeprefix("entry ") + ": stress over limit"   # "cut to 40%: stress over limit"
    return "stress warning"


def short_candidates(signals: pd.DataFrame, live: pd.DataFrame, tradable: list[str], weights: dict[str, float],
                     basis: str, desk_equity: float, exposures: list, held: set[str], top: int = TOP_N,
                     free_margin: dict[str, float] | None = None, funding_history=recent_funding) -> pd.DataFrame:
    """The ``top`` short candidates: lowest score first (a high score is what the long side of a signal buys), each with
    how crowded the short is (``funding_history``: coin -> its last 7 days of hourly funding rates)."""
    from desk import stress, trade_rules

    # A coin already held is a candidate that became a trade: it leaves the list and the next one moves up.
    names = [coin for coin in tradable if coin in live.index and coin in signals.index and coin not in held]
    frame = signals.loc[names].copy()
    usable = [name for name in weights if name in frame.columns]
    if not usable or frame.empty:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS)
    z = zscores(frame, usable)
    frame["score"] = sum(weights[name] * z[name] for name in usable) / len(usable)
    frame = frame.dropna(subset=["score"]).sort_values("score").head(top)
    budget = RISK_BUDGET_PCT * desk_equity
    rows = []
    for coin, row in frame.iterrows():
        stop = 3.0 * row["atr_pct"]
        size = budget / stop if stop > 0 else float("nan")
        _, note = stress.fit_entry(exposures, desk_equity, "crypto perp", coin, -size) if desk_equity > 0 and size == size else (1.0, "")
        apr, paid = crowding(funding_history(coin))
        crowded = ["crowded"] if paid == paid and paid > CROWDED_SHARE else []   # shorts paid in most hours this week
        notes = crowded + trade_rules.entry_notes(coin, "SHORT", size) + ([_stress_note(note)] if note else [])
        volume = live.loc[coin, "volume24h"]
        open_interest = live.loc[coin, "open_interest"] if "open_interest" in live.columns else float("nan")
        rows.append({"Coin": coin, "Score": row["score"], "Basis": basis, "vs BTC 30d": row.get("rel30"), "vs BTC 7d": row.get("rel7"),
                     "From high 60d": row.get("from_high60"), "Funding 7d (APR)": apr,
                     "OI / volume": open_interest / volume if volume else float("nan"), "Volume 24h": volume,
                     "Notes": "; ".join(notes)})
    return pd.DataFrame(rows, columns=CANDIDATE_COLUMNS)


def market_making_candidates(signals: pd.DataFrame, live: pd.DataFrame, tradable: list[str], laddered: set[str],
                             top: int = MM_TOP_N, edges: dict[str, float] | None = None,
                             funding_history=recent_funding) -> pd.DataFrame:
    """Liquid coins that have gone nowhere: low efficiency ratio, ranked half by it and half by liquidity.
    ``laddered``: coins market-made already (a ladder running, or one in the last 30 days); they leave the list."""
    names = [coin for coin in tradable if coin in live.index and coin in signals.index and coin not in laddered]
    columns = ["efficiency", "move30", "range_pct"] + (["range_position"] if "range_position" in signals.columns else [])
    frame = signals.loc[names, columns].dropna(subset=["efficiency", "move30", "range_pct"]).copy()
    frame = frame[frame["efficiency"] <= MM_MAX_EFFICIENCY]
    if frame.empty:
        return pd.DataFrame(columns=MM_COLUMNS)
    frame["volume24h"] = live.loc[frame.index, "volume24h"]
    frame["rank"] = frame["efficiency"].rank() + frame["volume24h"].rank(ascending=False)
    frame = frame.sort_values("rank").head(top)
    rows = []
    for coin, row in frame.iterrows():
        position = row.get("range_position", float("nan"))
        rates = funding_history(coin)
        longs_pay = float(np.mean(rates)) * 8760 if rates else float("nan")
        shorts_paid = float(np.mean([rate < 0 for rate in rates])) if rates else float("nan")
        crowd = (["crowded short"] if shorts_paid == shorts_paid and shorts_paid > CROWDED_SHARE else
                 ["crowded long"] if longs_pay == longs_pay and longs_pay >= CROWDED_LONG_APR else [])
        notes = crowd + (
            ["breakout risk: near the edge of its 60-day range"] if position == position and not MM_EDGE_ZONE[0] <= position <= MM_EDGE_ZONE[1] else [])
        rows.append({"Coin": coin, "Efficiency 30d": row["efficiency"], "Move 30d": row["move30"], "Range position 60d": position,
                     "Daily range 30d": row["range_pct"], "Volume 24h": row["volume24h"], "Longs pay 7d (APR)": longs_pay,
                     "Own ladder edge per $1k": (edges or {}).get(coin, float("nan")), "Notes": "; ".join(notes)})
    return pd.DataFrame(rows, columns=MM_COLUMNS)


def ladder_edges() -> dict[str, float]:
    """Each coin's own ladder edge where a ladder has traded it: spread captured less fees per $1,000 (desk/market_making.py)."""
    try:
        from desk.market_making import market_making_frame

        frame = market_making_frame()
    except Exception:
        return {}
    return {row["Coin"]: row["Captured per $1k"] for _, row in frame.iterrows() if row.get("Volume") and row["Captured per $1k"] == row["Captured per $1k"]}


def run(desk_equity: float | None = None, exposures: list | None = None, held: set[str] | None = None,
        laddered: set[str] | None = None, free_margin: dict[str, float] | None = None) -> dict:
    """Rank now from the saved candles; save and return the screener state."""
    panel = research.load_panel()
    if "BTC" not in panel:
        raise RuntimeError("no saved candles: run python -m desk.hl.signal_research first")
    data = research.frames(panel)
    tradable_frame = research.universe(data["close"], data["dollar_volume"])
    tradable = [coin for coin, flag in tradable_frame.iloc[-1].items() if flag]
    live = live_markets()
    signals = latest_signals(data)
    weights, basis = scoring(research.load_results())
    if desk_equity is None or exposures is None or held is None:
        desk_equity, exposures, held = desk_context()
    laddered = laddered if laddered is not None else {item["coin"] for item in _ladders()}
    # A candidate that became a trade is not a candidate any more: held now, traded in the last 30 days (the
    # journal), or market-made (a ladder running or recent). It leaves both lists; the next one moves up.
    from desk import journal

    used = set(held) | set(laddered) | journal.recently_traded()
    shorts = short_candidates(signals, live, tradable, weights, basis, desk_equity, exposures, used, free_margin=free_margin)
    mm = market_making_candidates(signals, live, tradable, used, edges=ladder_edges())
    state = {"as_of": pd.Timestamp.now(tz="UTC").isoformat(), "candles_to": f"{data['close'].index[-1]:%Y-%m-%d}",
             "basis": basis, "universe": len(tradable), "desk_equity": desk_equity,
             "shorts": json.loads(shorts.to_json(orient="records")), "market_making": json.loads(mm.to_json(orient="records"))}
    SCREENER_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCREENER_FILE.write_text(json.dumps(state, indent=1))
    return state


def desk_context() -> tuple[float, list, set[str]]:
    """Desk equity, every position's exposure (desk/stress.py) and the coins held, from a fresh desk read."""
    from desk import stress
    from desk.accounts import load_accounts
    from desk.overview import DeskOverview

    overview = DeskOverview(load_accounts())
    overview.fetch_data(include_risk=False, record_ib_history=False)
    exposures = [row for snapshot in overview.snapshots for row in stress.position_exposures(snapshot)]
    held = {position.symbol for snapshot in overview.snapshots if snapshot.venue == "hl" for position in snapshot.positions}
    return overview.desk_equity, exposures, held


def _ladders() -> list[dict]:
    from desk.market_making import running_ladders

    return running_ladders()


def load(path: Path = SCREENER_FILE) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _formatters() -> dict:
    percent = lambda value: "" if value is None or value != value else f"{_pct(value)}"
    from desk.numbers import usd

    money = lambda value: usd(value, missing="")
    number = lambda value: "" if value is None or value != value else f"{value:.2f}"
    return {"Score": number, "vs BTC 30d": percent, "vs BTC 7d": percent, "From high 60d": percent, "Funding 7d (APR)": percent,
            "OI / volume": number,
            "Longs pay 7d (APR)": percent,
            "Volume 24h": money,
            "Efficiency 30d": number, "Move 30d": percent, "Range position 60d": percent, "Daily range 30d": percent,
            "Own ladder edge per $1k": lambda value: "" if value is None or value != value else f"{_usd(value)}"}


def show_candidates() -> None:
    """Short candidates for the notebooks (from the last saved run; the trading daemon re-ranks hourly)."""
    from desk.hl.account_analytics import display_interactive_table

    state = load()
    if not state:
        print("No screener run yet: python -m desk.screener")
        return
    formats = _formatters()
    shorts = pd.DataFrame(state["shorts"], columns=CANDIDATE_COLUMNS)
    if len(shorts) > 1 and shorts["Basis"].nunique() == 1:  # one basis for every row: said once below, as on the dashboard
        shorts = shorts.drop(columns="Basis")
    display_interactive_table(shorts, title="SHORT CANDIDATES",
                              formatters={key: formats[key] for key in CANDIDATE_COLUMNS if key in formats}, key_columns={"Score": "min"})
    print(f"As of {state['as_of'][:16].replace('T', ' ')} UTC, candles to {state['candles_to']}; {state['universe']} coins in the "
          f"liquid universe. Score basis: {state['basis']}. Funding 7d is the short's side (about +11% APR is HL's "
          "balanced base rate); crowded: shorts paid in more than half of the last 7 days' hours.")


def show_ladder_candidates() -> None:
    """Market-making candidates for the notebooks."""
    from desk.hl.account_analytics import display_interactive_table

    state = load()
    if not state:
        print("No screener run yet: python -m desk.screener")
        return
    formats = _formatters()
    display_interactive_table(pd.DataFrame(state["market_making"], columns=MM_COLUMNS), title="MARKET-MAKING CANDIDATES",
                              formatters={key: formats[key] for key in MM_COLUMNS if key in formats}, key_columns={"Efficiency 30d": "min"})
    print("Efficiency: the 30-day net move over the sum of daily moves (near 0 is chop, good for a ladder). Own ladder edge: "
          "spread captured less fees per $1,000 where a ladder has traded the coin.")


def show_screener() -> None:
    """Both candidate lists."""
    show_candidates()
    show_ladder_candidates()


def main(argv: list[str] | None = None) -> int:
    from desk.privacy import mask_output

    mask_output()
    state = run()
    pd.set_option("display.width", 240)
    print(f"basis: {state['basis']}; universe {state['universe']}; candles to {state['candles_to']}")
    print(pd.DataFrame(state["shorts"]).drop(columns=["Basis"]).to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print(pd.DataFrame(state["market_making"]).to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
