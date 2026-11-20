"""Signal tests of 2026-11 under the desk's rules: net of costs, t >= 2 in BOTH halves (same sign) or not established.

1. Crypto Fear & Greed (alternative.me, daily since 2018): what BTC and the alt basket do after extreme fear (<= 15, <= 25), on the
   first day out of extreme fear (crossing above 20), and after the first day into it, for longs (buy the fear) and for the desk's
   short side (does fear precede bounces that hurt the shorts), plus the desk's own closed shorts split by the index at entry.
2. BVIV (Hyperliquid mkts:BVIV, the Bitcoin implied-volatility perp): behaviour against BTC, extreme negative funding -> next 24/72 h
   (price and carry), liquidity.
3. The regime watchers (desk/market_regime.py): does alt_season / equity_selloff predict forward returns, verified independently.
4. 'Longs on extreme funding on red days' on the HL daily panel with the hourly funding history.
(The order-book signals are tested by `python -m desk.book_signals test` on the server's log; their result is in the write-up.)

Index value of day D is published for D at 00:00 UTC, so a trade enters at D's open: no look-ahead. Costs: 9 bps round trip (taker in
and out). Overlapping windows: events are thinned so their holding windows do not overlap (the first of any run enters, the next
event must be at least h days later). Halves: events before and after the sample's midpoint date.

Run: PYTHONPATH=. python research/signal_tests_2026_11.py [fng|bviv|regime|funding|all]
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research" / "data" / "signal_tests_2026_11.json"
COST = 0.0009   # 9 bps round trip
HORIZONS = (1, 3, 7, 14)


def tstat(x) -> float:
    x = pd.Series(x).dropna()
    return float(x.mean() / x.std(ddof=1) * math.sqrt(len(x))) if len(x) > 2 and x.std(ddof=1) > 0 else float("nan")


def thinned(days: pd.DatetimeIndex, gap: int) -> pd.DatetimeIndex:
    out, last = [], None
    for day in days:
        if last is None or (day - last).days >= gap:
            out.append(day)
            last = day
    return pd.DatetimeIndex(out)


def summarize(net: pd.Series, split: pd.Timestamp) -> dict:
    """One direction's net return per event with t overall and in each half; passes only if positive with t >= 2 in both halves."""
    first, second = net[net.index < split], net[net.index >= split]
    t, t1, t2 = tstat(net), tstat(first), tstat(second)
    return {"n": int(len(net)), "mean": float(net.mean()) if len(net) else float("nan"), "hit": float((net > 0).mean()) if len(net) else float("nan"),
            "t": t, "t1": t1, "t2": t2, "n1": int(len(first)), "n2": int(len(second)),
            "passes": bool(len(net) > 4 and net.mean() > 0 and t1 >= 2 and t2 >= 2)}


# ---------------------------------------------------------------------------------------------------------------- 1. fear and greed
def fng_index() -> pd.Series:
    data = requests.get("https://api.alternative.me/fng/?limit=0", timeout=60).json()["data"]
    series = pd.Series({pd.Timestamp(int(d["timestamp"]), unit="s", tz="UTC"): int(d["value"]) for d in data})
    return series.sort_index()


def fng_prices() -> pd.DataFrame:
    from desk.venues import binance_archive as ba
    from research.binance_rules_test import alt_basket

    btc = ba.klines("BTCUSDT", "1d", "2019-01-01")["open"]
    alts = alt_basket().resample("1D").first()
    alts.index = alts.index.normalize()
    return pd.DataFrame({"BTC": btc, "Alts": alts}).dropna()


def fear_greed() -> dict:
    fng = fng_index()
    prices = fng_prices()
    fng = fng.reindex(prices.index).dropna()
    split = prices.index[len(prices) // 2]
    flags = {
        "fear <= 15": fng <= 15,
        "fear <= 25": fng <= 25,
        "first day into fear (<= 20 after > 20)": (fng <= 20) & (fng.shift(1) > 20),
        "first day out of fear (> 20 after <= 20)": (fng > 20) & (fng.shift(1) <= 20),
    }
    rows = []
    for label, flag in flags.items():
        for h in HORIZONS:
            forward = prices.shift(-h) / prices - 1
            days = thinned(flag[flag].index, h)
            for asset in ("BTC", "Alts"):
                raw = forward[asset].reindex(days).dropna()
                for side, sign in (("long", 1), ("short", -1)):
                    net = sign * raw - COST
                    rows.append({"signal": label, "h": h, "asset": asset, "side": side, **summarize(net, split)})
    table = pd.DataFrame(rows)
    # the desk's own closed crypto shorts, split by the index on the day they were opened
    closed = pd.read_csv(ROOT / "data" / "closed_trades_crypto.csv", parse_dates=["time", "opened"])
    shorts = closed[closed["side"].astype(str).str.upper() == "SHORT"].copy()
    shorts["opened_day"] = pd.to_datetime(shorts["opened"], utc=True).dt.normalize()
    shorts["fng"] = shorts["opened_day"].map(fng)
    shorts["ret"] = shorts["pnl"] / shorts["notional"].abs()
    shorts = shorts.dropna(subset=["fng", "ret"])
    # one row per position (a position closes in several fills): value-weighted by notional per coin and open day
    book = shorts.groupby(["coin", "opened_day"]).apply(lambda g: pd.Series({"ret": g["pnl"].sum() / g["notional"].abs().sum(), "fng": g["fng"].iloc[0]}), include_groups=False)
    fear_group, calm_group = book[book["fng"] <= 20]["ret"], book[book["fng"] > 20]["ret"]
    diff = fear_group.mean() - calm_group.mean() if len(fear_group) and len(calm_group) else float("nan")
    se = math.sqrt(fear_group.var(ddof=1) / len(fear_group) + calm_group.var(ddof=1) / len(calm_group)) if len(fear_group) > 2 and len(calm_group) > 2 else float("nan")
    desk = {"n_fear": int(len(fear_group)), "n_calm": int(len(calm_group)), "fear_mean": float(fear_group.mean()) if len(fear_group) else float("nan"),
            "calm_mean": float(calm_group.mean()) if len(calm_group) else float("nan"), "diff": float(diff), "t": float(diff / se) if se and not math.isnan(se) else float("nan")}
    return {"index_days": int(len(fng)), "from": str(fng.index.min().date()), "to": str(fng.index.max().date()), "split": str(split.date()),
            "now": int(fng.iloc[-1]), "days_le_15": int((fng <= 15).sum()), "days_le_25": int((fng <= 25).sum()), "table": table, "desk_shorts": desk}


# ---------------------------------------------------------------------------------------------------------------- 2. BVIV
def bviv() -> dict:
    import time

    from desk.hl.trade_analytics import post_info

    coin, end = "mkts:BVIV", int(time.time() * 1000)

    def candles(interval: str, days: int) -> pd.DataFrame:
        rows = post_info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": end - days * 86400000, "endTime": end}})
        frame = pd.DataFrame(rows)
        frame["t"] = pd.to_datetime(frame["t"], unit="ms", utc=True)
        for column in ("o", "h", "l", "c", "v"):
            frame[column] = frame[column].astype(float)
        return frame.set_index("t")

    hourly, daily = candles("1h", 120), candles("1d", 400)
    funding, start = [], end - 200 * 86400000
    while True:
        part = post_info({"type": "fundingHistory", "coin": coin, "startTime": start})
        if not part:
            break
        funding += part
        if len(part) < 500:
            break
        start = int(part[-1]["time"]) + 1
    f = pd.DataFrame(funding)
    f["t"] = pd.to_datetime(f["time"], unit="ms", utc=True)
    rate = f.set_index("t")["fundingRate"].astype(float)          # per hour, as a fraction
    btc_rows = post_info({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1h", "startTime": int(hourly.index.min().timestamp() * 1000), "endTime": end}})
    btc = pd.DataFrame(btc_rows)
    btc["t"] = pd.to_datetime(btc["t"], unit="ms", utc=True)
    btc = btc.set_index("t")["c"].astype(float)
    both = pd.concat([np.log(hourly["c"]).diff(), np.log(btc).diff()], axis=1, keys=["v", "b"]).dropna()
    beta = lambda r: float(np.cov(r.v, r.b)[0, 1] / r.b.var())   # noqa: E731
    daily_both = both.resample("1D").sum()
    px = hourly["c"]
    hourly_rate = rate.reindex(px.index, method="ffill")
    frame = pd.DataFrame({"px": px, "rate": hourly_rate})
    out = {"listed": str(hourly.index.min().date()), "days": int(len(daily)), "beta_hourly": beta(both), "beta_daily": beta(daily_both),
           "corr_daily": float(daily_both.v.corr(daily_both.b)), "funding_mean_bps_h": float(rate.mean() * 1e4), "funding_hours_below_5bps": int((rate < -5e-4).sum()),
           "funding_hours": int(len(rate)), "events": []}
    for hours in (24, 72):
        frame[f"fwd{hours}"] = frame.px.shift(-hours) / frame.px - 1
        frame[f"carry{hours}"] = (-frame.rate).rolling(hours).sum().shift(-hours)
        events = frame[frame.rate < -5e-4].dropna()
        picked, last = [], None
        for t in events.index:
            if last is None or (t - last).total_seconds() >= hours * 3600:
                picked.append(t)
                last = t
        e = events.loc[picked]
        mid = e.index[len(e) // 2] if len(e) else None
        for name, part in (("all", e), ("first half", e[e.index < mid] if mid is not None else e), ("second half", e[e.index >= mid] if mid is not None else e)):
            out["events"].append({"hours": hours, "part": name, "n": int(len(part)), "price": float(part[f"fwd{hours}"].mean()) if len(part) else float("nan"),
                                  "price_t": tstat(part[f"fwd{hours}"]), "carry": float(part[f"carry{hours}"].mean()) if len(part) else float("nan"),
                                  "carry_t": tstat(part[f"carry{hours}"]), "all_hours_price": float(frame[f"fwd{hours}"].mean())})
    l2 = post_info({"type": "l2Book", "coin": coin})
    bids, asks = l2["levels"]
    bb, aa = float(bids[0]["px"]), float(asks[0]["px"])
    daily_volume = (hourly["v"] * hourly["c"]).groupby(hourly.index.normalize()).sum()
    meta, ctx = post_info({"type": "metaAndAssetCtxs", "dex": "mkts"})
    live = next((ctx[k] for k, a in enumerate(meta["universe"]) if a["name"] == coin), {})
    out["liquidity"] = {"spread_bps": 1e4 * (aa / bb - 1), "bid_depth_1pct": sum(float(x["px"]) * float(x["sz"]) for x in bids if float(x["px"]) > bb * 0.99),
                        "ask_depth_1pct": sum(float(x["px"]) * float(x["sz"]) for x in asks if float(x["px"]) < aa * 1.01),
                        "median_daily_volume": float(daily_volume.median()), "days_over_100k": int((daily_volume > 1e5).sum()),
                        "open_interest": float(live.get("openInterest", 0)) * float(live.get("markPx", 0)), "mark": float(live.get("markPx", 0))}
    return out


# ---------------------------------------------------------------------------------------------------------------- 3. regime watchers
def regimes() -> dict:
    from desk import market_regime as mr

    alts = mr.hl_closes(mr.alt_universe())
    btc = mr.hl_closes(["BTC"])["BTC"]
    alt = mr.alt_signals(alts, btc).dropna(subset=["rel7"])
    alt_level = mr.levels(alt, ["A1", "A2", "A3", "A4"], confirm=3, window=5)
    basket = (1 + alts.pct_change(fill_method=None).clip(-0.5, 1.0).mean(axis=1, skipna=True).fillna(0)).cumprod()
    ratio = (basket / btc.reindex(basket.index).ffill())
    spy, vix = mr.yahoo_series("SPY"), mr.yahoo_series("^VIX")
    eq = mr.equity_signals(spy, vix).dropna(subset=["drawdown60"])
    eq_level = mr.levels(eq, ["E1", "E2", "E3", "E4"], confirm=2, window=3)
    rows = []
    for name, level, asset_name, series, horizons in (("alt season", alt_level, "alt basket", basket, (7, 14)), ("alt season", alt_level, "alts vs BTC", ratio, (7, 14)),
                                                      ("equity sell-off", eq_level, "SPY (sessions)", spy, (5, 20)),
                                                      ("equity sell-off", eq_level, "alt basket", basket, (7, 14)), ("equity sell-off", eq_level, "BTC", btc, (7, 14))):
        level = level.copy()
        level.index = pd.to_datetime(level.index).tz_localize(None) if getattr(level.index, "tz", None) else pd.to_datetime(level.index)
        s = series.copy()
        s.index = pd.to_datetime(s.index).tz_localize(None) if getattr(s.index, "tz", None) else pd.to_datetime(s.index)
        split = level.index[len(level) // 2]
        for h in horizons:
            forward = s.shift(-h) / s - 1
            base = forward.reindex(level.index)
            for state in ("notice+high", "high"):
                flag = level.isin(["notice", "high"]) if state == "notice+high" else level == "high"
                days = thinned(flag[flag].index, h)
                events = base.reindex(days).dropna()
                excess = events - base.dropna().groupby(base.dropna().index < split).transform("mean").reindex(events.index)   # vs the average day of the same half
                first, second = excess[excess.index < split], excess[excess.index >= split]
                rows.append({"regime": name, "state": state, "asset": asset_name, "h": h, "n": int(len(events)), "mean": float(events.mean()), "excess": float(excess.mean()),
                             "t": tstat(excess), "t1": tstat(first), "t2": tstat(second),
                             "same_sign_both_halves": bool(abs(tstat(first)) >= 2 and abs(tstat(second)) >= 2 and np.sign(tstat(first)) == np.sign(tstat(second)))})
    return {"table": pd.DataFrame(rows), "alt_days": int(len(alt_level)), "eq_days": int(len(eq_level)),
            "alt_high_days": int((alt_level == "high").sum()), "alt_notice_days": int((alt_level == "notice").sum()),
            "eq_high_days": int((eq_level == "high").sum()), "eq_notice_days": int((eq_level == "notice").sum())}


# ---------------------------------------------------------------------------------------------------------------- 4. funding-extreme longs on red days
def funding_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily open/close per coin from the research panel and the daily mean hourly funding rate per coin from the funding cache."""
    cache = Path.home() / "Library" / "Caches" / "trading-desk" / "api_cache"
    panel = pd.read_csv(cache / "signal_panel.csv.gz", parse_dates=["date"])
    panel = panel[~panel["coin"].str.contains(":")]
    opens = panel.pivot_table(index="date", columns="coin", values="open", aggfunc="last").sort_index()
    closes = panel.pivot_table(index="date", columns="coin", values="close", aggfunc="last").sort_index()
    funding = {}
    for path in cache.glob("funding_main_*.json"):
        coin = path.stem.replace("funding_main_", "")
        rows = json.loads(path.read_text()).get("rows", [])
        if len(rows) < 24 * 120:
            continue
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["time"], unit="ms").dt.normalize()
        funding[coin] = frame.groupby("date")["fundingRate"].sum().astype(float)   # the day's funding (sum of 24 hourly rates)
    funding = pd.DataFrame(funding).sort_index()
    common = [c for c in funding.columns if c in opens.columns]
    return opens[common], closes[common], funding[common]


def funding_longs() -> dict:
    opens, closes, funding = funding_panel()
    dates = opens.index.intersection(funding.index)
    opens, closes, funding = opens.loc[dates], closes.loc[dates], funding.loc[dates]
    day_ret = closes / opens - 1
    split = dates[len(dates) // 2]
    rows = []
    for funding_cut in (-0.0005, -0.001):            # daily funding (sum of hourly) at or below -0.05%, -0.10% a day (-18%, -37% a year)
        for red_cut in (-0.05,):
            signal = (funding <= funding_cut) & (day_ret <= red_cut)
            for h in (1, 3, 7):
                entry = opens.shift(-1)                                    # next day's open: the signal is known at the close
                exit_ = opens.shift(-1 - h)
                price = exit_ / entry - 1
                carry = -funding.shift(-1).rolling(h).sum().shift(-(h - 1))   # a long receives negative funding over the holding days
                net_all = price + carry - COST
                events = []
                for coin in signal.columns:
                    days = signal[coin][signal[coin]].index
                    for day in thinned(pd.DatetimeIndex(days), h):
                        v = net_all.at[day, coin] if day in net_all.index else np.nan
                        if pd.notna(v):
                            events.append((day, coin, float(v), float(price.at[day, coin]), float(carry.at[day, coin])))
                if not events:
                    continue
                ev = pd.DataFrame(events, columns=["day", "coin", "net", "price", "carry"]).set_index("day").sort_index()
                base = (price + carry - COST).stack().dropna()
                base_mean = float(base.mean())
                s = summarize(ev["net"], ev.index[len(ev) // 2])   # halves by the median event date: the funding history is thin early on
                rows.append({"funding_cut_per_day": funding_cut, "red_day": red_cut, "h": h, **s, "mean_price": float(ev["price"].mean()), "mean_carry": float(ev["carry"].mean()),
                             "all_days_net": base_mean, "coins": int(ev["coin"].nunique())})
    return {"table": pd.DataFrame(rows), "coins": int(opens.shape[1]), "days": int(len(dates)), "from": str(dates.min().date()), "to": str(dates.max().date()), "split": str(split.date())}


def main(argv: list[str] | None = None) -> int:
    which = (argv or sys.argv[1:] or ["all"])[0]
    pd.set_option("display.width", 220, "display.max_columns", 40)
    results = {}
    if which in ("fng", "all"):
        results["fng"] = fear_greed()
        r = results["fng"]
        print(f"F&G {r['from']} to {r['to']} ({r['index_days']} days, now {r['now']}); days <= 15: {r['days_le_15']}, <= 25: {r['days_le_25']}; halves split {r['split']}")
        print(r["table"].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
        print("desk shorts by index at entry:", r["desk_shorts"])
    if which in ("bviv", "all"):
        results["bviv"] = bviv()
        print(json.dumps(results["bviv"], indent=1, default=float))
    if which in ("regime", "all"):
        results["regime"] = regimes()
        print({k: v for k, v in results["regime"].items() if k != "table"})
        print(results["regime"]["table"].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    if which in ("funding", "all"):
        results["funding"] = funding_longs()
        print({k: v for k, v in results["funding"].items() if k != "table"})
        print(results["funding"]["table"].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(OUT.read_text()) if OUT.exists() else {}
    for key, value in results.items():
        existing[key] = json.loads(json.dumps(value, default=lambda o: o.to_dict("records") if isinstance(o, pd.DataFrame) else float(o)))
    OUT.write_text(json.dumps(existing, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
