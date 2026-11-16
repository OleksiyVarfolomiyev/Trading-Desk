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


COLUMNS = ["time", "venue", "account", "coin", "book", "side", "size", "entry", "exit", "notional", "pnl", "fees",
           "return", "opened", "hold_days"]


def closed_returns(fills_by_wallet: dict[str, list[dict]], ladders: bool = False) -> pd.DataFrame:
    """One row per HL closing fill, the layout of ``COLUMNS``: when, where, what, side closed, size, entry and exit
    price, closed notional, PnL (HL's closed PnL), the closing fee (negative, a cost), return (PnL over the cost of the part closed)
    and how long the position had been open (since its size last left zero). The market-making ladders' small closes
    are left out unless ``ladders`` (churn around a position, not trades)."""
    from desk.market_making import LADDER_CLOID_PREFIX

    rows = []
    for wallet, fills in fills_by_wallet.items():
        opened: dict[str, int] = {}
        for fill in sorted((f for f in fills if f.get("time") is not None), key=lambda f: int(f["time"])):
            coin, direction = str(fill.get("coin", "")), str(fill.get("dir", ""))
            if "/" in coin or coin.startswith("@"):
                continue
            if float(fill.get("startPosition") or 0) == 0 or coin not in opened:
                opened[coin] = int(fill["time"])
            if not direction.startswith("Close"):
                continue
            if not ladders and str(fill.get("cloid") or "").startswith(LADDER_CLOID_PREFIX):
                continue
            size, price, pnl = abs(float(fill["sz"])), float(fill["px"]), float(fill.get("closedPnl") or 0)
            if size <= 0 or price <= 0:
                continue
            entry = price - pnl / size if "Long" in direction else price + pnl / size
            if entry <= 0:
                continue
            time = pd.to_datetime(int(fill["time"]), unit="ms", utc=True)
            rows.append({"time": time, "venue": "HL", "account": wallet[-4:], "coin": coin,
                         "book": holding_kind("hl", coin), "side": "LONG" if "Long" in direction else "SHORT",
                         "size": size, "entry": entry, "exit": price, "notional": size * price, "pnl": pnl,
                         "fees": -float(fill.get("fee") or 0), "return": pnl / (size * entry),
                         "opened": pd.to_datetime(opened[coin], unit="ms", utc=True),
                         "hold_days": (int(fill["time"]) - opened[coin]) / 86_400_000})
    return pd.DataFrame(rows, columns=COLUMNS)


def ib_statement_closes(folder) -> pd.DataFrame:
    """The same layout for IB stocks, from the activity statements: each closing trade's realized P/L (IB's, net of
    commission) over its basis; entry price, open time and holding period from first-in-first-out pairing of the
    statements' trades (a close of shares bought before the first statement has no open time)."""
    import re
    from pathlib import Path

    from desk.venues import ib_statement

    trades, seen = [], set()
    for path in sorted(Path(folder).glob("ib_statement_*.csv")):
        try:
            sections = ib_statement.read_sections(path)
        except Exception:
            continue
        account = (re.search(r"ib_statement_([^_]+)_", path.name) or [None, ""])[1][-4:]
        for row in sections.get("Trades", []):
            if row.get("DataDiscriminator") != "Order" or row.get("Asset Category") != "Stocks" or row.get("Currency") != "USD":
                continue
            number = lambda key: float(str(row.get(key) or 0).replace(",", "") or 0)
            when = pd.Timestamp(row["Date/Time"].replace(",", "")).tz_localize("America/New_York").tz_convert("UTC")
            key = (row["Symbol"], when, number("Quantity"), number("T. Price"))
            if key in seen:
                continue
            seen.add(key)
            trades.append({"time": when, "coin": row["Symbol"], "quantity": number("Quantity"), "price": number("T. Price"),
                           "commission": number("Comm/Fee"), "basis": number("Basis"), "realized": number("Realized P/L"),
                           "closing": "C" in str(row.get("Code") or "").split(";"), "account": account})
    rows, lots = [], {}
    for trade in sorted(trades, key=lambda t: t["time"]):
        book, left = lots.setdefault(trade["coin"], []), trade["quantity"]
        consumed, weighted, first = 0.0, 0.0, None
        while left and book and (book[0][0] > 0) != (left > 0):
            lot = book[0]
            part = min(abs(lot[0]), abs(left))
            consumed, weighted = consumed + part, weighted + part * lot[1]
            first = first or lot[2]
            lot[0] += -part if lot[0] > 0 else part
            left += -part if left > 0 else part
            if abs(lot[0]) < 1e-12:
                book.pop(0)
        if abs(left) > 1e-12:
            book.append([left, trade["price"], trade["time"]])
        if not trade["closing"] or not trade["basis"]:
            continue
        size = abs(trade["quantity"])
        entry = weighted / consumed if consumed else abs(trade["basis"]) / size
        rows.append({"time": trade["time"], "venue": "IB", "account": trade["account"], "coin": trade["coin"],
                     "book": "Stocks", "side": "LONG" if trade["quantity"] < 0 else "SHORT", "size": size, "entry": entry,
                     "exit": trade["price"], "notional": size * trade["price"], "pnl": trade["realized"],
                     "fees": trade["commission"], "return": trade["realized"] / abs(trade["basis"]),
                     "opened": first if first is not None else pd.NaT,
                     "hold_days": (trade["time"] - first).total_seconds() / 86400 if first is not None else float("nan")})
    return pd.DataFrame(rows, columns=COLUMNS)


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


def metrics_table(frame: pd.DataFrame) -> pd.DataFrame:
    """The closes' numbers a model or a backtest needs, by side closed and in all: how often and how big the wins
    are against the losses (volume-weighted share in profit, payoff = average win over average loss, profit factor =
    gross profit over gross loss), expectancy per closed dollar, and how long positions were held."""
    rows = []
    for name, part in (("Long", frame[frame["side"] == "LONG"]), ("Short", frame[frame["side"] == "SHORT"]), ("All", frame)):
        if part.empty:
            continue
        wins, losses = part[part["pnl"] > 0], part[part["pnl"] < 0]
        weighted = lambda f: float((f["return"] * f["notional"]).sum() / f["notional"].sum()) if len(f) else float("nan")
        gross_loss = -float(losses["pnl"].sum())
        rows.append({"Side": name, "Closes": len(part), "Volume": float(part["notional"].sum()),
                     "In profit": float(wins["notional"].sum() / part["notional"].sum()),
                     "Avg win": weighted(wins), "Avg loss": weighted(losses),
                     "Payoff": weighted(wins) / -weighted(losses) if len(losses) and len(wins) else float("nan"),
                     "Profit factor": float(wins["pnl"].sum()) / gross_loss if gross_loss else float("nan"),
                     "Expectancy": weighted(part), "PnL": float(part["pnl"].sum()), "Fees": float(part["fees"].sum()),
                     "Median hold (days)": float(part["hold_days"].median())})
    return pd.DataFrame(rows)


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


def trades_per_day(fills_by_wallet: dict[str, list[dict]], kind: str, ib_fills: pd.DataFrame | None = None) -> pd.Series:
    """Fills per UTC day in one book: HL fills of its markets (the ladders' own left out, they are not decisions) and,
    for stocks, IB's statement trades."""
    from desk.market_making import LADDER_CLOID_PREFIX

    days = []
    for fills in fills_by_wallet.values():
        for fill in fills:
            coin = str(fill.get("coin", ""))
            if fill.get("time") is None or "/" in coin or coin.startswith("@") \
                    or str(fill.get("cloid") or "").startswith(LADDER_CLOID_PREFIX) or holding_kind("hl", coin) != kind:
                continue
            days.append(pd.to_datetime(int(fill["time"]), unit="ms", utc=True).normalize())
    if kind == "Stocks" and ib_fills is not None and len(ib_fills):
        days += list(pd.to_datetime(ib_fills["time"], utc=True).dt.normalize())
    return pd.Series(1, index=pd.DatetimeIndex(days)).groupby(level=0).sum() if days else pd.Series(dtype=int)


def plot_trades_vs_pnl(trades: pd.Series, returns: pd.Series, title: str = ""):
    """Each active day as a point: fills that day against the book's return that day (% of the day's equity), with
    the least-squares line and its correlation and t-stat in the title."""
    import plotly.graph_objects as go

    from desk.charts import show
    from desk.numbers import pct

    days = returns.index
    frame = pd.DataFrame({"trades": trades.reindex(days).fillna(0).astype(int), "return": 100 * returns})
    n = len(frame)
    if n < 10:
        print(f"Only {n} active days: too few to compare.")
        return
    slope, intercept = np.polyfit(frame["trades"], frame["return"], 1)
    corr = float(np.corrcoef(frame["trades"], frame["return"])[0, 1])
    t = corr * np.sqrt((n - 2) / max(1e-12, 1 - corr ** 2))
    rng = np.random.default_rng(0)
    jitter = frame["trades"] + rng.uniform(-0.18, 0.18, n)   # same counts do not stack on one vertical line
    figure = go.Figure()
    for name, color, mask in (("Gain days", "#16a34a", frame["return"] >= 0), ("Loss days", "#dc2626", frame["return"] < 0)):
        figure.add_trace(go.Scatter(x=jitter[mask], y=frame["return"][mask], mode="markers", name=name,
                                    marker={"color": color, "size": 8, "opacity": 0.8},
                                    text=[f"{d:%b %d}: {int(k)} fills" for d, k in zip(frame.index[mask], frame["trades"][mask])],
                                    hovertemplate="%{text}, %{y:.2f}%<extra></extra>"))
    xs = np.array([0, frame["trades"].max()])
    figure.add_trace(go.Scatter(x=xs, y=intercept + slope * xs, mode="lines", name="Fit",
                                line={"color": "#e5e7eb", "dash": "dot"}))
    figure.update_layout(
        title=f"{title}: daily return vs fills per day, correlation {corr:.2f} (t {t:.1f}, {n} days)",
        template="plotly_dark", hovermode="closest", xaxis={"title": "Fills in the day"},
        yaxis={"title": "Daily return", "ticksuffix": "%", "zeroline": True})
    show(figure)
