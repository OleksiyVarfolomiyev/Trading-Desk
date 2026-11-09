"""One look for every notebook chart: dark theme, legend above the plot, wrapped titles, $ ticks.

Call ``show(fig)`` instead of ``fig.show()``. It places the legend horizontally above the plot
area, breaks a long title into lines that fit the figure, and sizes the top margin so the title
and every legend row clear each other.
"""

from __future__ import annotations

import re

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
USD_TICKS = {"tickprefix": "", "tickformat": "$,.1f"}
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
        if _USD_ONLY.match(text):
            axis.update(title_text=None, **USD_TICKS)
        elif _USD_UNIT.search(text):
            axis.update(title_text=_USD_UNIT.sub("", text), **USD_TICKS)
        elif axis.tickprefix == "$" or text.lower().startswith("value of $"):
            axis.update(**USD_TICKS)
    return fig


def _has_panel_titles(fig) -> bool:
    """Whether subplot titles sit just above the plot area (where a paper-anchored legend would go)."""
    return any(getattr(note, "yref", None) == "paper" and (note.y or 0) >= 0.98 and note.yanchor == "bottom"
               for note in fig.layout.annotations or ())


def style(fig):
    """Legend on top and a wrapped title, with a top margin that fits both. Returns ``fig``.

    Positions are in pixels from the top of the figure (``yref='container'``): title first, then
    the legend rows, then room for subplot titles, then the plot.
    """
    dollar_axes(fig)
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
