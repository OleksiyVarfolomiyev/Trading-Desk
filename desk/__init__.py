"""Venue-neutral trading desk: accounts, venues, cross-venue risk and reports."""

# Every notebook chart uses Plotly's dark theme (black background), set once here because every
# notebook imports the desk package. Charts may still name the template explicitly.
CHART_TEMPLATE = "plotly_dark"
try:
    import plotly.io as _pio

    _pio.templates.default = CHART_TEMPLATE
except ImportError:  # plotly is optional for the executors
    pass
