"""Alt rally watch: a fast warning for the short-alt book, before a one-day squeeze takes a large bite.

The desk is net short alts, so a broad alt rally is the move that hurts it most. Over the desk's daily history
(Jun 2025 to Nov 2026) the 40 most liquid alts rose 5% or more in a day about twice a month; most of those faded
within the week, but three of the five +10% days came within three days of one. So:

* **watch:** the alt basket is up WATCH_MOVE or more over the last 24 hours. An alert to review: tighten, or trim
  the shorts that squeeze hardest.
* **cut:** up CUT_MOVE or more in 24 hours, or a second watch on a later day within 48 hours. An alert that
  recommends cutting gross by CUT_SHARE: that is where the damaging days live.

The stock book (long TradFi) gets the mirror image from HL's S&P 500 perp (xyz:SP500), which trades around the
clock. Over SPY's last three years a -2% day came about five times a year and mostly bounced (+2.3% on average
over the next ten sessions), while the -4% days clustered (three in one week of April 2025). So:

* **stock dip:** the S&P 500 down STOCK_WATCH_MOVE or more in 24 hours: review; red days are when the desk adds.
* **stock sell-off, cut:** down STOCK_CUT_MOVE or more, or a second dip on a later day within 48 hours.

Neither trades: the trading daemon (execution/hl_daemon.py) raises them as alerts, like adverse funding. The
basket is HL's main-DEX perps by 24-hour volume, BTC, ETH, stablecoins and gold left out (desk/market_regime.py),
each move measured from HL's price 24 hours ago, equal weight.
"""

from __future__ import annotations

import math

WATCH_MOVE = 0.05
CUT_MOVE = 0.08
CUT_SHARE = 1 / 3                  # the gross cut the cut alert recommends
ALT_COUNT = 40
REPEAT_WINDOW = (20 * 3600, 48 * 3600)  # a watch this long ago (a previous day, within two) escalates a new one
EXCLUDED = {"BTC", "ETH", "USDC", "USDT", "USDE", "DAI", "PAXG", "XAUT"}
STOCK_INDEX = "xyz:SP500"
STOCK_DEX = "xyz"
STOCK_WATCH_MOVE = -0.02
STOCK_CUT_MOVE = -0.035


def basket_move(meta: dict, contexts: list[dict], count: int = ALT_COUNT) -> tuple[float, int, int]:
    """The alt basket's move over the last 24 hours, and how many of its coins rose, of how many.

    ``meta`` and ``contexts`` are HL's ``metaAndAssetCtxs`` answer for the main DEX.
    """
    rows = []
    for asset, context in zip(meta.get("universe", []), contexts):
        name = str(asset.get("name", ""))
        if asset.get("isDelisted") or name in EXCLUDED or name.startswith("k"):
            continue
        try:
            mark, before = float(context.get("markPx") or 0), float(context.get("prevDayPx") or 0)
            volume = float(context.get("dayNtlVlm") or 0)
        except (TypeError, ValueError):
            continue
        if mark > 0 and before > 0:
            rows.append((volume, max(-0.5, min(1.0, mark / before - 1))))
    top = sorted(rows, reverse=True)[:count]
    if not top:
        return float("nan"), 0, 0
    moves = [move for _, move in top]
    return sum(moves) / len(moves), sum(move > 0 for move in moves), len(moves)


def level(move: float, earlier_watches: list[float], now: float, watch: float = WATCH_MOVE,
          cut: float = CUT_MOVE) -> str:
    """"none", "watch" or "cut" for a 24-hour ``move``, given when earlier watches fired (unix seconds). A negative
    ``watch`` and ``cut`` watch a fall (the stock dip) instead of a rise."""
    sign = -1.0 if watch < 0 else 1.0
    if not math.isfinite(move) or sign * move < sign * watch:
        return "none"
    if sign * move >= sign * cut:
        return "cut"
    low, high = REPEAT_WINDOW
    return "cut" if any(low <= now - when <= high for when in earlier_watches) else "watch"


def alert(level_name: str, move: float, rising: int, count: int, book_move: float | None) -> tuple[str, str] | None:
    """The alert's (title, text): the signal first, then the numbers; None when there is nothing to say."""
    if level_name == "none":
        return None
    book = f"; the book {_pct(book_move)} on it" if book_move is not None and math.isfinite(book_move) else ""
    detail = f"alts {_pct(move)} in 24h ({rising} of {count} up){book}"
    if level_name == "cut":
        return "Alt rally: cut gross", f"{detail}. Cut gross by {CUT_SHARE:.0%}?"
    return "Alt rally", f"{detail}. Review: tighten, or trim the shorts that squeeze hardest."


def index_move(meta: dict, contexts: list[dict], name: str = STOCK_INDEX) -> float:
    """The 24-hour move of one market (``xyz:SP500``) from HL's ``metaAndAssetCtxs`` answer for its DEX."""
    for asset, context in zip(meta.get("universe", []), contexts):
        if asset.get("name") == name:
            try:
                mark, before = float(context.get("markPx") or 0), float(context.get("prevDayPx") or 0)
            except (TypeError, ValueError):
                return float("nan")
            return mark / before - 1 if mark > 0 and before > 0 else float("nan")
    return float("nan")


def ib_change(positions: list[dict], contexts_by_coin: dict[str, dict]) -> float:
    """What the last 24 hours did to IB's stock positions, in USD: each priced by HL's perp on the same stock
    (``xyz:GOOGL``) where HL lists it, else by its own 24-hour change from the Mac's push (``change24h``)."""
    total = 0.0
    for position in positions:
        try:
            quantity = float(position.get("quantity") or 0) * float(position.get("multiplier") or 1)
            mark = float(position.get("mark_price") or 0)
        except (TypeError, ValueError):
            continue
        context = contexts_by_coin.get(f"{STOCK_DEX}:{position.get('symbol')}")
        move = float("nan")
        if context:
            try:
                move = float(context["markPx"]) / float(context["prevDayPx"]) - 1
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                pass
        if not math.isfinite(move):
            try:
                move = float(position.get("change24h"))
            except (TypeError, ValueError):
                continue
        if math.isfinite(move) and move > -1:
            total += quantity * mark * move / (1 + move)   # today's value less yesterday's
    return total


def stock_alert(level_name: str, move: float, book_move: float | None) -> tuple[str, str] | None:
    """The stock dip alert's (title, text), signal first; None when there is nothing to say. ``book_move`` is the
    stock book's 24-hour change (HL stocks and IB) over trading equity (HL and IB)."""
    if level_name == "none":
        return None
    book = (f"; the stock book {_pct(book_move)} of trading equity on it"
            if book_move is not None and math.isfinite(book_move) else "")
    detail = f"S&P 500 {_pct(move)} in 24h{book}"
    if level_name == "cut":
        return "Stock sell-off: cut gross", f"{detail}. Cut the stock longs by {CUT_SHARE:.0%}?"
    return "Stock dip", f"{detail}. Dips mostly bounce: add on red, or hold."


def book_move(positions: list[dict], contexts_by_coin: dict[str, dict], equity: float) -> float:
    """What the 24-hour moves did to the book: the sum over main-DEX positions of size times the price change,
    as a share of ``equity`` (negative: the book lost). ``positions`` are HL position dicts (coin, szi)."""
    if not equity:
        return float("nan")
    total = 0.0
    for position in positions:
        context = contexts_by_coin.get(str(position.get("coin")))
        if not context:
            continue
        try:
            size, mark, before = float(position.get("szi") or 0), float(context["markPx"]), float(context["prevDayPx"])
        except (KeyError, TypeError, ValueError):
            continue
        if before > 0:
            total += size * (mark - before)
    return total / equity


def _pct(value: float) -> str:
    from desk.numbers import pct

    return pct(value)
