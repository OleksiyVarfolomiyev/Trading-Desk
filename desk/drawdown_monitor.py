"""A statistical drawdown monitor for the crypto book: when is a drawdown too deep for the models to be right?

This is not the investor's tolerance (desk/drawdown.py, desk/fiduciary.json: the desk's drawdown the investor accepts).
It is a model-health signal for the crypto book only: if the models are right (a modest positive edge, today's risk), a
drawdown deeper than X should be rare, so crossing it is a reason to re-evaluate the models. Alert only; nothing trades.

How the levels are found (``fit``; the write-up is research/crypto_drawdown_threshold.md):

* The crypto book has about 34 managed days, far too few to measure its own drawdown distribution, so the shape comes
  from a PROXY: today's crypto positions (signed shares of the HL wallets' equity) replayed over every day of the HL
  daily-candle panel (``signal_panel``, ~526 days). That says how a book with today's exposure would have moved. It
  leaves out funding, fees and trading in and out, and today's short book was chosen with hindsight, so its replayed
  drift is not used: drift is assumed (an annualised Sharpe, default 1) and only the volatility, tails and serial
  dependence come from the replay.
* Max drawdown over 30, 60 and 90 days is simulated two ways: a stationary block bootstrap of the replayed returns
  (block length from their autocorrelation) and a Student-t model scaled to the current EWMA volatility. Percentile
  confidence intervals come from resampling the replay itself.
* The two levels are percentiles of the max drawdown over one quarter: ``watch`` is crossed in about a third of the
  quarters of a book whose model is right, ``re-evaluate`` in about one in twelve. Their power against a degraded model
  (no edge, 50% more volatility, a negative edge) is measured too.
* Levels are stored in units of the book's own volatility over a quarter (sigma * sqrt(63)), so they scale with the
  current exposure: ``levels_now`` recomputes sigma from today's weights each run.

    python -m desk.drawdown_monitor fit       refit the multiples and write desk/crypto_drawdown_threshold.json
    python -m desk.drawdown_monitor status    today's crypto drawdown against the two levels
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
FIT_FILE = Path(__file__).with_name("crypto_drawdown_threshold.json")
ALERT_STATE = REPO / "data" / "crypto_drawdown_alert.json"
PANEL = Path.home() / "Library" / "Caches" / "trading-desk" / "api_cache" / "signal_panel.csv.gz"
QUARTER = 63                      # trading-style days in a quarter: the horizon the false-alarm rate is stated for
HORIZONS = (30, 60, 90)
PERCENTILES = (50, 75, 90, 95, 99)
FALSE_ALARM = {"watch": 0.35, "re-evaluate": 0.08}    # share of quarters crossed by a book whose model is right
ASSUMED_SHARPE = 1.0              # the edge assumed when the model is right (annualised); a default for the user to confirm
DAYS = 365                        # crypto trades every day
MIN_HISTORY = 90                  # a coin needs this many days in the panel to be replayed as itself


# ------------------------------------------------------------------------------------------------ the proxy returns
def panel_returns(path: Path | None = None) -> pd.DataFrame:
    """Daily close-to-close returns of every coin in the HL candle panel (rows: days, columns: coins)."""
    frame = pd.read_csv(path or PANEL, usecols=["date", "coin", "close"])
    closes = frame.pivot_table(index="date", columns="coin", values="close", aggfunc="last").sort_index()
    closes.index = pd.to_datetime(closes.index)
    return closes.pct_change(fill_method=None)


def crypto_weights() -> dict[str, float]:
    """Today's crypto positions as signed shares of the HL wallets' equity (main dex; equity perps count as stocks)."""
    from desk.accounts import accounts_for, load_accounts
    from desk.hl.trade_analytics import post_info

    value = {}
    equity = 0.0
    for account in accounts_for("hl", load_accounts()):
        state = post_info({"type": "clearinghouseState", "user": account.id}) or {}
        equity += float((state.get("marginSummary") or {}).get("accountValue") or 0)
        spot = post_info({"type": "spotClearinghouseState", "user": account.id}) or {}   # the collateral of a unified account
        equity += sum(float(b.get("total") or 0) for b in spot.get("balances", []) if b.get("coin") == "USDC")
        for item in state.get("assetPositions", []):
            position = item.get("position", {})
            coin = str(position.get("coin", ""))
            if coin and ":" not in coin:
                size = float(position.get("szi") or 0)
                value[coin] = value.get(coin, 0.0) + math.copysign(abs(float(position.get("positionValue") or 0)), size)
    return {coin: v / equity for coin, v in value.items()} if equity > 0 else {}


def replay_returns(weights: dict[str, float], returns: pd.DataFrame) -> tuple[pd.Series, dict]:
    """The daily return of a book holding ``weights`` (signed shares of equity) on every panel day. A coin with under
    ``MIN_HISTORY`` days is replayed by the median coin's return (the alt basket) before it existed."""
    basket = returns.median(axis=1)
    pieces = {}
    stand_in = []
    for coin, weight in weights.items():
        series = returns[coin] if coin in returns else pd.Series(np.nan, index=returns.index)
        if series.notna().sum() < MIN_HISTORY:
            stand_in.append(coin)
            series = basket
        else:
            series = series.fillna(basket)
        pieces[coin] = weight * series
    total = pd.DataFrame(pieces).sum(axis=1, min_count=1).dropna()
    return total, {"stand_in": stand_in, "net": float(sum(weights.values())), "gross": float(sum(abs(w) for w in weights.values()))}


# ------------------------------------------------------------------------------------------------ the simulations
def max_drawdown(paths: np.ndarray) -> np.ndarray:
    """Max drawdown (positive share) of each path of daily returns, starting at a high (growth of 1)."""
    levels = np.cumprod(1 + paths, axis=1)
    peak = np.maximum.accumulate(np.concatenate([np.ones((len(levels), 1)), levels], axis=1), axis=1)[:, 1:]
    return (1 - levels / peak).max(axis=1)


def block_length(returns: np.ndarray, low: int = 3, high: int = 30) -> int:
    """The block length: the first lag at which the autocorrelation of the returns and of their absolute values is no longer
    distinguishable from zero (2/sqrt(n)), the larger of the two, within [low, high]."""
    n = len(returns)
    limit = 2 / math.sqrt(n)
    out = low
    for series in (returns - returns.mean(), np.abs(returns) - np.abs(returns).mean()):
        denominator = float((series ** 2).sum()) or 1.0
        lag = 1
        while lag < high and abs(float((series[lag:] * series[:-lag]).sum()) / denominator) > limit:
            lag += 1
        out = max(out, lag)
    return int(min(high, out))


def block_bootstrap(returns: np.ndarray, horizon: int, paths: int, rng: np.random.Generator, block: int) -> np.ndarray:
    """Stationary block bootstrap: blocks of geometric length (mean ``block``) from a circular copy of the returns."""
    n = len(returns)
    out = np.empty((paths, horizon))
    for i in range(paths):
        position, filled = int(rng.integers(n)), 0
        while filled < horizon:
            take = min(int(rng.geometric(1 / block)), horizon - filled)
            index = (position + np.arange(take)) % n
            out[i, filled:filled + take] = returns[index]
            filled += take
            position = int(rng.integers(n))
    return out


def ewma_vol(returns: np.ndarray, lam: float = 0.94) -> float:
    variance = float(np.var(returns[:20])) if len(returns) >= 20 else float(np.var(returns))
    for r in returns:
        variance = lam * variance + (1 - lam) * float(r) ** 2
    return math.sqrt(variance)


def student_paths(df: float, sigma: float, horizon: int, paths: int, rng: np.random.Generator) -> np.ndarray:
    """Independent Student-t returns with standard deviation ``sigma`` (the t scaled to unit variance)."""
    scale = math.sqrt((df - 2) / df) if df > 2 else 1.0
    return rng.standard_t(df, size=(paths, horizon)) * scale * sigma


def percentile_table(samples: np.ndarray) -> dict[int, float]:
    return {p: float(np.percentile(samples, p)) for p in PERCENTILES}


def managed_check(replayed: pd.Series, base_q_by_days=None) -> dict | None:
    """The crypto book's own managed-era days against the proxy: realised volatility and drawdown next to what today's
    positions replayed over the same days would have had, and where the book's drawdown sits in the simulated distribution
    for that many days. About 34 days: a sanity check, not a test."""
    try:
        from desk import drawdown

        levels = drawdown.load_levels().get("crypto")
    except Exception:
        return None
    if levels is None or len(levels) < 10:
        return None
    book = levels.pct_change().dropna()
    same = replayed.reindex(book.index).dropna()
    out = {"days": int(len(book)), "book_vol_daily": float(book.std(ddof=1)), "book_mean_daily": float(book.mean()),
           "book_max_dd": float(-drawdown.drawdowns(levels).min()),
           "replay_vol_same_days": float(same.std(ddof=1)) if len(same) > 3 else None,
           "correlation_with_replay": float(book.reindex(same.index).corr(same)) if len(same) > 3 else None}
    if base_q_by_days is not None:
        dd = base_q_by_days(len(book))
        out["book_dd_percentile"] = float((dd < out["book_max_dd"]).mean())
    return out


# ------------------------------------------------------------------------------------------------------------ the fit
def fit(weights: dict[str, float] | None = None, path: Path | None = None, paths: int = 4000, outer: int = 60, seed: int = 7) -> dict:
    """Everything the write-up and the monitor use: the distributions, the two levels in sigma units, false alarms, power."""
    from scipy import stats as sps

    rng = np.random.default_rng(seed)
    weights = weights if weights is not None else crypto_weights()
    if not weights:
        raise ValueError("no crypto positions to replay")
    replayed, info = replay_returns(weights, panel_returns(path))
    r = replayed.to_numpy()
    n = len(r)
    sigma = float(r.std(ddof=1))
    sigma_now = ewma_vol(r)
    block = block_length(r)
    centred = r - r.mean()
    drift = lambda sharpe, s: sharpe * s / math.sqrt(DAYS)          # daily drift for an annualised Sharpe at daily vol s
    df = float(sps.t.fit(centred / sigma)[0])
    df = float(min(max(df, 3.0), 30.0))

    def block_max_dd(series: np.ndarray, horizon: int, sharpe: float, vol_scale: float, count: int, blk: int) -> np.ndarray:
        base = series - series.mean()
        s = float(base.std(ddof=1))
        sample = block_bootstrap(base * vol_scale, horizon, count, rng, blk) + drift(sharpe, s)
        return max_drawdown(sample)

    out = {"as_of": pd.Timestamp.now(tz="UTC").isoformat(), "days": n, "first_day": str(replayed.index[0].date()),
           "last_day": str(replayed.index[-1].date()), "weights": {c: round(w, 4) for c, w in sorted(weights.items())},
           "net": info["net"], "gross": info["gross"], "stand_in": info["stand_in"], "sigma_daily": sigma,
           "sigma_now": sigma_now, "block": block, "t_df": df, "assumed_sharpe": ASSUMED_SHARPE, "quarter": QUARTER,
           "replay_mean_daily": float(r.mean()), "scenarios": {}, "distribution": {}}
    for sharpe in (0.0, ASSUMED_SHARPE, 2.0):
        label = f"sharpe_{sharpe:g}"
        out["scenarios"][label] = {}
        for horizon in HORIZONS:
            blocked = block_max_dd(r, horizon, sharpe, 1.0, paths, block)
            student = max_drawdown(student_paths(df, sigma_now, horizon, paths, rng) + drift(sharpe, sigma_now))
            intervals = {p: [] for p in PERCENTILES}
            for _ in range(outer):                       # sampling uncertainty of the replay: resample it, redo the simulation
                resampled = block_bootstrap(r, n, 1, rng, block)[0]
                dd = block_max_dd(resampled, horizon, sharpe, 1.0, 500, block)
                for p in PERCENTILES:
                    intervals[p].append(float(np.percentile(dd, p)))
            out["scenarios"][label][str(horizon)] = {
                "bootstrap": percentile_table(blocked), "student_t": percentile_table(student),
                "bootstrap_ci": {p: [float(np.percentile(v, 5)), float(np.percentile(v, 95))] for p, v in intervals.items()}}
    # The levels: percentiles of the quarter's max drawdown under the assumed edge, as multiples of sigma * sqrt(quarter).
    base_q = block_max_dd(r, QUARTER, ASSUMED_SHARPE, 1.0, paths * 3, block)
    unit = sigma * math.sqrt(QUARTER)
    levels = {name: float(np.percentile(base_q, 100 * (1 - p))) for name, p in FALSE_ALARM.items()}
    out["levels"] = {name: {"drawdown": value, "sigma_units": value / unit, "false_alarm_per_quarter": FALSE_ALARM[name]}
                     for name, value in levels.items()}
    ci = {name: [] for name in FALSE_ALARM}
    for _ in range(outer):   # how well the replay pins each level: resample it and re-derive the level
        resampled = block_bootstrap(r, n, 1, rng, block)[0]
        dd = block_max_dd(resampled, QUARTER, ASSUMED_SHARPE, 1.0, 600, block)
        for name, p in FALSE_ALARM.items():
            ci[name].append(float(np.percentile(dd, 100 * (1 - p))))
    for name in FALSE_ALARM:
        out["levels"][name]["ci90"] = [float(np.percentile(ci[name], 5)), float(np.percentile(ci[name], 95))]
    out["managed_check"] = managed_check(replayed, base_q_by_days=lambda days: block_max_dd(r, days, ASSUMED_SHARPE, 1.0, 3000, block))
    student_q = max_drawdown(student_paths(df, sigma_now, QUARTER, paths * 3, rng) + drift(ASSUMED_SHARPE, sigma_now))
    out["levels_student_t"] = {name: float(np.percentile(student_q, 100 * (1 - p))) for name, p in FALSE_ALARM.items()}
    # Power: how often each level is crossed within a quarter and within two when the model is degraded.
    out["power"] = {}
    for label, sharpe, vol in (("model right", ASSUMED_SHARPE, 1.0), ("no edge", 0.0, 1.0), ("negative edge", -1.0, 1.0),
                               ("vol +50%", ASSUMED_SHARPE, 1.5), ("no edge and vol +50%", 0.0, 1.5)):
        row = {}
        for horizon, tag in ((QUARTER, "quarter"), (2 * QUARTER, "two quarters")):
            dd = block_max_dd(r, horizon, sharpe, vol, paths * 2, block)
            row[tag] = {name: float((dd >= value).mean()) for name, value in levels.items()}
        out["power"][label] = row
    out["frequency"] = {name: float((base_q >= value).mean()) for name, value in levels.items()}
    return out


def save_fit(result: dict, path: Path | None = None) -> Path:
    target = path or FIT_FILE
    target.write_text(json.dumps(result, indent=1))
    return target


def load_fit(path: Path | None = None) -> dict | None:
    try:
        return json.loads((path or FIT_FILE).read_text())
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------------- the monitor
def levels_now(fitted: dict, weights: dict[str, float] | None = None, path: Path | None = None) -> dict | None:
    """The two drawdown levels today: the stored multiples times the book's volatility over a quarter, from today's
    positions replayed over the panel (the stored volatility when the positions cannot be read)."""
    sigma = fitted["sigma_daily"]
    source = "stored"
    try:
        weights = weights if weights is not None else crypto_weights()
        if weights:
            replayed, _ = replay_returns(weights, panel_returns(path))
            sigma = float(replayed.std(ddof=1))
            source = "today's positions"
    except Exception as exc:
        from desk import degraded

        degraded.report("Crypto drawdown levels", exc)
    unit = sigma * math.sqrt(fitted["quarter"])
    return {"watch": fitted["levels"]["watch"]["sigma_units"] * unit,
            "re-evaluate": fitted["levels"]["re-evaluate"]["sigma_units"] * unit, "sigma": sigma, "source": source}


def level_reached(drawdown: float | None, levels: dict | None) -> str | None:
    """'re-evaluate' or 'watch' when the crypto drawdown (a negative share) is at or past a level, else None."""
    if levels is None or drawdown is None or not math.isfinite(drawdown):
        return None
    depth = -drawdown
    if depth >= levels["re-evaluate"] - 1e-12:
        return "re-evaluate"
    if depth >= levels["watch"] - 1e-12:
        return "watch"
    return None


def current_drawdown(levels_of: dict[str, pd.Series] | None = None) -> float | None:
    """The crypto book's drawdown from its high-water mark over the managed era (a negative share)."""
    from desk import drawdown

    series = (levels_of or drawdown.load_levels()).get("crypto")
    return float(drawdown.drawdowns(series).iloc[-1]) if series is not None and len(series) else None


def alerts(drawdown_now: float | None, levels: dict | None, fitted: dict | None) -> list[list[str]]:
    """Attention rows for a crypto drawdown at a level (alert only), the evidence in the text."""
    level = level_reached(drawdown_now, levels)
    if level is None or fitted is None:
        return []
    from desk.numbers import pct

    frequency = fitted["levels"][level]["false_alarm_per_quarter"]
    evidence = (f"Crypto book {pct(drawdown_now)} from its high; the {level} level is {pct(-levels[level])} (a book whose model is "
                f"right crosses it in about {frequency:.0%} of quarters, assuming a Sharpe of {fitted['assumed_sharpe']:g}).")
    if level == "re-evaluate":
        return [["Crypto drawdown: re-evaluate models", evidence + " Check the theses, sizing and regime before adding risk."]]
    return [["Crypto drawdown: watch", evidence]]


def notify(drawdown_now: float | None, levels: dict | None, fitted: dict | None, state_path: Path | None = None, send=None) -> str | None:
    """A phone push when the level changes to watch or re-evaluate (once per level); returns the level sent."""
    level = level_reached(drawdown_now, levels)
    path = state_path or ALERT_STATE
    try:
        last = json.loads(path.read_text()).get("level")
    except (OSError, ValueError):
        last = None
    if level == last:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"level": level, "at": pd.Timestamp.now(tz="UTC").isoformat()}))
    if level is None:
        return None
    rows = alerts(drawdown_now, levels, fitted)
    if not rows:
        return None
    if send is None:
        from desk.push import send
    send(rows[0][0], rows[0][1])
    return level


def status() -> dict | None:
    """Today's crypto drawdown against the two levels, for the tile, the alerts and the command line."""
    fitted = load_fit()
    if fitted is None:
        return None
    levels = levels_now(fitted)
    drawdown_now = current_drawdown()
    return {"fit": fitted, "levels": levels, "drawdown": drawdown_now, "reached": level_reached(drawdown_now, levels)}


def main(argv: list[str] | None = None) -> int:
    command = (argv if argv is not None else sys.argv[1:] or ["status"])[0]
    if command == "fit":
        result = fit()
        save_fit(result)
        print(json.dumps({k: result[k] for k in ("days", "net", "gross", "sigma_daily", "block", "t_df", "levels", "frequency")}, indent=1))
    elif command == "status":
        info = status()
        if info is None:
            print("no fit yet: run python -m desk.drawdown_monitor fit")
            return 1
        levels = info["levels"] or {}
        print(f"crypto drawdown {info['drawdown']:.2%}; watch {levels.get('watch', float('nan')):.2%}, "
              f"re-evaluate {levels.get('re-evaluate', float('nan')):.2%} ({levels.get('source')}); reached: {info['reached']}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
