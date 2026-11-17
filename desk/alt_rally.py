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
MOMENTUM_COINS = ("BTC", "ETH")   # a fast move in the majors is the desk's momentum signal
MOMENTUM_MOVE = 0.03               # 3% or more off the low (or the high) ...
MOMENTUM_MINUTES = 120             # ... within the last two hours
SELLOFF_MOVE = -0.05               # a bounce after a 5%+ fall over the prior day is the desk's cue to take profits
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


SQUEEZE_MOVE = 0.15        # the position's coin moved this much against it in 24 hours...
SQUEEZE_FUNDING_APR = 5.0  # ...while its side pays this much funding (500% APR): crowded and being squeezed


def squeeze(side: str, move: float, funding_apr: float) -> bool:
    """A position being squeezed: a short up SQUEEZE_MOVE with shorts paying SQUEEZE_FUNDING_APR (or a long the mirror).
    ``funding_apr`` is HL's rate annualised (positive: longs pay)."""
    if not (math.isfinite(move) and math.isfinite(funding_apr)):
        return False
    if side == "short":
        return move >= SQUEEZE_MOVE and funding_apr <= -SQUEEZE_FUNDING_APR
    return move <= -SQUEEZE_MOVE and funding_apr >= SQUEEZE_FUNDING_APR


def squeeze_alert(coin: str, side: str, move: float, funding_apr: float, mark: float, stop: float | None) -> tuple[str, str]:
    """("Squeeze: BERA short", "24h 29%, funding -7.5% d, SL 8.3%"): the move against the position, the funding it
    pays a day (negative: paid), the stop's distance."""
    return f"Squeeze: {coin.split(':')[-1]} {side}", f"24h {_pct(abs(move))}, {_funding_stop(funding_apr, mark, stop)}"


def _funding_stop(funding_apr: float, mark: float, stop: float | None) -> str:
    """"funding -7.5% d, SL 8.3%": what the squeezed side pays a day, and how far the stop is (or "no SL")."""
    stop_text = f"SL {_pct(abs(stop / mark - 1))}" if stop and mark else "no SL"
    return f"funding {_pct(-abs(funding_apr) / 365)} d, {stop_text}"


# Naming: "Squeeze stalled" is only a squeeze that stopped running while its funding still bleeds (critical: the cue
# to cut). A position that is merely not moving is a capital-efficiency matter and, if it is ever labelled, is
# "Idle" (an amber marker or a screener note), never "stalled", so the two cannot be mistaken for each other.
STALL_HOURS = 12
STALL_BAND = 0.03          # every hourly close within +-3% of the stretch's midpoint: the move has stalled


def stalled(closes: list[float]) -> bool:
    """The last STALL_HOURS hourly closes all within +-STALL_BAND of their midpoint (the price has stopped moving)."""
    if len(closes) < STALL_HOURS:
        return False
    recent = closes[-STALL_HOURS:]
    middle = (max(recent) + min(recent)) / 2
    return middle > 0 and max(recent) <= middle * (1 + STALL_BAND) and min(recent) >= middle * (1 - STALL_BAND)


def stalled_squeeze(closes: list[float], side: str) -> bool:
    """A squeeze that stopped: the coin ran SQUEEZE_MOVE against the position in the 24 hours before the last
    STALL_HOURS, and those hours are flat. ``closes``: hourly, oldest first, at least 24 + STALL_HOURS of them.
    A coin that was merely flat while paying funding is not a stalled squeeze."""
    if len(closes) < 24 + STALL_HOURS or not stalled(closes):
        return False
    start, before = closes[-STALL_HOURS], closes[-(24 + STALL_HOURS):-STALL_HOURS]
    run = start / min(before) - 1 if side == "short" else 1 - start / max(before)
    return run >= SQUEEZE_MOVE


def stall_alert(coin: str, side: str, funding_apr: float, mark: float, stop: float | None) -> tuple[str, str]:
    """("Squeeze stalled: BERA short", "flat 12h, funding -8.6% d, SL 9%"): the cue to cut a squeezed position that
    stopped moving while its funding still bleeds."""
    return f"Squeeze stalled: {coin.split(':')[-1]} {side}", f"flat {STALL_HOURS}h, {_funding_stop(funding_apr, mark, stop)}"


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


def burst(candles: list[dict], threshold: float = MOMENTUM_MOVE) -> tuple[float, float, float, float] | None:
    """A fast move in the window ``candles`` cover (HL candles: t, h, l, c): ``(move, minutes, from, to)`` off the
    window's low (a bounce, positive) or high (a drop, negative), whichever is larger, when it reaches ``threshold``."""
    if not candles:
        return None
    close, end = float(candles[-1]["c"]), int(candles[-1]["t"])
    low = min(candles, key=lambda candle: float(candle["l"]))
    high = max(candles, key=lambda candle: float(candle["h"]))
    up = close / float(low["l"]) - 1
    down = close / float(high["h"]) - 1
    move, start, price = (up, int(low["t"]), float(low["l"])) if up >= -down else (down, int(high["t"]), float(high["h"]))
    if abs(move) < threshold:
        return None
    return move, max(5.0, (end - start) / 60000 + 5), price, close


def selloff_before(hourly: list[dict], low_time_ms: int) -> float:
    """How far the price fell over the 24 hours up to ``low_time_ms`` (the bounce's low), from HL hourly candles:
    the low against the highest high of that day (negative)."""
    day = [candle for candle in hourly if low_time_ms - 86_400_000 <= int(candle["t"]) <= low_time_ms]
    if not day:
        return 0.0
    low = min(float(candle["l"]) for candle in day)
    return low / max(float(candle["h"]) for candle in day) - 1


def volume_ratio(candles: list[dict], since_ms: int) -> float:
    """Volume per candle since ``since_ms`` (the move) against the median candle of the rest of the day: momentum is
    read in price speed and volume together."""
    move = [float(candle.get("v") or 0) for candle in candles if int(candle["t"]) >= since_ms]
    rest = sorted(float(candle.get("v") or 0) for candle in candles if int(candle["t"]) < since_ms)
    if not move or not rest:
        return float("nan")
    usual = rest[len(rest) // 2]
    return (sum(move) / len(move)) / usual if usual else float("nan")


CROWDED_FUNDING = 0.20   # 20% a year against the move: the other side is crowded and fighting it


def funding_read(move: float, funding_apr: float) -> str:
    """Who is behind a fast move: quiet funding is a natural flow; funding paid by the side the move hurts (shorts
    paying into a bounce) is a crowded squeeze."""
    against = -funding_apr if move > 0 else funding_apr   # shorts pay (negative funding) into a bounce
    if against >= CROWDED_FUNDING:
        return f"funding {_pct(funding_apr)} a year: {'shorts' if move > 0 else 'longs'} crowded, a squeeze"
    return f"funding {_pct(funding_apr)} a year: quiet, a natural flow"


EXTRAORDINARY_SIGMA = 2.0   # a 24h move this many daily standard deviations is "too good to be true": take some
EXTRAORDINARY_MARKETS = (("BTC", ""), ("ETH", ""), ("xyz:SP500", "xyz"))


def extraordinary(move: float, daily_closes: list[float], sigma: float = EXTRAORDINARY_SIGMA) -> float | None:
    """The 24h ``move`` in daily standard deviations (the last 60 daily returns) when it reaches ``sigma``, else None.
    The desk's rule: an extraordinary day is when the side it favours trims profits (1/3 to 2/3 of a big move is the
    take; nobody calls the top), whichever way it goes."""
    returns = [b / a - 1 for a, b in zip(daily_closes[-61:-1], daily_closes[-60:]) if a > 0]
    if len(returns) < 20:
        return None
    mean = sum(returns) / len(returns)
    sd = (sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)) ** 0.5
    if not sd:
        return None
    score = move / sd
    return score if abs(score) >= sigma else None


def fading(candles: list[dict], move: float, recent: int = 6) -> bool:
    """True once a fast move has lost its strength: the last ``recent`` candles (30 minutes of 5-minute candles) went
    against it, or the price gave back a third of the move from its best. Align with strength; a fading move is
    weakness and is not chased."""
    if len(candles) <= recent:
        return False
    close = float(candles[-1]["c"])
    then = float(candles[-recent - 1]["c"])
    if move > 0:
        low, best = min(float(c["l"]) for c in candles), max(float(c["h"]) for c in candles)
        return close < then or best - close >= (best - low) / 3
    high, worst = max(float(c["h"]) for c in candles), min(float(c["l"]) for c in candles)
    return close > then or close - worst >= (high - worst) / 3


# Shorts outrunning BTC: the desk shorts alts weaker than BTC, so shorts rising faster than BTC is the thesis turning
# while the basket may still be under the alt-rally watch. 2026-11-15: the 11 shorts ran +5% to +13% in 24 hours with
# BTC +0.5% and the basket at +4.5%, and nothing said so until the user did.
STRENGTH_MOVE_24H = 0.03    # the short book, size-weighted, 3%+ ahead of BTC over 24 hours...
STRENGTH_MOVE_4H = 0.02     # ...or 2%+ ahead over 4 hours (the turn inside the day)
STRENGTH_BREADTH = 0.6      # with most of the shorts ahead of BTC, not one squeeze
STRENGTH_MIN_SHORTS = 3


def shorts_vs_btc(shorts: list[tuple], btc_move: float) -> dict | None:
    """How the short book moved against BTC, beta-adjusted: ``shorts`` are (coin, |notional|, move over the window[,
    beta to BTC]). Alts are a more volatile BTC, so a short "beats" BTC only when it rises more than its beta times
    BTC's move (beta 1 when unknown). Returns the size-weighted excess, how many shorts beat their beta-expected move
    of how many, the book's raw move and the three furthest ahead."""
    rows = []
    for item in shorts:
        coin, size, move = item[:3]
        beta = item[3] if len(item) > 3 and item[3] is not None and math.isfinite(item[3]) else 1.0
        if size > 0 and math.isfinite(move):
            rows.append((coin, size, move, move - beta * btc_move))
    if len(rows) < STRENGTH_MIN_SHORTS or not math.isfinite(btc_move):
        return None
    total = sum(size for _, size, _, _ in rows)
    leaders = sorted(rows, key=lambda row: row[3], reverse=True)[:3]
    return {"ahead": sum(size * excess for _, size, _, excess in rows) / total,
            "beating": sum(excess > 0 for _, _, _, excess in rows), "count": len(rows), "btc": btc_move,
            "book": sum(size * move for _, size, move, _ in rows) / total,
            "leaders": [(coin, move) for coin, _, move, _ in leaders]}


BTC_TURN_MOVE = 0.02   # BTC 2%+ off its 24-hour low and green on the day: the market turned under a short book


def btc_turned(off_low: float, day_move: float) -> bool:
    """BTC climbed BTC_TURN_MOVE or more off its 24-hour low and is now up on the day (red to green)."""
    return math.isfinite(off_low) and math.isfinite(day_move) and off_low >= BTC_TURN_MOVE and day_move > 0


def strength_alert(day: dict | None, recent: dict | None, btc_off_low: float = float("nan"),
                   btc_day: float = float("nan")) -> tuple[str, str] | None:
    """An alert when the short book is off-thesis, or None:

    * "Alts outrunning BTC": the shorts run past their beta times BTC's move over 4 hours (2%+) or 24 hours (3%+), most
      of them doing so;
    * "Market turned green": BTC 2%+ off its day low and green on the day, with the shorts at least keeping pace over
      4 hours, beta-adjusted (the user's checkpoint: BTC from red to green is the market turning; alts are a more
      volatile BTC, so they follow it by their beta).
    """
    def breadth(read):
        return read is not None and read["beating"] >= STRENGTH_BREADTH * read["count"]

    turned = btc_turned(btc_off_low, btc_day)
    btc_text = f"BTC {_pct(btc_off_low)} off its day low and green on the day ({_pct(btc_day)})" if turned else ""
    parts = [(label, read) for label, read, threshold in (("4h", recent, STRENGTH_MOVE_4H), ("D", day, STRENGTH_MOVE_24H))
             if breadth(read) and read["ahead"] >= threshold]
    if parts:
        label, read = parts[0]
        title = "Alts outrunning BTC: shorts off-thesis"
    elif turned and breadth(recent) and recent["ahead"] >= 0:
        label, read = "4h", recent
        title = "Market turned green: shorts off-thesis"
    else:
        return None
    leaders = f"{label} " + ", ".join(f"{coin.split(':')[-1]} {_pct(move)}" for coin, move in read["leaders"])   # the coins' moves, not the positions' result
    text = (f"shorts {_pct(read['book'])} vs BTC {_pct(read['btc'])} in {label}, {_pct(read['ahead'])} beyond their "
            f"beta ({read['beating']} of {read['count']} ahead; {leaders})" + (f"; {btc_text}" if btc_text else "")
            + ". The weak-alt thesis is off for now: a signal if it holds for days or BTC turns with it.")
    return title, text[0].upper() + text[1:]
