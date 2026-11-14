"""One look for every notebook chart: dark theme, legend above the plot, wrapped titles, $ ticks.

Call ``show(fig)`` instead of ``fig.show()``. It places the legend horizontally above the plot
area, breaks a long title into lines that fit the figure, and sizes the top margin so the title
and every legend row clear each other.
"""

from __future__ import annotations

import re
from desk.numbers import usd as _usd, pct as _pct

LINE_PX = 22                # height of one title or legend line
CHAR_PX = 10.0              # width of one title character (17px font), rounded up so titles never overflow
LEGEND_ITEM_PAD_PX = 45     # symbol and spacing around each legend label
LEGEND_CHAR_PX = 7.0        # rough width of one legend character
DEFAULT_WIDTH_PX = 800      # notebook output width when a figure does not set one (narrow side)
DEFAULT_HEIGHT_PX = 450     # Plotly's default figure height
TOP_PAD_PX = 10
GAP_PX = 8
MIN_TITLE_CHARS = 30


def wrap_title(text: str, width_px: float | None = None) -> str:
    """Break a title into lines (``<br>``) that fit the figure width; existing breaks are kept."""
    if not text:
        return text
    limit = max(MIN_TITLE_CHARS, int((width_px or DEFAULT_WIDTH_PX) / CHAR_PX) - 4)
    lines = []
    for part in re.split(r"<br\s*/?>", text):
        line = ""
        for word in part.split(" "):
            plain = re.sub(r"<[^>]+>", "", line + " " + word if line else word)
            if line and len(plain) > limit:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        lines.append(line)
    return "<br>".join(lines)


def _legend_rows(fig, width_px: float) -> int:
    """How many rows a horizontal legend needs for the figure's named traces."""
    names = [trace.name for trace in fig.data
             if trace.name and trace.showlegend is not False and getattr(trace, "type", "") not in {"heatmap"}]
    # Plotly shows no legend for a single trace unless asked to.
    if not names or fig.layout.showlegend is False or (len(names) == 1 and fig.layout.showlegend is not True):
        return 0
    rows, used = 1, 0.0
    for name in names:
        item = LEGEND_ITEM_PAD_PX + LEGEND_CHAR_PX * len(re.sub(r"<[^>]+>", "", str(name)))
        if used and used + item > width_px:
            rows, used = rows + 1, 0.0
        used += item
    return rows


# d3 currency format: $1,234.5 and -$50.0 (a "$" tickprefix would print $-50.0).
USD_TICKS = {"tickprefix": "", "tickformat": "$~s"}  # $10k: short ticks that fit; end labels carry the detail
_USD_ONLY = re.compile(r"^\s*(USD|\$|US\$)\s*$", re.I)
_USD_UNIT = re.compile(r"\s*\((USD|\$|US\$)\)\s*$", re.I)


def dollar_axes(fig):
    """Dollar axes show $ in the tick labels instead of naming the unit in the title.

    An axis titled "USD" loses its title; "Daily volume ($)" becomes "Daily volume". Both, and
    any axis already prefixed with $ or titled "Value of $...", get ticks like $1,234.5.
    """
    for name in fig.layout.to_plotly_json():
        if not name.startswith(("xaxis", "yaxis")):
            continue
        axis = fig.layout[name]
        text = axis.title.text if axis.title and axis.title.text else ""
        # A chart that already chose a dollar format (whole dollars, "$,.0f") keeps it.
        ticks = {} if (axis.tickformat or "").startswith("$") else USD_TICKS
        if _USD_ONLY.match(text):
            axis.update(title_text=None, **ticks)
        elif _USD_UNIT.search(text):
            axis.update(title_text=_USD_UNIT.sub("", text), **ticks)
        elif axis.tickprefix == "$" or text.lower().startswith("value of $"):
            axis.update(**ticks)
    return fig


def _has_panel_titles(fig) -> bool:
    """Whether subplot titles sit just above the plot area (where a paper-anchored legend would go)."""
    return any(getattr(note, "yref", None) == "paper" and (note.y or 0) >= 0.98 and note.yanchor == "bottom"
               for note in fig.layout.annotations or ())


END_LABEL_MARGIN_PX = 84   # right margin that fits an end label such as "$12,345.67"


def _axis_name(ref: str | None, kind: str) -> str:
    """``'y3'`` -> ``'yaxis3'``; ``None`` or ``'y'`` -> ``'yaxis'``."""
    suffix = (ref or kind)[1:]
    return f"{kind}axis{suffix}"


def _end_text(value: float, axis, chart_title: str = "") -> str:
    """The end value in its axis's own units: $1,234.56, 12.34%, or 104.3 on an index (start = 100)."""
    title = ((axis.title.text or "") if axis and axis.title else "") + " " + (chart_title or "")
    if axis is not None and ((axis.tickformat or "").startswith("$") or axis.tickprefix == "$"):
        return f"{_usd(-abs(value))}" if value < 0 else f"{_usd(value)}"
    if axis is not None and (axis.tickformat or "").endswith("%"):
        return f"{_pct(value * 100, scaled=True)}"  # a d3 "%" format scales: 0.14 is shown as 14%
    if axis is not None and axis.ticksuffix == "%":
        return f"{_pct(value, scaled=True)}"  # values already in percent
    if "index" in title.lower() or "start = 100" in title or "of $100" in title:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def _is_time(values) -> bool:
    """Whether a trace's x values are dates (time series get end labels; returns or regressions do not)."""
    import numpy as np
    import pandas as pd

    if values is None or not len(values):
        return False
    first = values[0]
    if isinstance(first, (pd.Timestamp, np.datetime64)) or hasattr(first, "year"):
        return True
    if isinstance(first, str):
        try:
            pd.Timestamp(first)
            return not first.replace(".", "", 1).lstrip("-").isdigit()
        except (ValueError, TypeError):
            return False
    return False


END_LABEL_GAP_PX = 14


def _spread(fig, labels: list[dict]) -> None:
    """Push end labels closer than a line apart vertically (``yshift``), keeping their order, as the web dashboard
    does. The axis range is estimated from the traces (the figure is not rendered yet), so the gap is approximate."""
    import math

    height = (fig.layout.height or 450) - 160   # roughly the plot area under the title, legend and margins
    for ref in {label["yref"] for label in labels}:
        values = [label["y"] for label in labels if label["yref"] == ref]
        for trace in fig.data:
            if (trace.yaxis or "y") == ref and trace.y is not None and not trace.stackgroup:
                values += [float(v) for v in trace.y if v is not None and math.isfinite(float(v))]
        low, high = min(values), max(values)
        if high <= low:
            continue
        scale = height / ((high - low) * 1.1)
        group = sorted((label for label in labels if label["yref"] == ref), key=lambda label: -label["y"])
        place = None
        for label in group:
            at = (label["y"] - low) * scale
            place = at if place is None or place - at >= END_LABEL_GAP_PX else place - END_LABEL_GAP_PX
            if round(place - at):
                label["yshift"] = round(place - at)


def end_labels(fig):
    """Write each time-series line's last value at its right end, as on the web dashboard. Returns ``fig``.

    A stacked layer's label sits at the top of its layer and shows the layer's own value. Lines drawn
    as markers only (a live point, scatter points) and charts without dates on x are left alone.
    """
    import math

    import pandas as pd

    stacked: dict[tuple, float] = {}
    labels = []
    ends: dict[str, int] = {}   # legend group -> index of the label that ends latest
    chart_title = fig.layout.title.text if fig.layout.title and fig.layout.title.text else ""
    for trace in fig.data:
        if getattr(trace, "type", "scatter") != "scatter" or trace.visible is False:
            continue
        mode = trace.mode or "lines"
        if "lines" not in mode or not _is_time(trace.x) or trace.y is None:
            continue
        points = [(x, float(y)) for x, y in zip(trace.x, trace.y) if y is not None and math.isfinite(float(y))]
        if not points:
            continue
        x, y = points[-1]
        top = y
        if trace.stackgroup:  # the layer's label sits where the layer ends on the stack
            key = (trace.xaxis, trace.yaxis, trace.stackgroup)
            top = stacked.get(key, 0.0) + y
            stacked[key] = top
        axis = fig.layout[_axis_name(trace.yaxis, "y")]
        color = (trace.line.color if trace.line and trace.line.color else None)
        group = trace.legendgroup  # one line drawn as several traces (green above zero, red below): label its real end
        if group:
            earlier = ends.get(group)
            if earlier is not None:
                if pd.Timestamp(labels[earlier]["x"]) >= pd.Timestamp(x):
                    continue
                labels[earlier] = None
            ends[group] = len(labels)
        labels.append({"x": x, "y": top, "xref": trace.xaxis or "x", "yref": trace.yaxis or "y",
                       "text": _end_text(y, axis, chart_title), "showarrow": False, "xanchor": "left", "xshift": 6,
                       "font": {"size": 11, **({"color": color} if color else {})}})
    labels = [label for label in labels if label]
    _spread(fig, labels)
    for label in labels:
        fig.add_annotation(**label)
    if labels:
        right = (fig.layout.margin.r if fig.layout.margin and fig.layout.margin.r is not None else 0)
        fig.update_layout(margin={"r": max(right, END_LABEL_MARGIN_PX)})
    return fig


def style(fig):
    """Legend on top, a wrapped title and end-value labels, with a top margin that fits. Returns ``fig``.

    Positions are in pixels from the top of the figure (``yref='container'``): title first, then
    the legend rows, then room for subplot titles, then the plot.
    """
    dollar_axes(fig)
    end_labels(fig)
    width = fig.layout.width or DEFAULT_WIDTH_PX
    height = fig.layout.height or DEFAULT_HEIGHT_PX
    title = fig.layout.title.text if fig.layout.title else None
    used = TOP_PAD_PX
    if title:
        wrapped = wrap_title(title, width)
        lines = wrapped.count("<br>") + 1
        # Plotly positions a multi-line title by its first line whatever the anchor; later lines
        # run downward. So the first line sits just under the top, and room is kept for the rest.
        fig.update_layout(title={"text": wrapped, "yref": "container", "y": 1 - (used + LINE_PX) / height,
                                 "yanchor": "bottom", "x": 0.01, "xanchor": "left"})
        used += lines * LINE_PX + GAP_PX
    rows = _legend_rows(fig, width)
    if rows:
        fig.update_layout(legend={"orientation": "h", "yref": "container", "y": 1 - used / height,
                                  "yanchor": "top", "xanchor": "left", "x": 0})
        used += rows * LINE_PX + GAP_PX
    if _has_panel_titles(fig):
        used += LINE_PX
    if title or rows:
        fig.update_layout(margin={"t": used + GAP_PX})
    return fig


def show(fig) -> None:
    """Style the figure (legend on top, wrapped title) and display it."""
    style(fig).show()
