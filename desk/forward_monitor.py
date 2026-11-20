"""Forward monitor: every rule, signal and sizing method that runs in prod, tested historically AND watched forward.

Policy (user, 2026-11-19): a backtest says a rule worked then; only its live record says it still works now. Each item in
REGISTRY carries its historical expectation (the study or replay that justified it, or none, which is a flag itself) and
a live outcome stream in the same units. ``drift`` compares the two and raises:

* **Live drift: <rule>** when live results fall below what the history expected (z <= -2 on the live mean, or a one-sided
  CUSUM alarm on outcomes standardised by the historical spread). A flag means investigate: the market changed, or what we
  do has a flaw. Never on tiny samples: under MIN_LIVE outcomes the status is "too few live".
* **No historical test: ...** for items running without a study behind them.
* **No forward test: ...** for items whose live outcome cannot be measured yet.

Live data: the daemon's log (fetched from the desk server by the daily job: ``fetch_server_data``), the closed-trades
files, the trade journal, the order-book signal log and daily screener snapshots. Prices for forward returns come from
Hyperliquid hourly candles (cached).

    python -m desk.forward_monitor          # evaluate, save data/forward_monitor.json and publish the tile
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DATA = REPO / "data"
FORWARD_DIR = DATA / "forward"
STATE_FILE = DATA / "forward_monitor.json"
LOG_COPY = FORWARD_DIR / "hl_daemon_events.log"
PRICE_CACHE = FORWARD_DIR / "prices.json"
SCREENER_LOG = FORWARD_DIR / "screener_log.jsonl"
BOOK_SIGNALS_DIR = REPO / "research" / "data" / "orderbook_signals"
RULES_TEST = REPO / "research" / "data" / "binance_rules_test.csv"
MM_REPLAY = REPO / "research" / "data" / "neutral_mm_replay.csv"

MIN_LIVE = 10          # fewer live outcomes than this: no verdict
DRIFT_Z = -2.0         # live mean this many standard errors below the expectation
CUSUM_K, CUSUM_H = 0.5, 4.0   # one-sided CUSUM on standardised outcomes (slack, alarm level)
TRADE_COST = 0.001     # taker in and out plus slippage, as the rules replay


# ---------------------------------------------------------------------------------------------------------- the test
@dataclass
class Expectation:
    """What the history says one outcome should average: mean (per event), its spread, and whether it passed the desk's
    significance rule (t >= 2 in both halves)."""
    label: str
    mean: float
    sd: float | None
    n: int
    passed: bool
    source: str


def expectation_from_t(label: str, mean: float, n: int, t: float, t1: float | None, t2: float | None, source: str) -> Expectation:
    sd = abs(mean) * math.sqrt(n) / abs(t) if t and n else None
    passed = t1 is not None and t2 is not None and mean > 0 and min(t1, t2) >= 2
    return Expectation(label, mean, sd, n, passed, source)


def expectation_from_sample(label: str, values: list[float], source: str) -> Expectation | None:
    values = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(values) < 2 * MIN_LIVE:
        return None
    series = pd.Series(values)
    half = len(series) // 2
    t = lambda s: s.mean() / (s.std(ddof=1) / math.sqrt(len(s))) if len(s) > 1 and s.std(ddof=1) > 0 else 0.0  # noqa: E731
    return Expectation(label, float(series.mean()), float(series.std(ddof=1)), len(series),
                       bool(series.mean() > 0 and min(t(series[:half]), t(series[half:])) >= 2), source)


def cusum_alarm(values: list[float], mean: float, sd: float) -> bool:
    """One-sided (downside) CUSUM: True when live outcomes run persistently below the expected mean."""
    if not sd or sd <= 0:
        return False
    s = 0.0
    for value in values:
        s = max(0.0, s + (mean - value) / sd - CUSUM_K)
        if s > CUSUM_H:
            return True
    return False


def drift(live: list[float], expected: Expectation | None) -> dict:
    """Live mean against the expectation: z of the live mean (live standard error) and the CUSUM alarm."""
    live = [v for v in live if isinstance(v, (int, float)) and math.isfinite(v)]
    n = len(live)
    out = {"n": n, "live_mean": (sum(live) / n) if n else None, "z": None, "cusum": False}
    if expected is None or n < MIN_LIVE:
        return out
    series = pd.Series(live)
    se = series.std(ddof=1) / math.sqrt(n) if n > 1 else 0.0
    out["z"] = float((series.mean() - expected.mean) / se) if se > 0 else None
    out["cusum"] = cusum_alarm(live, expected.mean, expected.sd or float(series.std(ddof=1)))
    return out


# ---------------------------------------------------------------------------------------------------------- the registry
@dataclass
class Item:
    key: str
    name: str
    purpose: str                    # research | strategy | execution | risk (the commit-label purposes)
    since: str                      # live since (date): outcomes before it are history, not live
    expectation: Callable[["Sources"], Expectation | None] | None = None
    live: Callable[["Sources"], list[float]] | None = None
    unit: str = "return"            # how an outcome reads: return (per event) or per $1K traded
    off: str = ""                   # set when the rule is switched off (why)
    forward_only: str = ""          # no backtest by design or a failed one: the live record must show an edge (t>=2 both halves)
    note: str = ""
    monitor: str = ""               # monitoring, not a trading rule (alert only): no edge to test, shown with this status


@dataclass
class Sources:
    """Where live data comes from; tests pass their own."""
    log_text: str = ""
    closed_crypto: pd.DataFrame = field(default_factory=pd.DataFrame)
    closed_stocks: pd.DataFrame = field(default_factory=pd.DataFrame)
    journal: list = field(default_factory=list)
    price_at: Callable[[str, datetime], float | None] | None = None    # coin, time -> price (hourly close)
    book_signal_frame: pd.DataFrame | None = None
    screener_log: list = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _rules_row(test: str) -> Callable[[Sources], Expectation | None]:
    def load(_: Sources) -> Expectation | None:
        try:
            table = pd.read_csv(RULES_TEST)
        except OSError:
            return None
        row = table[table["test"] == test]
        if row.empty:
            return None
        r = row.iloc[0]
        return expectation_from_t(test, float(r["mean"]), int(r["events"]), float(r["t"]), float(r["t_first"]),
                                  float(r["t_second"]), "research/binance_rules_test.py")
    return load


ACTED_ON = re.compile(r"(?:[Tt]rimmed shorts by \d+%|[Tt]rimmed \d+% of the shorts|[Bb]anked[^:]*|[Ff]lattened[^:]*): (.*)$")
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC (.*)$")


def log_events(text: str, pattern: str) -> list[tuple[datetime, str]]:
    out = []
    for line in text.splitlines():
        match = LOG_LINE.match(line)
        if match and re.search(pattern, match.group(2)) and "ALERT" not in match.group(2)[:6]:
            out.append((datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc), match.group(2)))
    return out


def _forward_after_trims(pattern: str, hours: int = 24, sign: int = 1) -> Callable[[Sources], list[float]]:
    """Per auto event: the mean forward move of the coins it acted on over ``hours`` (a trim of shorts earns the bounce it
    stops paying for), net of the trade cost; events younger than ``hours`` are left out."""
    def live(src: Sources) -> list[float]:
        outcomes = []
        for at, text in log_events(src.log_text, pattern):
            if src.now - at < timedelta(hours=hours) or src.price_at is None:
                continue
            listed = ACTED_ON.search(text)
            if not listed:
                continue
            stocks = "SP500" in text
            coins = [part.split()[0] for part in re.split(r"[;,]", listed.group(1)) if part.split()]   # "xyz:MSFT 0.043" -> "xyz:MSFT"
            moves = []
            for coin in coins:
                name = coin if ":" in coin or not stocks else f"xyz:{coin}"
                start, end = src.price_at(name, at), src.price_at(name, at + timedelta(hours=hours))
                if start and end:
                    moves.append(sign * (end / start - 1))
            if moves:
                outcomes.append(sum(moves) / len(moves) - TRADE_COST)
        return outcomes
    return live


NOTICE_MOVE = re.compile(r"\bBTC \S+ (up|down) in ")


def _notice_followthrough(pattern: str, hours: int = 72) -> Callable[["Sources"], list[float]]:
    """Per "test thesis?" notice: BTC's further move in the direction of the alert over ``hours``. Positive means the
    adverse move went on after the notice (the thesis was rightly questioned); negative means it reversed."""
    def live(src: "Sources") -> list[float]:
        outcomes = []
        for at, text in log_events(src.log_text, pattern):
            match = NOTICE_MOVE.search(text)
            if not match or src.now - at < timedelta(hours=hours) or src.price_at is None:
                continue
            start, end = src.price_at("BTC", at), src.price_at("BTC", at + timedelta(hours=hours))
            if start and end:
                outcomes.append((1 if match.group(1) == "up" else -1) * (end / start - 1))
        return outcomes
    return live


def _closed_returns(frame_of: Callable[[Sources], pd.DataFrame], since: str, before: bool,
                    account: str | None = None) -> Callable[[Sources], list[float]]:
    def get(src: Sources) -> list[float]:
        frame = frame_of(src)
        if frame is None or frame.empty or "time" not in frame:
            return []
        times = pd.to_datetime(frame["time"], utc=True, format="ISO8601")
        mask = (times < pd.Timestamp(since, tz="UTC")) if before else (times >= pd.Timestamp(since, tz="UTC"))
        if account:
            mask &= frame["account"].astype(str) == account
        rows = frame[mask]
        if "return" in rows:
            return [float(v) for v in rows["return"] if pd.notna(v)]
        return []
    return get


def _history(frame_of: Callable[[Sources], pd.DataFrame], since: str, label: str, account: str | None = None):
    def load(src: Sources) -> Expectation | None:
        return expectation_from_sample(label, _closed_returns(frame_of, since, True, account)(src),
                                       "closed trades before the change")
    return load


def _mm_expectation(_: Sources) -> Expectation | None:
    try:
        table = pd.read_csv(MM_REPLAY)
    except OSError:
        return None
    values = [float(v) / 1000 for v in table["per_1k"] if pd.notna(v)]   # per $1 traded, the live unit below
    if len(values) < 2:
        return None
    series = pd.Series(values)
    return Expectation("ladder PnL per $ traded", float(series.mean()), float(series.std(ddof=1)), len(values), False,
                       "research/neutral_mm_replay.py (6 runs, not significant)")


def _mm_live(src: Sources) -> list[float]:
    frame = src.closed_crypto
    if frame.empty:
        return []
    times = pd.to_datetime(frame["time"], utc=True, format="ISO8601")
    rows = frame[(times >= pd.Timestamp("2026-11-13", tz="UTC")) & (frame["account"].astype(str) == "c248")]
    return [float(p) / float(n) for p, n in zip(rows["pnl"], rows["notional"]) if n]


def _sizing_live(src: Sources) -> list[float]:
    """Trades opened since the sizing restore, closed: their return (the sizing's own test needs planned risk, not
    recorded yet)."""
    return _closed_returns(lambda s: pd.concat([s.closed_crypto, s.closed_stocks]) if not (s.closed_crypto.empty and s.closed_stocks.empty)
                           else pd.DataFrame(), "2026-11-20", False)(src)


def _book_signals_live(src: Sources) -> list[float]:
    """The order-book logger's best signal series (net per trade); its verdicts are the forward test itself."""
    if src.book_signal_frame is None or src.book_signal_frame.empty:
        return []
    from desk import book_signals

    frame = book_signals.forward_returns(src.book_signal_frame, 60)
    best: list[float] = []
    for name in book_signals.TESTED_SIGNALS:
        if name in frame:
            series = book_signals.signal_series(frame, name)
            if len(series) > len(best):
                best = [float(v) for v in series]
    return best


def _screener_live(src: Sources) -> list[float]:
    """Each logged screener short: the coin's 7-day move against BTC from the snapshot (a short earns when it lags)."""
    outcomes = []
    for snap in src.screener_log:
        at = datetime.fromisoformat(snap["as_of"].replace("Z", "+00:00"))
        if src.now - at < timedelta(days=7) or src.price_at is None:
            continue
        btc0, btc1 = src.price_at("BTC", at), src.price_at("BTC", at + timedelta(days=7))
        for coin in snap.get("shorts", []):
            p0, p1 = src.price_at(coin, at), src.price_at(coin, at + timedelta(days=7))
            if p0 and p1 and btc0 and btc1:
                outcomes.append(-((p1 / p0) - (btc1 / btc0)) - TRADE_COST)
    return outcomes


def _count(pattern: str) -> Callable[[Sources], list[float]]:
    return lambda src: []   # events without a measurable outcome: the count is shown, there is no forward test


REGISTRY: list[Item] = [
    Item("trim-shorts", "Auto: trim shorts on an extraordinary down day", "risk", "2026-11-15",
         _rules_row("trim down: trim shorts: alts next 24h (net)"), _forward_after_trims(r"^auto trimmed shorts"),
         note="outcome: the trimmed coins' next 24h move (the bounce the shorts no longer pay)"),
    Item("bank-shorts", "Auto: bank shorts on a post-sell-off bounce", "risk", "2026-11-15",
         _rules_row("bank: alts next 24h (net)"), _forward_after_trims(r"^auto bank")),
    Item("flatten", "Auto: test-thesis notice on adverse momentum", "risk", "2026-11-20", None,
         _notice_followthrough(r"^auto test thesis\?", hours=72),
         forward_only="a notice since 2026-11-19 (closes nothing): tracks whether the adverse move went on",
         note="outcome: BTC's further move in the alert's direction over 72h (positive: the adverse move continued)"),
    Item("btc-flip", "Auto: BTC momentum flip", "strategy", "2026-11-15", _rules_row("flip: BTC trail 1.5% (net)"),
         off="off: failed the replay (583 bursts, nothing after costs)"),
    Item("trim-longs", "Auto: trim longs on an extraordinary up day", "risk", "2026-11-15",
         _rules_row("trim up: trim longs: alts next 24h (net)"), off="off: trimming longs lost in both halves"),
    Item("regime-guard", "Regime guard", "risk", "2026-11-11", None, _count(r"regime guard")),
    Item("margin-guard", "Margin guard", "risk", "2026-11-11", None, _count(r"margin guard")),
    Item("ladders", "Market-making ladders (c248)", "execution", "2026-11-13", _mm_expectation, _mm_live, unit="per $ traded",
         note="neutral tests: ETH (since 2026-11-16) and NEAR (since 2026-11-20), $11 a level, 5 levels; review at 200 fills each "
              "(verdict_fills 200 in ladder.json): net edge per $1K must reach t>=2, else the test ends (research/mm_tests.md)"),
    Item("crypto-entries", "Crypto entries without stops", "strategy", "2026-11-17",
         _history(lambda s: s.closed_crypto, "2026-11-17", "crypto trade return", "07f2"),
         _closed_returns(lambda s: s.closed_crypto, "2026-11-17", False, "07f2"),
         note="history: the same wallet's closed trades before stops were removed"),
    Item("stock-entries", "Stock entries without stops", "strategy", "2026-11-17",
         _history(lambda s: s.closed_stocks, "2026-11-17", "stock trade return"),
         _closed_returns(lambda s: s.closed_stocks, "2026-11-17", False)),
    Item("entry-sizing", "Entry sizing: half-Kelly, volatility", "execution", "2026-11-20", None, _sizing_live,
         note="no study of sizing outcomes; live return of trades opened since the restore"),
    Item("book-signals", "Order-book signals (logger)", "research", "2026-11-19", None, _book_signals_live,
         forward_only="forward-only by design", note="best signal's net return per snapshot at 60 minutes"),
    Item("screener", "Screener short candidates", "strategy", "2026-11-20", None, _screener_live,
         forward_only="history: picks vs basket not established", note="outcome: 7-day move against BTC from each daily snapshot"),
    Item("take-profits", "Held take-profit on crypto entries (single target)", "execution", "2026-11-20", None, _count(r"take-profit"),
         note="one held target at the sizing distance x reward; crypto only, stocks held passively. The take-profit ladder "
              "(desk/tp_ladder.py) is off until research/tp_ladder_replay.py shows it beats the single target"),
    Item("mm-tuning", "Adaptive market making (c248)", "execution", "2026-11-20", None, _count(r"tuned "),
         note="every 6 hours from the ladders' own fills (markouts 1m/5m/60m, edge per $1K) and the market; bounded steps, "
              "no change under 30 fills (desk/mm_tuning.py); no study that tuning beats fixed settings"),
    Item("ladder-no-loss", "Ladders never reduce at a loss", "risk", "2026-11-20", None, _count(r"ladder"),
         note="trims and inventory-reducing quotes only at a better price than the position's entry"),
    Item("dd-tolerance", "Desk drawdown tolerance alert", "risk", "2026-11-20", None, None,
         monitor="monitoring, alert only", note="the investor's accepted drawdown (desk/fiduciary.json); attention at 75% "
         "of it and at it, phone push on a new level; nothing trades on it (desk/drawdown.py)"),
    Item("dd-crypto-models", "Crypto drawdown: re-evaluate models", "risk", "2026-11-20", None, None,
         monitor="monitoring, alert only", note="the crypto book's drawdown against two statistical levels (watch about a third of "
         "quarters, re-evaluate about 1 in 12, for a book whose model is right at a Sharpe of 1, proxy replay of today's "
         "positions); a crossing means check the theses, sizing and regime (desk/drawdown_monitor.py, "
         "research/crypto_drawdown_threshold.md)"),
    Item("claude-suggestions", "Claude suggestions", "strategy", "2026-11-21", None, None,
         monitor="monitoring, no trading rule", note="every recommendation logged when made (data/suggestions.jsonl) and scored against the price at 1, 3, 7 days and "
         "against what was executed, split strategy-fit vs additional; too few below 20 scored (desk/suggestions.py, research/suggestions_review.md)"),
    Item("cap-flex", "Risk-cap flex, opt-in per trade", "risk", "2026-11-20", None, _count(r"FLEX"),
         note="3-point flex (33% to 36%) used only when chosen on the confirm step or with --use-flex"),
]


# ---------------------------------------------------------------------------------------------------------- evaluation
def _pct(value: float | None) -> str:
    from desk.numbers import pct

    return pct(value) if value is not None and math.isfinite(value) else ""


def evaluate(src: Sources, registry: list[Item] = REGISTRY) -> list[dict]:
    rows = []
    for item in registry:
        try:
            expected = item.expectation(src) if item.expectation else None
        except Exception as exc:   # a broken source is a status, not a crash
            expected, item_error = None, f"history unreadable ({type(exc).__name__})"
        else:
            item_error = ""
        try:
            live = item.live(src) if item.live and not item.off else []
        except Exception as exc:
            live, item_error = [], f"live data unreadable ({type(exc).__name__})"
        result = drift(live, expected)
        if item.monitor:
            status = item.monitor
        elif item.off:
            status = item.off
        elif item_error:
            status = item_error
        elif item.forward_only:
            status = forward_verdict(live, item.forward_only)
        elif expected is None:
            status = "no historical test"
        elif item.live is None or item.live.__qualname__.startswith("_count"):
            status = "no forward test"
        elif result["n"] < MIN_LIVE:
            status = f"too few live ({result['n']})" if result["n"] else "no live outcomes yet"
        elif (result["z"] is not None and result["z"] <= DRIFT_Z) or result["cusum"]:
            status = "live drift"
        else:
            status = "in line"
        if status == "in line" and expected is not None and not expected.passed:
            status = "in line; history not significant"
        live_text = _pct(result["live_mean"]) if result["n"] else ""
        expected_text = _pct(expected.mean) if expected is not None else ""
        rows.append({"key": item.key, "Rule": item.name, "Purpose": item.purpose, "Live n": result["n"] or None,
                     "Live vs expected": f"{live_text} vs {expected_text}" if live_text and expected_text else live_text or (f"exp {expected_text}" if expected_text else ""),
                     "Status": status, "z": result["z"], "cusum": result["cusum"],
                     "history": (expected.source if expected else ""), "history_passed": bool(expected and expected.passed),
                     "note": item.note, "forward": bool(item.live) and not item.live.__qualname__.startswith("_count") and not item.off})
    return rows


def forward_verdict(live: list[float], history: str) -> str:
    """For an item whose history is absent by design or failed: does the live record show an edge yet?"""
    values = [v for v in live if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(values) < MIN_LIVE:
        return f"too few live ({len(values)}); {history}" if values else f"no live outcomes yet; {history}"
    passed = expectation_from_sample("live", values, "")
    if passed is not None and passed.passed:
        return "edge holding live (t>=2 both halves)"
    return f"no edge live so far; {history}"


def attention(rows: list[dict]) -> list[list[str]]:
    out = []
    for row in rows:
        if row["Status"] == "live drift":
            out.append([f"Live drift: {row['Rule']}", f"{row['Live vs expected']} over {row['Live n']} live; investigate."])
    missing_history = [r["Rule"] for r in rows if r["Status"] == "no historical test"]
    if missing_history:
        out.append(["No historical test", "; ".join(missing_history) + "."])
    missing_forward = [r["Rule"] for r in rows if not r.get("forward") and not r["Status"].startswith(("off", "monitoring"))]
    if missing_forward:
        out.append(["No forward test", "; ".join(missing_forward) + "."])
    return out


# ---------------------------------------------------------------------------------------------------------- live data
def fetch_server_data(host: str = "desk") -> list[str]:
    """Copy the daemon's event lines and the order-book signal log from the desk server (the daily job). Failures are
    reported, not raised."""
    notes = []
    FORWARD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        text = subprocess.run(["ssh", host, "grep -E ' UTC (auto |regime guard|margin guard|.*take-profit)' ~/.trading-desk/hl_daemon.log"],
                              capture_output=True, text=True, timeout=60).stdout
        if text:
            LOG_COPY.write_text(text)
    except Exception as exc:
        notes.append(f"daemon log not fetched ({type(exc).__name__})")
    try:
        BOOK_SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["rsync", "-q", f"{host}:trading-desk/research/data/orderbook_signals/", str(BOOK_SIGNALS_DIR) + "/"],
                       capture_output=True, timeout=120)
    except Exception as exc:
        notes.append(f"order-book log not fetched ({type(exc).__name__})")
    return notes


def snapshot_screener() -> None:
    """Append today's screener shorts (data/screener.json) to the forward log, once per screener run."""
    try:
        screener = json.loads((DATA / "screener.json").read_text())
    except (OSError, ValueError):
        return
    as_of = screener.get("as_of")
    shorts = [row.get("Coin") for row in screener.get("shorts", []) if row.get("Coin")]
    if not as_of or not shorts:
        return
    seen = {json.loads(line).get("as_of") for line in SCREENER_LOG.read_text().splitlines()} if SCREENER_LOG.exists() else set()
    if as_of in seen:
        return
    SCREENER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SCREENER_LOG.open("a") as handle:
        handle.write(json.dumps({"as_of": as_of, "shorts": shorts}) + "\n")


def hourly_price_at() -> Callable[[str, datetime], float | None]:
    """Coin, time -> the hourly close at or after that time, from Hyperliquid (cached on disk)."""
    from desk.hl.trade_analytics import post_info

    try:
        cache = json.loads(PRICE_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}

    def price_at(coin: str, at: datetime) -> float | None:
        hour = int(at.timestamp() // 3600 * 3600 * 1000)
        key = f"{coin}@{hour}"
        if key in cache:
            return cache[key]
        if at > datetime.now(timezone.utc):
            return None
        try:
            rows = post_info({"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": hour,
                                                                "endTime": hour + 2 * 3600 * 1000}})
            value = float(rows[0]["c"]) if rows else None
        except Exception:
            value = None
        if value is not None:
            cache[key] = value
            PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            PRICE_CACHE.write_text(json.dumps(cache))
        return value
    return price_at


def live_sources() -> Sources:
    def read_csv(path: Path) -> pd.DataFrame:
        try:
            return pd.read_csv(path)
        except OSError:
            return pd.DataFrame()
    try:
        journal = json.loads((DATA / "trade_journal.json").read_text())
    except (OSError, ValueError):
        journal = []
    frame = None
    try:
        from desk import book_signals

        frame = book_signals.load_log(BOOK_SIGNALS_DIR)
    except Exception:
        frame = None
    screener = [json.loads(line) for line in SCREENER_LOG.read_text().splitlines()] if SCREENER_LOG.exists() else []
    return Sources(log_text=LOG_COPY.read_text() if LOG_COPY.exists() else "",
                   closed_crypto=read_csv(DATA / "closed_trades_crypto.csv"), closed_stocks=read_csv(DATA / "closed_trades_stocks.csv"),
                   journal=journal, price_at=hourly_price_at(), book_signal_frame=frame, screener_log=screener)


def run(push: bool = True, fetch: bool = True, directory: Path | None = None) -> dict:
    notes = fetch_server_data() if fetch else []
    snapshot_screener()
    rows = evaluate(live_sources())
    result = {"as_of": datetime.now(timezone.utc).isoformat(), "items": rows, "attention": attention(rows), "notes": notes}
    STATE_FILE.write_text(json.dumps(result, default=str))
    publish(result, push=push, directory=directory)
    return result


def publish(result: dict, push: bool = True, directory: Path | None = None) -> None:
    from desk import tile_feed

    table = pd.DataFrame([{k: row[k] for k in ("Rule", "Live n", "Live vs expected", "Status")} for row in result["items"]])
    tile_feed.publish(table, "Forward monitor", reason="each prod rule's live record against its history", key="forward-monitor",
                      relevance=0.1, hours=48, formats={"Live n": "num"}, push=push, directory=directory,
                      attention=result["attention"])


def main() -> int:
    result = run()
    for row in result["items"]:
        print(f"  {row['Rule']}: {row['Status']}" + (f" ({row['Live vs expected']}, n {row['Live n']})" if row["Live n"] else ""))
    for note in result["notes"]:
        print("  note: " + note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
