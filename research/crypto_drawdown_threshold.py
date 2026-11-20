"""Write research/crypto_drawdown_threshold.md and its chart from desk/crypto_drawdown_threshold.json.

    python research/crypto_drawdown_threshold.py        (after python -m desk.drawdown_monitor fit)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from desk.drawdown_monitor import FIT_FILE, HORIZONS, PERCENTILES  # noqa: E402


def pct(x, digits=1):
    return f"{100 * x:.{digits}f}%"


def chart(fit: dict, path: Path) -> None:
    base = fit["scenarios"]["sharpe_1"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))
    ax = axes[0]
    for horizon, style in zip(HORIZONS, ("-", "--", ":")):
        table = base[str(horizon)]["bootstrap"]
        ax.plot(PERCENTILES, [100 * table[str(p)] for p in PERCENTILES], style, color="#d95926", label=f"{horizon} days")
        t = base[str(horizon)]["student_t"]
        ax.plot(PERCENTILES, [100 * t[str(p)] for p in PERCENTILES], style, color="#6b7280", alpha=0.8)
    for name, color in (("watch", "#2563eb"), ("re-evaluate", "#dc2626")):
        ax.axhline(100 * fit["levels"][name]["drawdown"], color=color, lw=1, ls="-.")
        ax.text(PERCENTILES[0], 100 * fit["levels"][name]["drawdown"] + 0.3, f"{name} {pct(fit['levels'][name]['drawdown'])}", color=color, fontsize=8)
    ax.set_xlabel("percentile of the period's max drawdown")
    ax.set_ylabel("max drawdown, % of the book")
    ax.set_title("Orange: block bootstrap · grey: Student-t (Sharpe 1)", fontsize=9)
    ax.legend(fontsize=8)
    ax = axes[1]
    labels = list(fit["power"])
    x = range(len(labels))
    for k, (name, color) in enumerate((("watch", "#2563eb"), ("re-evaluate", "#dc2626"))):
        ax.bar([i + (k - 0.5) * 0.38 for i in x], [100 * fit["power"][lab]["quarter"][name] for lab in labels], 0.38, color=color, label=name)
    ax.set_xticks(list(x))
    ax.set_xticklabels([lab.replace(" and ", "\n+ ").replace("model right", "model\nright") for lab in labels], fontsize=7)
    ax.set_ylabel("% of quarters the level is crossed")
    ax.set_title("False alarms (model right) vs detection (degraded)", fontsize=9)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)


def write(fit: dict) -> Path:
    base = fit["scenarios"]["sharpe_1"]
    zero, two = fit["scenarios"]["sharpe_0"], fit["scenarios"]["sharpe_2"]
    watch, reeval = fit["levels"]["watch"], fit["levels"]["re-evaluate"]
    mc = fit.get("managed_check") or {}
    lines = [
        "# Crypto drawdown: when should a breach make us re-evaluate the models?", "",
        f"Fitted {fit['as_of'][:10]}. Alert only, nothing trades on it. This is a model-health signal for the crypto book; it is "
        "not the investor's tolerance (`desk/fiduciary.json`), which is the desk's drawdown the investor accepts.", "",
        "## What the data can and cannot support", "",
        f"- The crypto book has **{mc.get('days', '~35')} managed days**. That is far too few to measure its own drawdown distribution, "
        "so the shape comes from a **proxy**, not from the book.",
        f"- The proxy replays **today's crypto positions** (net {pct(fit['net'], 0)}, gross {pct(fit['gross'], 0)} of the HL wallets' equity, "
        f"{len(fit['weights'])} coins) over **{fit['days']} days** of the HL candle panel ({fit['first_day']} to {fit['last_day']}). "
        "It leaves out funding, fees and trading in and out. Today's short book was picked with hindsight, so its replayed drift is "
        "**not used**: drift is assumed, and only volatility, tails and serial dependence come from the replay.",
        f"- Coins with under 90 days of history are replayed by the median coin: {', '.join(fit['stand_in']) or 'none'}.",
        f"- **Assumed edge: an annualised Sharpe of {fit['assumed_sharpe']:g}** for a book whose model is right. This is an assumption "
        "for the user to confirm, not a measured number; the levels under Sharpe 0 and 2 are in the sensitivity table.", "",
        "## Method", "",
        f"- Max drawdown over 30, 60 and 90 days, two ways: a **stationary block bootstrap** of the replayed returns (mean block "
        f"{fit['block']} days, from the autocorrelation of the returns and of their absolute values) and a **Student-t** model "
        f"(df {fit['t_df']:.1f}, scaled to the current EWMA volatility, independent days).",
        "- Percentile confidence intervals come from resampling the replay itself (the sampling uncertainty of the proxy, not of the "
        "positions or the models).",
        f"- The two levels are percentiles of the **one-quarter ({fit['quarter']} days) max drawdown** under the assumed edge: "
        f"**watch** is crossed in {pct(watch['false_alarm_per_quarter'], 0)} of quarters by a book whose model is right, "
        f"**re-evaluate** in {pct(reeval['false_alarm_per_quarter'], 0)}.", "",
        "## Max drawdown distribution (Sharpe 1)", "",
        "| Horizon | Model | " + " | ".join(f"p{p}" for p in PERCENTILES) + " |", "|---|---|" + "---|" * len(PERCENTILES)]
    for horizon in HORIZONS:
        for label, key in (("block bootstrap", "bootstrap"), ("Student-t", "student_t")):
            row = base[str(horizon)][key]
            lines.append(f"| {horizon} days | {label} | " + " | ".join(pct(row[str(p)]) for p in PERCENTILES) + " |")
        ci = base[str(horizon)]["bootstrap_ci"]
        lines.append(f"| {horizon} days | 90% interval (bootstrap) | " + " | ".join(f"{pct(ci[str(p)][0], 0)} to {pct(ci[str(p)][1], 0)}" for p in PERCENTILES) + " |")
    lines += ["", "### Sensitivity to the assumed edge (60 days, block bootstrap)", "",
              "| Assumed Sharpe | " + " | ".join(f"p{p}" for p in PERCENTILES) + " |", "|---|" + "---|" * len(PERCENTILES)]
    for label, table in (("0 (no edge)", zero), ("1 (assumed)", base), ("2", two)):
        row = table["60"]["bootstrap"]
        lines.append(f"| {label} | " + " | ".join(pct(row[str(p)]) for p in PERCENTILES) + " |")
    lines += ["", "## The two levels", "",
              "| Level | Drawdown from the high | In book volatilities (sigma x sqrt(63)) | 90% interval of the proxy | Student-t level | Crossed in | ",
              "|---|---|---|---|---|---|",
              f"| watch | {pct(watch['drawdown'])} | {watch['sigma_units']:.2f} | {pct(watch['ci90'][0])} to {pct(watch['ci90'][1])} | "
              f"{pct(fit['levels_student_t']['watch'])} | {pct(watch['false_alarm_per_quarter'], 0)} of quarters |",
              f"| re-evaluate | {pct(reeval['drawdown'])} | {reeval['sigma_units']:.2f} | {pct(reeval['ci90'][0])} to {pct(reeval['ci90'][1])} | "
              f"{pct(fit['levels_student_t']['re-evaluate'])} | {pct(reeval['false_alarm_per_quarter'], 0)} of quarters |", "",
              f"**Best-supported range for the re-evaluate level: {pct(reeval['ci90'][0], 0)} to {pct(reeval['ci90'][1], 0)}** (the bootstrap's 90% "
              f"interval; the Student-t model gives {pct(fit['levels_student_t']['re-evaluate'], 0)}); the monitor uses the bootstrap value. Both levels scale with the "
              "book's volatility: they are stored in units of its daily volatility times sqrt(63), and the monitor recomputes that "
              f"volatility from today's positions on every run (today {pct(fit['sigma_daily'], 2)} a day), so adding exposure widens them "
              "and cutting exposure tightens them.", "",
              "## False alarms and detection", "",
              "| Book | Crossed watch in a quarter | Crossed re-evaluate in a quarter | Crossed watch in two quarters | Crossed re-evaluate in two quarters |",
              "|---|---|---|---|---|"]
    for label, row in fit["power"].items():
        lines.append(f"| {label} | {pct(row['quarter']['watch'], 0)} | {pct(row['quarter']['re-evaluate'], 0)} | "
                     f"{pct(row['two quarters']['watch'], 0)} | {pct(row['two quarters']['re-evaluate'], 0)} |")
    right, none = fit["power"]["model right"], fit["power"]["no edge"]
    lines += ["", "**Detection is weak.** A drawdown monitor is a poor test of whether the edge is still there: a book with no edge crosses "
              f"the re-evaluate level in {pct(none['quarter']['re-evaluate'], 0)} of quarters, against {pct(right['quarter']['re-evaluate'], 0)} "
              "for one whose model is right. It is better at catching a volatility blow-up (vol +50%) or a negative edge. Treat a crossing "
              "as a prompt to look at the theses, sizing and regime, not as proof that a model failed, and a quiet period as saying "
              "little about the models.", "",
              "![drawdown levels and power](crypto_drawdown_threshold.png)", "",
              "## The crypto book so far", ""]
    if mc:
        lines += [f"- {mc['days']} managed days: realised volatility {pct(mc['book_vol_daily'], 2)} a day against "
                  f"{pct(mc['replay_vol_same_days'], 2)} for today's positions replayed over the same days "
                  f"(correlation {mc['correlation_with_replay']:.2f}): the book carried more risk than today's positions, and the proxy "
                  "is a rough guide to it.",
                  f"- Its max drawdown so far is {pct(mc['book_max_dd'])}, at the {pct(mc.get('book_dd_percentile', float('nan')), 0)} "
                  "percentile of the simulated max drawdown for that many days: shallow, with a mean of "
                  f"{pct(mc['book_mean_daily'], 2)} a day that no sustained edge would deliver."]
    lines += ["", "## Caveats", "",
              "- The proxy replays today's positions, which include a short book chosen in a bear regime; real exposure changes through "
              "the period, funding and fees are left out, and the managed-era book was more volatile than the replay.",
              "- The false-alarm rates assume the model is right at the assumed Sharpe and that days are exchangeable within blocks; "
              "regime changes break that, and a regime change is exactly what a crossing should prompt us to check.",
              "- The drawdown is measured from the book's high-water mark since the managed era began, not a rolling quarter; "
              "the false-alarm rate is per quarter starting at a high, so a book already under water crosses sooner.",
              "- Refit with `python -m desk.drawdown_monitor fit` as the managed history grows; with a year of managed days the "
              "book's own returns can replace the proxy.", ""]
    path = REPO / "research" / "crypto_drawdown_threshold.md"
    path.write_text("\n".join(lines))
    chart(fit, REPO / "research" / "crypto_drawdown_threshold.png")
    return path


if __name__ == "__main__":
    print(write(json.loads(FIT_FILE.read_text())))
