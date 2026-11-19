"""Backfill Hyperliquid history for research: daily candles with volume (main perps, delisted included, and the
HIP-3 equity perps, which count as stocks) and hourly funding for the most liquid perps.

    /Users/admin/anaconda3/envs/py314/bin/python research/backfill_hl_history.py [--days 1100] [--funding 40]

Slow and resumable: HL's rate limit is shared with the trading daemon, so requests are spaced out and every coin is
saved as soon as it is read. Candles go to data/api_cache/signal_panel.csv.gz (the desk's research panel), funding to
the usual api_cache files (both stay out of git).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path[:0] = [str(Path(__file__).resolve().parents[1])]

from desk.hl import signal_research as sr  # noqa: E402
from desk.hl.trade_analytics import post_info  # noqa: E402


def equity_perps() -> list[str]:
    """Every HIP-3 perp ("dex:COIN"): the deployer dexes' markets (stocks, indices, commodities)."""
    names = []
    for dex in post_info({"type": "perpDexs"}) or []:
        if not dex:
            continue
        meta = post_info({"type": "meta", "dex": dex["name"]})
        names += [asset["name"] for asset in meta.get("universe", []) if not asset.get("isDelisted")]
        time.sleep(1)
    return names


def refresh_hip3(panel: dict, days: int, spacing: float, log=print) -> None:
    today = pd.Timestamp.now(tz="UTC").normalize()
    for index, coin in enumerate(equity_perps()):
        saved = panel.get(coin)
        if saved is not None and len(saved) and saved.index[-1] >= today - pd.Timedelta(days=1):
            continue
        start = saved.index[-1] if saved is not None and len(saved) else today - pd.Timedelta(days=days)
        try:
            fresh = sr._candles(coin, int(start.timestamp() * 1000))
        except Exception as exc:
            log(f"  {coin}: not read ({type(exc).__name__})")
            time.sleep(spacing)
            continue
        fresh = fresh[fresh.index < today]
        if len(fresh):
            merged = fresh if saved is None or saved.empty else pd.concat([saved[saved.index < fresh.index[0]], fresh])
            panel[coin] = merged[~merged.index.duplicated(keep="last")].sort_index()
            sr.save_panel(panel)
        time.sleep(spacing)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=1100)
    parser.add_argument("--funding", type=int, default=40, help="hourly funding for this many most liquid perps (0 = none)")
    parser.add_argument("--spacing", type=float, default=3.0)
    args = parser.parse_args()
    panel = sr.refresh_panel(days=args.days, spacing=args.spacing)
    print(f"main perps: {len(panel)} coins")
    refresh_hip3(panel, args.days, args.spacing)
    print(f"with HIP-3: {len(panel)} coins")
    if args.funding:
        volume = {coin: (frame["volume"] * frame["close"]).tail(30).median() for coin, frame in panel.items() if ":" not in coin and len(frame)}
        top = sorted(volume, key=volume.get, reverse=True)[:args.funding]
        sr.backfill_funding(top, days=args.days, pause=args.spacing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
