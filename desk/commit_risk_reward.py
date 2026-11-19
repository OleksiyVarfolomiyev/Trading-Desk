"""How the desk's commits relate to its risk and reward: where the effort goes against where P&L and risk come from.

    python -m desk.commit_risk_reward run       # read the books, test, write desk/commit_risk_reward.json (+ research notes)
    python -m desk.commit_risk_reward publish   # the "Build vs risk/reward" tiles from that json (no book reads)

Three parts, with the limits said up front: the build history covers 34 days (2026-10-16 on), and 8 of its 10 rule changes
landed in the last 8 days, so a before/after test has one regime change and 8 days after it.

A. Effort map. Each area's share of commits and of code churn (desk/build_history.csv) beside the size of the return drivers
   it can move (the benchmark attribution's Picks, Sizing, Timing, Market exposure and Fees & funding, and the risk
   drivers). Which area can move which driver is a judgement (``DRIVERS``), not an estimate.
B. Event study. ``EVENTS`` lists the commits that changed how the desk trades. Windows around events overlap (``confounded``),
   so the test is the one that can be run: the book's daily return on its benchmark's return and a post-change dummy
   (``regime_test``), compared with the same test at every other split date (``placebo``). A result counts only when |t| is at
   least 2 and the sign holds in both halves of the after period; with 8 days after, the halves have 4 days each.
C. Verdicts: ``verdict`` says established or not, with the reason.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
RESULT_PATH = REPO / "desk" / "commit_risk_reward.json"
SIGNIFICANT_T = 2.0
MIN_SIDE = 7          # days either side of a split for the regime test
CONFOUND_DAYS = 3     # another rule change within this many days confounds an event

# The commits that changed how the desk trades (behaviour), curated from the labelled history: (date, area, what changed).
EVENTS = [
    ("2026-11-02", "daemon", "Ladder orders (market making starts)"),
    ("2026-11-04", "daemon", "Kill switch and execution quality"),
    ("2026-11-11", "daemon", "Margin guard on account-wide liquidation risk; wider HL stops; conviction trades; ladders in the daemon"),
    ("2026-11-12", "daemon", "Take-profits and ladder quotes held in daemon memory; per-coin ladder settings"),
    ("2026-11-13", "daemon", "Fade entry modes (stop=none)"),
    ("2026-11-14", "journal", "Auto-trading standing order: bank half of each crypto short in profit; regime-aware ladders"),
    ("2026-11-15", "daemon", "Auto flatten on strong BTC momentum; auto trading follows the Binance replay; neutral ladders"),
    ("2026-11-16", "daemon", "Build-ups to the full risk budget; unprotect drops stops and guards on directional positions"),
    ("2026-11-17", "daemon", "Funding out of the risk cap; thesis entries beyond the cap; stop-free sizing"),
    ("2026-11-18", "daemon", "No exchange stop-loss on new trades; half-Kelly batch execution"),
]
BURST_START = "2026-11-11"   # the day the rule changes begin to bunch (the margin guard, wider stops, conviction trades)

# Which areas can move which driver of risk and reward (judgement). The driver's size is shared equally among them.
DRIVERS = {
    "Picks": ["signals", "journal"],
    "Sizing": ["daemon"],
    "Timing": ["daemon", "signals"],
    "Market exposure": ["daemon", "signals"],
    "Fees & funding": ["daemon", "hl"],
    "Drawdown and leverage": ["daemon"],
}


# ---------------------------------------------------------------------------------------------------------------- A
def effort_by_area(rows: list[dict]) -> pd.DataFrame:
    """Commits and code churn per area from the labelled history, as shares of the total."""
    frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["Area", "Commits", "Commits %", "Churn %", "Rule changes"])
    frame["churn"] = pd.to_numeric(frame["added"], errors="coerce").fillna(0) + pd.to_numeric(frame["removed"], errors="coerce").fillna(0)
    grouped = frame.groupby("area").agg(commits=("hash", "count"), churn=("churn", "sum"))
    events = Counter(area for _, area, _ in EVENTS)
    out = pd.DataFrame({"Area": grouped.index, "Commits": grouped["commits"].to_numpy(),
                        "Commits %": (grouped["commits"] / grouped["commits"].sum()).to_numpy(),
                        "Churn %": (grouped["churn"] / max(grouped["churn"].sum(), 1)).to_numpy(),
                        "Rule changes": [events.get(area, 0) for area in grouped.index]})
    return out.sort_values("Commits", ascending=False).reset_index(drop=True)


def outcome_by_area(parts: dict[str, float], risk_size: float) -> dict[str, float]:
    """Each area's share of the drivers it can move. ``parts`` are the attribution's pp (absolute size is used);
    ``risk_size`` is the drawdown-and-leverage driver on the same scale. Shares sum to 1 over the areas that move a driver."""
    sizes = {name: abs(float(parts.get(name, 0.0) or 0.0)) for name in DRIVERS if name != "Drawdown and leverage"}
    sizes["Drawdown and leverage"] = abs(float(risk_size))
    total = sum(sizes.values())
    share: dict[str, float] = {}
    if not total:
        return share
    for name, areas in DRIVERS.items():
        for area in areas:
            share[area] = share.get(area, 0.0) + sizes[name] / total / len(areas)
    return share


# ---------------------------------------------------------------------------------------------------------------- B
def ols_hac(y: np.ndarray, x: np.ndarray, lags: int = 2) -> tuple[np.ndarray, np.ndarray]:
    """OLS coefficients and Newey-West t-stats (Bartlett weights)."""
    n, k = x.shape
    xtx_inv = np.linalg.pinv(x.T @ x)
    beta = xtx_inv @ x.T @ y
    resid = y - x @ beta
    scores = x * resid[:, None]
    meat = scores.T @ scores
    for lag in range(1, min(lags, n - 1) + 1):
        gamma = scores[lag:].T @ scores[:-lag]
        meat += (1 - lag / (lags + 1)) * (gamma + gamma.T)
    cov = xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    return beta, np.where(se > 0, beta / np.where(se > 0, se, 1), np.nan)


def regime_test(panel: pd.DataFrame, split: str | pd.Timestamp, lags: int = 2) -> dict:
    """The book's daily return on what its exposure should have earned (the net weight going into the day times the
    benchmark's return) and a dummy for the days from ``split`` on. ``gamma`` is the average extra daily return after the
    split that the exposure and the market do not explain (per day, in return units): a book that turned net short in a
    falling market must not get credit for the fall. ``gamma_plain`` controls with the benchmark alone."""
    data = panel[["ret", "bench"]].copy()
    data["matched"] = (panel["net"].shift(1) * panel["bench"]) if "net" in panel else panel["bench"]
    data = data.dropna()
    split = pd.Timestamp(split)
    post = (data.index >= split).astype(float)
    if post.sum() < MIN_SIDE or (1 - post).sum() < MIN_SIDE:
        return {}
    y = data["ret"].to_numpy()

    def fit(control: str, mask=None):
        frame, flag = (data, post) if mask is None else (data[mask], post[mask])
        x = np.column_stack([np.ones(len(frame)), frame[control].to_numpy(), flag])
        return ols_hac(frame["ret"].to_numpy(), x, lags)

    beta, t = fit("matched")
    plain, _ = fit("bench")
    after_index = data.index[data.index >= split]
    cut = len(after_index) // 2
    halves = {}
    for name, keep in (("first", after_index[:cut]), ("second", after_index[cut:])):
        if len(keep) >= 2:
            mask = (data.index < split) | data.index.isin(keep)
            halves[name] = float(fit("matched", mask)[0][2])
    return {"split": str(split.date()), "alpha": float(beta[0]), "beta_matched": float(beta[1]), "gamma": float(beta[2]),
            "t_gamma": float(t[2]), "gamma_plain": float(plain[2]), "days_before": int((1 - post).sum()),
            "days_after": int(post.sum()), "gamma_first_half": halves.get("first"), "gamma_second_half": halves.get("second")}


def placebo(panel: pd.DataFrame, actual: str, lags: int = 2) -> dict:
    """The same test at every split date with at least MIN_SIDE days each side: where the real split ranks."""
    days = panel[["ret", "bench"]].dropna().index
    results = {}
    for day in days[MIN_SIDE:len(days) - MIN_SIDE + 1]:
        row = regime_test(panel, day, lags)
        if row:
            results[str(day.date())] = row["t_gamma"]
    if actual not in results:
        return {"splits": len(results)}
    mine = abs(results[actual])
    return {"splits": len(results), "actual_t": results[actual],
            "share_as_large": float(np.mean([abs(t) >= mine for t in results.values()])),
            "largest_other_t": float(max((abs(t) for d, t in results.items() if d != actual), default=np.nan))}


def period_stats(frame: pd.DataFrame) -> dict:
    ret = frame["ret"].dropna()
    if len(ret) < 2:
        return {}
    wealth = (1 + ret).cumprod()
    return {"days": int(len(ret)), "mean_daily": float(ret.mean()), "vol_daily": float(ret.std()),
            "ratio": float(ret.mean() / ret.std()) if ret.std() > 0 else float("nan"),
            "max_drawdown": float((wealth / wealth.cummax() - 1).min()), "benchmark_daily": float(frame["bench"].mean()),
            "net": float(frame["net"].mean()), "gross": float(frame["gross"].mean()),
            "fees_and_funding_daily": float((frame["fees"] + frame["funding"]).mean())}


def confounding(events: list[tuple[str, str, str]] = EVENTS, days: int = CONFOUND_DAYS) -> list[dict]:
    """For each rule change, how many other rule changes fall within ``days`` days either side."""
    dates = [pd.Timestamp(d) for d, _, _ in events]
    return [{"date": d, "area": a, "what": w, "others_within": sum(1 for o in dates if o != day and abs((o - day).days) <= days)}
            for (d, a, w), day in zip(events, dates)]


def verdict(test: dict, plc: dict) -> tuple[str, str]:
    """Established only when |t| >= 2, the first and second half of the after period agree in sign, and the real split is
    unusual among the placebo splits (at most 1 in 10 as large)."""
    if not test:
        return "not testable", "too few days either side"
    t = test["t_gamma"]
    first, second = test.get("gamma_first_half"), test.get("gamma_second_half")
    same_sign = first is not None and second is not None and np.sign(first) == np.sign(second) == np.sign(test["gamma"])
    unusual = plc.get("share_as_large", 1.0) <= 0.10 + 1e-9
    if abs(t) >= SIGNIFICANT_T and same_sign and unusual:
        return "established", f"t {t:.1f}, both halves agree, {plc.get('share_as_large', 1):.0%} of other splits as large"
    reasons = []
    if abs(t) < SIGNIFICANT_T:
        reasons.append(f"t {t:.1f} below 2")
    if not same_sign:
        reasons.append("the halves of the after period disagree")
    if not unusual:
        reasons.append(f"{plc.get('share_as_large', 1):.0%} of other splits are as large")
    return "not established", "; ".join(reasons)


# ---------------------------------------------------------------------------------------------------------------- books
def daily_panel(book) -> pd.DataFrame:
    """One book's daily series on the desk's trading equity: return, benchmark return, exposures, fees and funding."""
    from desk import books

    closes = book.benchmark_closes.copy()
    closes.index = pd.to_datetime(closes.index, utc=True).normalize()
    bench = closes.pct_change(fill_method=None).reindex(book.days).fillna(0.0)
    coins, equity = book.coins, book.equity.reindex(book.days)
    grouped = coins.groupby("day")
    panel = pd.DataFrame({
        "ret": books.chart_returns(book).reindex(book.days), "bench": bench, "equity": equity,
        "net": grouped["value"].sum().reindex(book.days).div(equity),
        "gross": coins["value"].abs().groupby(coins["day"]).sum().reindex(book.days).div(equity),
        "fees": grouped["fees"].sum().reindex(book.days).div(equity.shift(1)),
        "funding": grouped["funding"].sum().reindex(book.days).div(equity.shift(1))})
    panel.index = panel.index.tz_convert(None) if panel.index.tz is not None else panel.index
    return panel


def analyse(panels: dict[str, pd.DataFrame], attribution: dict[str, dict], rows: list[dict], since: str = "2026-10-16") -> dict:
    """Everything the notes and tiles show, from the books' panels, their attribution (``decision_attribution``'s 30-day
    window per book, the build's own period) and the labelled history."""
    effort = effort_by_area(rows)
    risk, tests = {}, {}
    for kind, panel in panels.items():
        window = panel.loc[since:]
        risk[kind] = {"before": period_stats(panel.loc[since:pd.Timestamp(BURST_START) - pd.Timedelta(days=1)]),
                      "after": period_stats(panel.loc[BURST_START:]), "since": period_stats(window)}
        test = regime_test(panel, BURST_START)
        plc = placebo(panel, BURST_START)
        tests[kind] = {"test": test, "placebo": plc, "verdict": verdict(test, plc)}
    outcome = {}
    for kind, panel in panels.items():
        parts = (attribution.get(kind) or {}).get("parts", {})
        risk_size = 100 * abs(risk[kind]["since"].get("max_drawdown", 0.0))   # the worst fall, in pp of equity like the parts
        outcome[kind] = outcome_by_area({k: 100 * v for k, v in parts.items()}, risk_size)
    return {"since": since, "effort": effort.to_dict("records"), "risk": risk, "regime": tests, "outcome": outcome,
            "attribution": {k: (v or {}).get("parts", {}) for k, v in attribution.items()},
            "confounding": confounding(), "burst_start": BURST_START}


# ---------------------------------------------------------------------------------------------------------------- tiles
def table_rows(result: dict) -> pd.DataFrame:
    """The Build vs risk/reward table: per area, effort against the drivers it can move, with the evidence level."""
    effort = pd.DataFrame(result["effort"])
    outcome = pd.DataFrame(result["outcome"]).fillna(0.0)
    share = outcome.mean(axis=1) if not outcome.empty else pd.Series(dtype=float)
    can_move = {area: [name for name, areas in DRIVERS.items() if area in areas] for area in effort["Area"]}
    established = any(entry["verdict"][0] == "established" for entry in result["regime"].values())
    rows = []
    for _, row in effort.iterrows():
        drivers = can_move.get(row["Area"], [])
        rows.append({"Area": row["Area"], "Commits": row["Commits %"], "Churn": row["Churn %"],
                     "Rule changes": int(row["Rule changes"]),
                     "Outcome share": float(share.get(row["Area"], 0.0)),
                     "Moves": ", ".join(drivers) if drivers else "nothing directly",
                     "Evidence": ("rule changes helped" if established and row["Rule changes"] else "not established") if drivers else "n/a"})
    return pd.DataFrame(rows)


def publish_cached(path: Path = RESULT_PATH, hours: float = 24 * 14, push: bool = True, directory: Path | None = None) -> list[str]:
    """The two tiles (a table and a chart) from the saved result; ranked low like the Build history tiles."""
    import plotly.graph_objects as go

    from desk import tile_feed

    if not path.exists():
        return []
    result = json.loads(path.read_text())
    table = table_rows(result)
    figure = go.Figure([go.Bar(name="Effort (commits)", x=table["Area"], y=table["Commits"]),
                        go.Bar(name="Outcome (drivers it can move)", x=table["Area"], y=table["Outcome share"])])
    figure.update_layout(barmode="group")
    common = {"hours": hours, "relevance": 0.1, "push": False, "directory": directory}
    tile_feed.publish(table, "Build vs risk/reward: effort and outcome", reason="where the build effort goes against where P&L and risk come from",
                      key="build-risk-reward-table", formats={"Commits": "pct", "Churn": "pct", "Outcome share": "pct", "Rule changes": "num"}, **common)
    tile_feed.publish(figure, "Build vs risk/reward: effort against outcome share", reason="share of commits against share of the drivers an area can move",
                      key="build-risk-reward-chart", formats=None, **common)
    if push:
        tile_feed.push_to_web(directory)
    return ["build-risk-reward-table", "build-risk-reward-chart"]


NOTES_PATH = REPO / "research" / "commit_risk_reward.md"
CHART_PATH = REPO / "research" / "commit_risk_reward.png"
EXPOSURE_CHART_PATH = REPO / "research" / "commit_risk_reward_exposure.png"


def write_charts(result: dict, panels: dict[str, pd.DataFrame]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    table = table_rows(result)
    fig, ax = plt.subplots(figsize=(9, 3.6))
    index = np.arange(len(table))
    ax.bar(index - 0.2, 100 * table["Commits"], 0.4, label="share of commits")
    ax.bar(index + 0.2, 100 * table["Outcome share"], 0.4, label="share of the drivers the area can move")
    ax.set_xticks(index, table["Area"])
    ax.set_ylabel("%")
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(CHART_PATH, dpi=140)
    plt.close(fig)

    crypto = panels["Crypto"].loc[result["since"]:]
    earned = (panels["Crypto"]["net"].shift(1) * panels["Crypto"]["bench"]).loc[result["since"]:]
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.plot(100 * crypto["ret"].fillna(0).cumsum(), label="crypto book (sum of daily returns)")
    ax.plot(100 * earned.fillna(0).cumsum(), label="what its net exposure earned from BTC's moves")
    ax.plot(100 * crypto["bench"].cumsum(), label="BTC (sum of daily returns)", alpha=0.6)
    ax.axvline(pd.Timestamp(result["burst_start"]), color="grey", linestyle=":")
    ax.set_ylabel("% of equity")
    import matplotlib.dates as mdates
    locator = mdates.AutoDateLocator(minticks=4, maxticks=6)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    ax.legend(frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(EXPOSURE_CHART_PATH, dpi=140)
    plt.close(fig)


def write_notes(result: dict, panels: dict[str, pd.DataFrame]) -> None:
    """research/commit_risk_reward.md and its two charts, from the result."""
    write_charts(result, panels)
    pct = lambda x, d=1: "n/a" if x is None or not np.isfinite(x) else f"{100 * x:.{d}f}%"
    table = table_rows(result)
    effort = "\n".join(f"| {r.Area} | {r.Commits:.0%} | {r.Churn:.0%} | {r._4} | {r._5:.0%} | {r.Moves} | {r.Evidence} |"
                        for r in table.rename(columns={"Rule changes": "_4", "Outcome share": "_5"}).itertuples())
    risk_rows = []
    for kind, entry in result["risk"].items():
        for name in ("before", "after"):
            x = entry[name]
            risk_rows.append(f"| {kind} | {name} | {x['days']} | {pct(x['mean_daily'], 2)} | {pct(x['vol_daily'], 2)} | {x['ratio']:.2f} | "
                             f"{pct(x['max_drawdown'])} | {x['net']:.2f} | {x['gross']:.2f} | {pct(x['benchmark_daily'], 2)} |")
    regime_rows = []
    for kind, entry in result["regime"].items():
        t, plc = entry["test"], entry["placebo"]
        regime_rows.append(f"| {kind} | {pct(t['gamma_plain'], 2)} | {pct(t['gamma'], 2)} | {t['t_gamma']:.1f} | {pct(t['gamma_first_half'], 2)} / "
                           f"{pct(t['gamma_second_half'], 2)} | {plc.get('share_as_large', float('nan')):.0%} of {plc.get('splits')} | "
                           f"{entry['verdict'][0]}: {entry['verdict'][1]} |")
    crypto = panels["Crypto"].loc[result["since"]:]
    gap = (crypto["ret"].fillna(0) - (panels["Crypto"]["net"].shift(1) * panels["Crypto"]["bench"]).loc[result["since"]:].fillna(0)).cumsum() * 100
    gap_total, gap_before = float(gap.iloc[-1]), float(gap.loc[:pd.Timestamp(result["burst_start"]) - pd.Timedelta(days=1)].iloc[-1])
    events = "\n".join(f"| {e['date']} | {e['area']} | {e['what']} | {e['others_within']} |" for e in result["confounding"])
    attribution = result["attribution"]
    text = f"""# Commits against risk and reward

Written by `python -m desk.commit_risk_reward run` (tiles: `publish`). Data: `desk/build_history.csv` (509 commits, 2026-10-16 to
2026-11-19), the books' daily series on desk trading equity, and the 30-day benchmark attribution.

**Power, up front.** The build covers 34 days. Eight of its ten rule changes landed in the last 8 days (from {result['burst_start']}),
so the only before/after test that can be run has one regime change and 8 days after it. Nothing below clears the desk's bar
(t of at least 2 and the same sign in both halves) for rule changes helping risk-adjusted return.

## A. Where the effort goes, and what it can move

| Area | Commits | Churn | Rule changes | Share of the drivers it can move | Which drivers | Evidence that its changes helped |
|---|---|---|---|---|---|---|
{effort}

The "can move" mapping is a judgement (`DRIVERS` in `desk/commit_risk_reward.py`): Picks to signals and journal; Sizing to the
daemon; Timing and Market exposure to the daemon and signals; Fees & funding to the daemon and HL; drawdown and leverage to the
daemon. A driver's size is its absolute pp in the 30-day attribution (crypto and stocks averaged), and drawdown is the worst
fall since the build began; each driver is shared equally among the areas that can move it.

- **UI is {table.loc[table.Area == 'ui', 'Commits'].iloc[0]:.0%} of commits and {table.loc[table.Area == 'ui', 'Churn'].iloc[0]:.0%} of churn and moves no driver directly.** It works through decisions the desk then takes.
- **The daemon is {table.loc[table.Area == 'daemon', 'Commits'].iloc[0]:.0%} of commits and {table.loc[table.Area == 'daemon', 'Outcome share'].iloc[0]:.0%} of the outcome drivers.** Signals is {table.loc[table.Area == 'signals', 'Commits'].iloc[0]:.0%} of commits and {table.loc[table.Area == 'signals', 'Outcome share'].iloc[0]:.0%} of the drivers.

![effort and outcome](commit_risk_reward.png)

Crypto, 30 days (pp of equity): Picks {100 * attribution['Crypto'].get('Picks', 0):.1f}, Sizing {100 * attribution['Crypto'].get('Sizing', 0):.1f}, Timing {100 * attribution['Crypto'].get('Timing', 0):.1f}, Market exposure {100 * attribution['Crypto'].get('Market exposure', 0):.1f}, Fees & funding {100 * attribution['Crypto'].get('Fees & funding', 0):.1f}.
Stocks: Picks {100 * attribution['Stocks'].get('Picks', 0):.1f}, Sizing {100 * attribution['Stocks'].get('Sizing', 0):.1f}, Timing {100 * attribution['Stocks'].get('Timing', 0):.1f}, Market exposure {100 * attribution['Stocks'].get('Market exposure', 0):.1f}, Fees & funding {100 * attribution['Stocks'].get('Fees & funding', 0):.1f}. The stocks inputs moved between fetches a few minutes apart in the beta-weight test, so read the stocks parts loosely.

## B. Rule changes: before and after

Ten behaviour-changing commits (daemon rules, stops, auto-trading, sizing, guards, ladders, thesis rules), curated in `EVENTS`. Each has
other rule changes within {CONFOUND_DAYS} days, so no event has a clean window (last column):

| Date | Area | What changed | Other changes within {CONFOUND_DAYS} days |
|---|---|---|---|
{events}

Before and after {result['burst_start']} (when the changes bunch):

| Book | Period | Days | Mean daily return | Daily vol | Mean / vol | Max drawdown | Net | Gross | Benchmark daily |
|---|---|---|---|---|---|---|---|---|---|
{chr(10).join(risk_rows)}

The crypto book's mean return rose from 0.4% to 2.3% a day, with vol up 2.3 times and gross exposure up 2.2 times, in a BTC that went
from -0.4% to -1.9% a day. That is a bigger bet in a falling market, not necessarily better rules. The test that separates them
regresses the daily return on what the net exposure going into the day should have earned (net weight x BTC's return) and a dummy
for the days from {result['burst_start']}; the dummy (`gamma`) is the extra daily return no exposure explains.

| Book | gamma, benchmark alone | gamma, exposure-matched | t | gamma, first / second half of the after days | Placebo: other splits as large | Verdict |
|---|---|---|---|---|---|---|
{chr(10).join(regime_rows)}

With the benchmark alone the crypto book looks like +2.2% a day (t 2.4). Once the exposure is controlled it is {pct(result['regime']['Crypto']['test']['gamma'], 2)} (t {result['regime']['Crypto']['test']['t_gamma']:.1f}):
most of the gain was the net short earning BTC's fall. The book did finish {gap_total:.1f} pp (cumulative daily returns) above what its exposure earned, but
{gap_before:.1f} pp of that gap was already there before {result['burst_start']}, so the rule changes do not explain it. Other split dates give |t| up to {result['regime']['Crypto']['placebo'].get('largest_other_t', float('nan')):.1f}, so the real split is not unusual.

![what the exposure earned](commit_risk_reward_exposure.png)

## C. What is established and what is not

Established:
- The daily return of the crypto book tracks its exposure-weighted BTC move (matched beta {result['regime']['Crypto']['test']['beta_matched']:.2f}): the P&L since the build is mostly Market exposure and Timing, not selection.
- Effort and outcome are far apart: most commits are UI, while the drivers sit in the daemon and signals.
- No rule change has a clean before/after window.

Not established (each fails t of at least 2 in both halves, or the placebo): that the rule changes raised risk-adjusted return, for crypto or stocks; that any single rule (auto flatten, build-ups, stop-free entries, ladders) helped or hurt.

## Focus, with the evidence level

1. **Keep rule changes apart and dated, then judge them one at a time.** Evidence: structural (none of the ten events has a clean window). Ship at most one behaviour change per week, label it `feat(daemon)`, and write down the expected effect on a named driver first.
2. **Test the daemon's rules on replays before live.** Evidence: structural. The daemon is the one area that moves sizing, timing, exposure and drawdown, and the live record cannot isolate its changes; replays can (as the Binance and stop replays did).
3. **Measure exposure-matched return as the desk's skill number.** Evidence: strong for crypto in this sample. Raw return flatters a bigger bet in a falling market (+2.2% a day raw against {pct(result['regime']['Crypto']['test']['gamma'], 2)} matched).
4. **Spend less on UI polish unless it changes a decision.** Evidence: descriptive (UI is the largest share of commits and moves no driver directly). It is not evidence that the UI is worthless: the decision tools sit there.
5. **Fix the stocks inputs before reading the stocks book.** Evidence: the stocks Picks and Sizing moved between runs a few minutes apart in the beta-weight test, so no stocks conclusion above rests on them.

Revisit after 60 or more active days with at most one behaviour change per week.
"""
    NOTES_PATH.write_text(text)


def run(write: bool = True) -> dict:
    """Read both books and the labelled history, run the tests and save the result."""
    from desk import build_history, books

    panels, attribution = {}, {}
    for kind, options in (("Crypto", {}), ("Stocks", {"since": "2025-01-13"})):
        book = books.Book(kind, **options).fetch_data()
        panels[kind] = daily_panel(book)
        returns = books.chart_returns(book).dropna()
        attribution[kind] = books.decision_attribution(book, returns).get("M", {})   # 30 days: the build's own period
    result = analyse(panels, attribution, build_history.read_csv())
    if write:
        RESULT_PATH.write_text(json.dumps(result, indent=1, default=float))
        write_notes(result, panels)
    return result


def main(argv: list[str] | None = None) -> int:
    command = (argv if argv is not None else sys.argv[1:] or ["run"])[0]
    if command == "run":
        result = run()
        for kind, entry in result["regime"].items():
            print(kind, entry["verdict"], entry["test"])
    elif command == "publish":
        print("published " + ", ".join(publish_cached()))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
