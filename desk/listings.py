"""Where an HL market's longer price history lives: the listing it tracks.

A market that is new on Hyperliquid (an equity perp listed weeks ago) has too few candles for a beta, but the share or
token it tracks has years of history. ``LISTINGS`` is the reviewable map from the market's bare name to the Yahoo
ticker of its underlying. Every entry was checked against the market's own candles over the days they share (daily
return correlation and price ratio, below), so a ticker that only looks right is not added. ``spliced`` puts the
listing's history before the market's first candle, scaled to meet it, and keeps the market's own prices after.
A Yahoo failure is reported (a chip) and the market's own history is used.

Checked 2026-11-19 (HL candles vs Yahoo, daily returns): PAXG vs PAXG-USD corr 0.999 over 602 days; ICE vs ICE corr
0.94 over 15; RELIANCE vs RELIANCE.NS corr 0.81 over 16 (NSE closes hours before the US day, so the link is looser;
price ratio is the INR/USD rate). xyz:JIO is Jio Platforms (about $14.8 a share, JPL.NS), listed on 2026-10-28:
JIOFIN.NS is a different company (corr -0.10) and JPL.NS has only days of history, so JIO is not mapped: it has no
long history to borrow.
"""

from __future__ import annotations

import pandas as pd

from desk import degraded

# HL market name (without the dex prefix) -> Yahoo ticker of the underlying.
LISTINGS = {
    "PAXG": "PAXG-USD",        # tokenised gold; the spot token and the perp trade the same price
    "ICE": "ICE",              # Intercontinental Exchange
    "RELIANCE": "RELIANCE.NS", # Reliance Industries (NSE, INR; returns only)
}

SHORT_HISTORY_DAYS = 120   # fewer candles than a beta wants (90 days of returns) and a listing is worth fetching


def base_name(market: str) -> str:
    return str(market).split(":")[-1].upper()


def listing_for(market: str) -> str | None:
    return LISTINGS.get(base_name(market))


def spliced(market: str, own: pd.Series, days: int) -> pd.Series:
    """The market's closes with its underlying's history before the first candle. Unmapped, or a listing that cannot be
    read, leaves the market's own closes (a failure is reported)."""
    ticker = listing_for(market)
    if not ticker or own is None or own.empty:
        return own
    try:
        from desk.live_risk import yahoo_stock_closes

        listed = yahoo_stock_closes(ticker, days)
    except Exception as exc:
        degraded.report(f"{base_name(market)} price history", exc)
        return own
    own = own[~own.index.duplicated(keep="last")].sort_index()
    own.index = pd.DatetimeIndex(own.index).normalize()
    listed = listed[~listed.index.duplicated(keep="last")].sort_index()
    listed.index = pd.DatetimeIndex(listed.index).normalize()
    start = own.index[0]
    before = listed[listed.index < start]
    anchor = listed[listed.index <= start]
    if before.empty or anchor.empty or not float(anchor.iloc[-1]):
        return own
    scale = float(own.iloc[0]) / float(anchor.iloc[-1])
    return pd.concat([before * scale, own])
