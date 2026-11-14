"""Dollar amounts as the desk shows them: rounded for reading, the same in notebooks and on the dashboards
(desk/dashboard_static/app.js ``usd`` follows the same rule).

    $0.42   under $10: cents
    $548    under $1,000: whole dollars
    $2.11K  $12.3K  $123K
    $2.53M  $16.7M
    $1.2B
"""

from __future__ import annotations

import re

import math

UNITS = ((1e9, "B"), (1e6, "M"), (1e3, "K"))


def usd(value, signed: bool = False, missing: str = "n/a") -> str:
    """``value`` in dollars, rounded to three significant figures from $1,000 up (no "+" on gains)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return missing
    if not math.isfinite(value):
        return missing
    sign = "-" if value < 0 else ""
    size = abs(value)
    rounded = float(f"{size:.3g}")
    for scale, unit in UNITS:
        if rounded >= scale:
            return f"{sign}${rounded / scale:.3g}{unit}"
    if size >= 10:
        return f"{sign}${size:,.0f}"
    return f"{sign}${size:.2f}"


STALE_DAYS = 60   # a date older than this reads as its month (May'25); a recent one keeps its day (Nov 3)


def since(when, now=None) -> str:
    """A start date for reading: "May'25" once it is more than STALE_DAYS old, else "Nov 3"; "" when unknown.
    The dashboards (desk/dashboard_static/app.js and web/live.js ``sinceLabel``) follow the same rule."""
    import pandas as pd

    try:
        stamp = pd.Timestamp(when)
    except (TypeError, ValueError):
        return ""
    if pd.isna(stamp):
        return ""
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now
    if now - stamp > pd.Timedelta(days=STALE_DAYS):
        return f"{stamp:%b}'{stamp:%y}"
    return f"{stamp:%b} {stamp.day}"


# A HIP-3 market's venue prefix ("xyz:AAPL", "io:IONQ", "para:ANSEM") is plumbing: readers see AAPL, as on the
# dashboards (marketName in app.js). A lowercase prefix straight before a ticker; times ("10:30") and prose are untouched.
_MARKET_PREFIX = re.compile(r"(?<![\w:./-])[a-z][a-z0-9]*:(?=[A-Z0-9])")


def market_names(text):
    """``text`` with HIP-3 venue prefixes dropped: "xyz:AAPL +$5" reads "AAPL +$5". Non-text is returned as is."""
    return _MARKET_PREFIX.sub("", text) if isinstance(text, str) else text


def market_frame(frame):
    """``frame`` (a copy) with venue prefixes dropped from every text cell and from a text index."""
    import pandas as pd

    text = lambda dtype: dtype == object or pd.api.types.is_string_dtype(dtype)   # object on pandas 2, str on pandas 3
    out = frame.copy()
    for column in out.columns:
        if text(out[column].dtype):
            out[column] = out[column].map(market_names)
    if text(out.index.dtype):
        out.index = out.index.map(market_names)
    return out


DISPLAY_TIMEZONE = "America/New_York"   # the desk's clock; shown without a zone name


def clock(when, now=None) -> str:
    """A moment in the desk's time zone for reading: "10 am" or "10:35 am" today, "Nov 12, 10 am" this year,
    "Nov 12 '25, 10 am" before; "" when unknown. A naive stamp is taken as UTC (the daemon's and the files')."""
    import pandas as pd

    try:
        stamp = pd.Timestamp(when)
    except (TypeError, ValueError):
        return ""
    if pd.isna(stamp):
        return ""
    stamp = (stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp).tz_convert(DISPLAY_TIMEZONE)
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    now = (now.tz_localize("UTC") if now.tzinfo is None else now).tz_convert(DISPLAY_TIMEZONE)
    hour = stamp.hour % 12 or 12
    time = f"{hour}{f':{stamp.minute:02d}' if stamp.minute else ''} {'am' if stamp.hour < 12 else 'pm'}"
    if stamp.date() == now.date():
        return time
    day = f"{stamp:%b} {stamp.day}" + (f" '{stamp:%y}" if stamp.year != now.year else "")
    return f"{day}, {time}"


def pct(value, signed: bool = False, missing: str = "n/a", scaled: bool = False) -> str:
    """A share or return for reading: whole percents from 10% up (100%, 57%), one decimal from 1% (5.3%), two
    below (0.42%), trailing zeros dropped (5%, not 5.0%). ``value`` is a fraction (0.05), or already in percent
    with ``scaled``. Gains carry no "+" (the colour says it). The dashboards' ``pct`` and ``percent`` follow the same rule."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return missing
    if not math.isfinite(value):
        return missing
    number = value if scaled else value * 100
    size = abs(number)
    digits = 0 if size >= 9.95 else 1 if size >= 0.995 else 2
    text = f"{size:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text == "0":
        return "0%"
    sign = "-" if number < 0 else ""   # no "+" anywhere on the desk: colour carries the sign; ``signed`` is kept for callers
    return f"{sign}{text}%"
