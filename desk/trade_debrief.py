"""Trade debrief journal: for a trade, what the thesis was, how the market went, how the execution went.

    PYTHONPATH=. python -m desk.trade_debrief --coin CASHCAT            # one trade, printed
    PYTHONPATH=. python -m desk.trade_debrief --all --out data/debriefs  # every journal trade, one markdown file each

* Thesis: the journal's written ``thesis`` (``journal.record_entry(..., thesis="...")``), else the strategy tag, its
  signals and the desk rule that fired (the event log); an unwritten thesis is said to be unwritten.
* Market: the price at entry and now (or at the close), the best and worst the trade saw (hourly candles), the coin
  against BTC over the same hours, and the funding paid or earned.
* Execution: entry fills against the price when the order started, maker or taker, fees, how long the fills took, how
  the position ended (take-profit, stop or a close) and what the trade made against the best it showed.

Every figure is from HL's fills, funding and candles; nothing is estimated except where the text says so.
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

import numpy as np
import pandas as pd

from desk import journal

REPO = Path(__file__).resolve().parents[1]


def _ms(text: str) -> int:
    return int(pd.Timestamp(text, tz="UTC").timestamp() * 1000)


def _clock(ms: float) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert("America/New_York").strftime("%b %d %-I:%M %p")


def _candles(coin: str, start_ms: int, end_ms: int, interval: str) -> pd.DataFrame:
    from desk.hl.trade_analytics import post_info

    request = {"type": "candleSnapshot", "req": {"coin": coin.split(":")[-1], "interval": interval, "startTime": int(start_ms),
                                                 "endTime": int(end_ms)}}
    if ":" in coin:
        request["dex"] = coin.split(":")[0]
    rows = post_info(request) or []
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    for column in ("o", "h", "l", "c", "v"):
        frame[column] = frame[column].astype(float)
    frame["t"] = frame["t"].astype("int64")
    return frame


def trade_fills(fills: list[dict], coin: str, side: str, opened_ms: int, closed_ms: int | None) -> tuple[list[dict], list[dict]]:
    """(entry fills, exit fills) of one trade in one coin: entries open or add in the trade's direction, exits reduce it."""
    opening = "Open Short" if side == "SHORT" else "Open Long"
    closing = "Close Short" if side == "SHORT" else "Close Long"
    own = [f for f in fills if f.get("coin") == coin and int(f["time"]) >= opened_ms - 60_000
           and (closed_ms is None or int(f["time"]) <= closed_ms + 60_000)]
    return ([f for f in own if str(f.get("dir", "")) == opening], [f for f in own if str(f.get("dir", "")).startswith(closing)])


def execution(entries: list[dict], exits: list[dict], arrival: float | None, side: str) -> dict:
    """The execution numbers: average entry and exit, slippage against the arrival price, share filled as maker, fees and
    how long the entry took."""
    def average(rows):
        size = sum(abs(float(r["sz"])) for r in rows)
        return sum(abs(float(r["sz"])) * float(r["px"]) for r in rows) / size if size else float("nan")

    entry_px, exit_px = average(entries), average(exits)
    sign = 1.0 if side == "LONG" else -1.0   # adverse = paying up for a long, selling lower for a short
    slippage = sign * (entry_px / arrival - 1) if arrival and np.isfinite(entry_px) else float("nan")
    notional = sum(abs(float(r["sz"])) * float(r["px"]) for r in entries) or float("nan")
    maker = sum(abs(float(r["sz"])) * float(r["px"]) for r in entries if not r.get("crossed")) / notional if np.isfinite(notional) else float("nan")
    times = [int(r["time"]) for r in entries]
    return {"entry_px": entry_px, "exit_px": exit_px, "slippage": slippage, "maker_share": maker, "notional": notional,
            "fees": sum(float(r.get("fee") or 0) for r in entries + exits),
            "entry_seconds": (max(times) - min(times)) / 1000 if times else float("nan"),
            "closed_pnl": sum(float(r.get("closedPnl") or 0) for r in exits)}


def market(coin_candles: pd.DataFrame, btc_candles: pd.DataFrame, side: str, entry_px: float) -> dict:
    """The price path during the trade: best and worst excursion for the position's side, the move, and against BTC."""
    if coin_candles.empty or not np.isfinite(entry_px):
        return {}
    high, low, last = float(coin_candles["h"].max()), float(coin_candles["l"].min()), float(coin_candles["c"].iloc[-1])
    sign = 1.0 if side == "LONG" else -1.0
    best = sign * ((high if side == "LONG" else low) / entry_px - 1)
    worst = sign * ((low if side == "LONG" else high) / entry_px - 1)
    move = sign * (last / entry_px - 1)
    btc = float(btc_candles["c"].iloc[-1] / btc_candles["o"].iloc[0] - 1) if not btc_candles.empty else float("nan")
    coin_move = float(coin_candles["c"].iloc[-1] / coin_candles["o"].iloc[0] - 1)
    return {"last": last, "high": high, "low": low, "best": best, "worst": worst, "move": move,
            "coin_move": coin_move, "btc_move": btc, "vs_btc": coin_move - btc if np.isfinite(btc) else float("nan")}


def thesis_text(entry: dict, events: list[dict]) -> tuple[str, bool]:
    """(the thesis, whether it was written): the journal's, else what the desk recorded about the trade."""
    if entry.get("thesis"):
        return entry["thesis"], True
    parts = [f"{entry['side']} {entry['coin']}, tagged {entry['strategy']}."]
    if entry.get("signals"):
        parts.append("Signals at entry: " + ", ".join(f"{k} {v}" for k, v in entry["signals"].items()) + ".")
    opened_ms = _ms(entry["opened"])
    near = [e for e in events if str(e.get("kind", "")).startswith("auto") and entry["coin"] in (e.get("coins") or [])
            and abs(float(e.get("at", 0)) * 1000 - opened_ms) < 6 * 3600_000]
    if near:
        parts.append("Desk rule: " + str(near[0].get("note", ""))[:240])
    parts.append("No written thesis was recorded for this trade (record one with journal.record_entry(thesis=...)).")
    return " ".join(parts), False


def pct(value: float, digits: int = 1) -> str:
    return "n/a" if not np.isfinite(value) else f"{100 * value:.{digits}f}%"


def markdown(entry: dict, thesis: tuple[str, bool], mkt: dict, ex: dict, ended: str, hours: float) -> str:
    side, coin = entry["side"], entry["coin"]
    lines = [f"# {coin} {side}, {_clock(_ms(entry['opened']) )} ({ended}, {hours:.1f} h)", "",
             "## Thesis", thesis[0], "", "## How the market went"]
    if mkt:
        lines += [f"- Entry {ex['entry_px']:.6g}; now/last {mkt['last']:.6g}; the position's move {pct(mkt['move'])}.",
                  f"- Best the trade showed {pct(mkt['best'])}, worst {pct(mkt['worst'])} (hourly highs and lows).",
                  f"- {coin} moved {pct(mkt['coin_move'])} over the same hours against BTC {pct(mkt['btc_move'])}: "
                  f"{pct(mkt['vs_btc'])} {'ahead of' if mkt['vs_btc'] > 0 else 'behind'} BTC."]
    else:
        lines.append("- No candles for the window.")
    lines += ["", "## How execution went",
              f"- Entry average {ex['entry_px']:.6g} on ${ex['notional']:,.0f}; {pct(ex['slippage'], 2)} against the price when the order started "
              f"(positive: cost).",
              f"- {pct(ex['maker_share'], 0)} filled as maker; entry took {ex['entry_seconds']:.0f} s; fees ${ex['fees']:.2f}."]
    if np.isfinite(ex["exit_px"]):
        lines.append(f"- Exit average {ex['exit_px']:.6g}; realized ${ex['closed_pnl']:+,.2f} before fees.")
    if mkt and np.isfinite(ex["entry_px"]) and mkt["best"] > 0 and mkt["move"] < mkt["best"]:
        lines.append(f"- It gave back {pct(mkt['best'] - mkt['move'])} of the {pct(mkt['best'])} the trade once showed.")
    return "\n".join(lines) + "\n"


def chart(entry: dict, fills: list[dict], path: Path, now_ms: int | None = None) -> Path | None:
    """One picture of the trade: the coin's price from a bit before the entry (15-minute candles) with the entry and exit
    fills marked, the entry price as a line, the best and worst the trade saw shaded, and BTC rebased to the entry."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    now_ms = now_ms or int(time.time() * 1000)
    opened_ms = _ms(entry["opened"])
    closed_ms = _ms(entry["closed"]) if entry.get("closed") else None
    entries, exits = trade_fills(fills, entry["coin"], entry["side"], opened_ms, closed_ms)
    if not entries:
        return None
    start, end = opened_ms - 3 * 3600_000, (closed_ms or now_ms) + 3600_000
    candles, btc = _candles(entry["coin"], start, end, "15m"), _candles("BTC", start, end, "15m")
    if candles.empty:
        return None
    ex = execution(entries, exits, None, entry["side"])
    times = pd.to_datetime(candles["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    good, bad = "#16a34a", "#dc2626"
    figure, axis = plt.subplots(figsize=(9, 4.4))
    axis.plot(times, candles["c"], color="#334155", lw=1.6, label=entry["coin"])
    axis.fill_between(times, candles["l"], candles["h"], color="#94a3b8", alpha=0.25, lw=0)
    axis.axhline(ex["entry_px"], color="#64748b", ls="--", lw=1)
    held = candles[candles["t"] >= opened_ms]
    if len(held):
        best = held["l"].min() if entry["side"] == "SHORT" else held["h"].max()
        worst = held["h"].max() if entry["side"] == "SHORT" else held["l"].min()
        axis.axhline(best, color=good, lw=0.8, ls=":"); axis.axhline(worst, color=bad, lw=0.8, ls=":")
        axis.annotate(f"best {abs(best / ex['entry_px'] - 1):.1%}", (times.iloc[-1], best), color=good, fontsize=8, va="bottom", ha="right")
        axis.annotate(f"worst {abs(worst / ex['entry_px'] - 1):.1%}", (times.iloc[-1], worst), color=bad, fontsize=8, va="top", ha="right")
    for rows, color, marker in ((entries, "#2563eb", "v" if entry["side"] == "SHORT" else "^"), (exits, "#7c3aed", "o")):
        if rows:
            axis.scatter(pd.to_datetime([int(r["time"]) for r in rows], unit="ms", utc=True).tz_convert("America/New_York"),
                         [float(r["px"]) for r in rows], color=color, marker=marker, s=46, zorder=5,
                         label="entry fills" if color == "#2563eb" else "exit fills")
    if not btc.empty:
        twin = axis.twinx()
        base = float(btc[btc["t"] >= opened_ms]["c"].iloc[0]) if (btc["t"] >= opened_ms).any() else float(btc["c"].iloc[0])
        twin.plot(pd.to_datetime(btc["t"], unit="ms", utc=True).dt.tz_convert("America/New_York"), 100 * (btc["c"] / base - 1),
                  color="#f59e0b", lw=1, alpha=0.8, label="BTC (% from entry)")
        twin.set_ylabel("BTC, % from entry", color="#f59e0b")
        twin.spines[["top"]].set_visible(False)
    axis.set_title(f"{entry['coin']} {entry['side']}: entry {ex['entry_px']:.6g}", fontsize=11)
    axis.set_ylabel("Price"); axis.legend(loc="upper left", fontsize=8, frameon=False)
    axis.spines[["top", "right"]].set_visible(False)
    figure.autofmt_xdate(); figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=120)
    plt.close(figure)
    return path


def debrief(entry: dict, fills: list[dict], events: list[dict] | None = None, now_ms: int | None = None) -> str:
    now_ms = now_ms or int(time.time() * 1000)
    opened_ms = _ms(entry["opened"])
    closed_ms = _ms(entry["closed"]) if entry.get("closed") else None
    entries, exits = trade_fills(fills, entry["coin"], entry["side"], opened_ms, closed_ms)
    end_ms = closed_ms or now_ms
    first = min((int(f["time"]) for f in entries), default=opened_ms)
    arrival_candles = _candles(entry["coin"], first - 60_000, first + 60_000, "1m")
    arrival = float(arrival_candles["o"].iloc[0]) if not arrival_candles.empty else None
    ex = execution(entries, exits, arrival, entry["side"])
    coin_candles = _candles(entry["coin"], opened_ms, end_ms, "1h")
    btc_candles = _candles("BTC", opened_ms, end_ms, "1h")
    mkt = market(coin_candles, btc_candles, entry["side"], ex["entry_px"])
    return markdown(entry, thesis_text(entry, events or []), mkt, ex, "closed" if closed_ms else "open",
                    (end_ms - opened_ms) / 3600_000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coin", help="the journal trade's coin (the latest entry)")
    parser.add_argument("--wallet", help="last four characters of the wallet")
    parser.add_argument("--all", action="store_true", help="every journal trade")
    parser.add_argument("--out", type=Path, help="write one markdown file per trade into this folder")
    parser.add_argument("--chart", action="store_true", help="also draw the trade's picture (a PNG next to the markdown, or in charts/)")
    args = parser.parse_args(argv)
    from desk import event_watch
    from desk.accounts import hl_wallet
    from desk.hl.account_analytics import fetch_user_fills

    entries = journal.load()
    if not args.all:
        entries = [e for e in entries if e["coin"].upper() == (args.coin or "").upper()
                   and (not args.wallet or e["wallet"] == args.wallet[-4:].lower())][-1:]
    if not entries:
        print("No journal trade matches.")
        return 1
    events = event_watch.load_events()
    fills_by_wallet: dict[str, list[dict]] = {}
    for entry in entries:
        wallet = hl_wallet(entry["wallet"])
        if wallet not in fills_by_wallet:
            fills_by_wallet[wallet] = fetch_user_fills(wallet)
        text = debrief(entry, fills_by_wallet[wallet], events)
        if args.chart:
            name = f"{entry['opened'][:10]}_{entry['wallet']}_{entry['coin'].replace(':', '-')}.png"
            picture = chart(entry, fills_by_wallet[wallet], (args.out or REPO / "charts") / name)
            print(f"picture: {picture}" if picture else "no picture (no entry fills in HL's history)")
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            name = f"{entry['opened'][:10]}_{entry['wallet']}_{entry['coin'].replace(':', '-')}.md"
            (args.out / name).write_text(text)
            print(f"wrote {args.out / name}")
        else:
            print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
