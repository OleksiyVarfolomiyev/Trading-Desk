"""Returns on the money the desk closed: how much volume closed at each return, and the density behind it.

Each closing fill on HL carries HL's closed PnL; its return is that PnL over the cost of the part closed
(size x entry price, the entry recovered from the fill price and the PnL). The histogram is weighted by the
closed notional, so a bar's height reads as dollars closed at that return and the density as the chance that a
dollar closed lands there, a kernel estimate on the same history (the desk's own, so it carries its regime).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from desk.books import holding_kind


def closed_returns(fills_by_wallet: dict[str, list[dict]], ladders: bool = False) -> pd.DataFrame:
    """One row per closing fill: time, coin, book, return, closed notional, closed PnL. The market-making ladders'
    small closes are left out unless ``ladders`` (they are churn around a position, not trades)."""
    from desk.market_making import LADDER_CLOID_PREFIX

    rows = []
    for wallet, fills in fills_by_wallet.items():
        for fill in fills:
            direction = str(fill.get("dir", ""))
            if not ladders and str(fill.get("cloid") or "").startswith(LADDER_CLOID_PREFIX):
                continue
            if not direction.startswith("Close") or "/" in str(fill.get("coin", "")) or str(fill["coin"]).startswith("@"):
                continue
            size, price, pnl = abs(float(fill["sz"])), float(fill["px"]), float(fill.get("closedPnl") or 0)
            if size <= 0 or price <= 0:
                continue
            entry = price - pnl / size if "Long" in direction else price + pnl / size
            if entry <= 0:
                continue
            rows.append({"time": pd.to_datetime(int(fill["time"]), unit="ms", utc=True), "wallet": wallet[-4:],
                         "coin": fill["coin"], "book": holding_kind("hl", fill["coin"]),
                         "return": pnl / (size * entry), "notional": size * price, "pnl": pnl})
    return pd.DataFrame(rows)


def weighted_density(values: np.ndarray, weights: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Gaussian kernel density of ``values`` weighted by ``weights`` (Silverman's bandwidth on the weighted spread)."""
    weights = weights / weights.sum()
    mean = (weights * values).sum()
    spread = np.sqrt((weights * (values - mean) ** 2).sum())
    effective = 1.0 / (weights ** 2).sum()
    bandwidth = max(1.06 * spread * effective ** -0.2, 1e-4)
    return (weights[None, :] * np.exp(-0.5 * ((grid[:, None] - values[None, :]) / bandwidth) ** 2)).sum(axis=1) \
        / (bandwidth * np.sqrt(2 * np.pi))


def summary(frame: pd.DataFrame) -> dict:
    """Volume-weighted facts of a set of closes: share of dollars closed in profit, mean, 5th and 95th percentile."""
    order = frame.sort_values("return")
    cumulative = order["notional"].cumsum() / order["notional"].sum()
    quantile = lambda q: float(order["return"].iloc[min(int(cumulative.searchsorted(q)), len(order) - 1)])
    return {"closes": len(frame), "volume": float(frame["notional"].sum()),
            "in profit": float(frame.loc[frame["return"] > 0, "notional"].sum() / frame["notional"].sum()),
            "mean": float((frame["return"] * frame["notional"]).sum() / frame["notional"].sum()),
            "p5": quantile(0.05), "median": quantile(0.5), "p95": quantile(0.95)}


def plot_return_distribution(frame: pd.DataFrame, title: str = "", bins: int = 40, clip: float = 0.3):
    """Closed volume by return (bars, dollars) with the density of a closed dollar's return (line, right axis)."""
    import plotly.graph_objects as go

    from desk.charts import show
    from desk.numbers import pct

    data = frame.copy()
    data["return"] = data["return"].clip(-clip, clip)
    edges = np.linspace(-clip, clip, bins + 1)
    counts, _ = np.histogram(data["return"], bins=edges, weights=data["notional"])
    centers = 100 * (edges[:-1] + edges[1:]) / 2
    width = 100 * (edges[1] - edges[0]) * 0.92
    figure = go.Figure()
    for name, color, mask in (("Closed at a loss", "#dc2626", centers < 0), ("Closed in profit", "#16a34a", centers >= 0)):
        figure.add_trace(go.Bar(x=centers[mask], y=counts[mask], width=width, name=name, marker_color=color, opacity=0.8,
                                hovertemplate="%{x:.1f}%: $%{y:,.0f} closed<extra></extra>"))
    grid = np.linspace(-clip, clip, 400)
    density = weighted_density(data["return"].to_numpy(), data["notional"].to_numpy(), grid)
    figure.add_trace(go.Scatter(x=100 * grid, y=density / 100, name="Density", yaxis="y2", line={"color": "#e5e7eb", "width": 2},
                                hovertemplate="%{x:.1f}%: %{y:.3f}<extra></extra>"))
    stats = summary(frame)
    figure.update_layout(
        title=f"{title}: {pct(stats['in profit'])} of dollars closed in profit, mean {pct(stats['mean'])}, "
              f"5th percentile {pct(stats['p5'])}",
        template="plotly_dark", bargap=0, xaxis={"title": "Return on the closed position", "ticksuffix": "%"},
        yaxis={"title": "Volume closed ($)"},
        yaxis2={"title": "Probability density", "overlaying": "y", "side": "right", "rangemode": "tozero",
                "showgrid": False})
    show(figure)
