"""Combined account analytics for every desk account: the equivalent of hl_accounts and ib_account.

Combines the HL wallets (saved value and PnL history) and the IB account (IB Flex daily NAV
with deposits and withdrawals) into one desk time-weighted return, so deposits, withdrawals
and transfers between accounts are never performance. Wallet tokens on other chains have no
value history, so they appear in current totals only.

HL keeps detailed history for about a month (older saved points are weekly), so risk
statistics use weekly returns over the full shared history, plus daily returns over the days
every account has daily data.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from IPython.display import display

from desk.accounts import accounts_for, ib_data_dir, load_accounts, short_id
from desk.privacy import account_file
from desk.hl.account_analytics import display_interactive_table, fetch_btc_candles, update_portfolio_history
from desk.hl.trade_analytics import bootstrap_btc_regression, bootstrap_var, time_weighted_index
from desk.live_risk import MIN_RATIO_DAYS
from desk.notebook_highlights import HighlightsState, percent, signed_money
from desk.notebook_highlights import show as show_highlights
from desk.overview import DeskOverview
from desk import emphasis
from desk.charts import show as show_chart

ROOT_DATA = Path(__file__).resolve().parents[1] / "data"
RESEARCH_PRICES = Path(__file__).resolve().parents[1] / "research" / "data" / "daily_prices_usd.csv"
# IB NAV is end of the US trading day; stamp it at 21:00 UTC (16:00-17:00 New York).
IB_CLOSE_HOUR_UTC = 21
WINDOWS = {"7D": 7, "30D": 30, "90D": 90, "1Y": 365}
# A window may start up to this far past its cut. HL's month history begins exactly 30 days before its latest
# sample, so a young account's first point (c248, funded 2026-10-21) sits on the 30D cut and was read as just
# too late about half the time, leaving the 30D return blank. Same rule as WINDOW_EDGE_MS in web/live.js.
WINDOW_EDGE = pd.Timedelta(days=1)


def through(series, cutoff: pd.Timestamp):
    """The points at or before ``cutoff``; when there are none, the first point if it is within ``WINDOW_EDGE``."""
    before = series[series.index <= cutoff]
    if before.empty and len(series) and series.index[0] <= cutoff + WINDOW_EDGE:
        return series.iloc[:1]
    return before
# The performance table's periods: the windows, then since each account's (or the desk's) first day.
INCEPTION = "since inception"
PERIODS = [*WINDOWS, INCEPTION]
ACCOUNTS_HIGHLIGHTS_STATE = "desk_accounts_highlights_state.json"
DEEP_DRAWDOWN = -0.10   # the desk this far below its peak is flagged


def ib_history(nav: pd.DataFrame) -> pd.DataFrame:
    """IB Flex NAV as ``ts, accountValue, pnl`` (cumulative PnL = NAV change less deposits)."""
    frame = nav.copy()
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    frame = frame.drop_duplicates("date", keep="last").sort_values("date")
    change = frame["nav"].diff() - frame["flows"]
    change.iloc[0] = 0.0  # the first day only sets the starting value
    return pd.DataFrame({
        "ts": frame["date"] + pd.Timedelta(hours=IB_CLOSE_HOUR_UTC),
        "accountValue": frame["nav"].to_numpy(),
        "pnl": change.cumsum().to_numpy(),
    })


def wallet_performance(accounts: list, data_dir: Path | str) -> dict[str, pd.DataFrame]:
    """Each HL wallet's tokens on other chains: daily ``pnl_usd`` and ``base_usd`` (what the tokens held at the
    previous close earned from price moves, ``desk.venues.onchain_history``) with the value, as
    ``ts, accountValue, pnl, base`` where ``ts`` is the day's close. Deposits and withdrawals never count as
    performance, because a balance only earns from the day after it arrives."""
    from desk.overview import wallet_daily_value
    from desk.venues.onchain_history import load_returns

    histories = {}
    for account in accounts_for("hl", accounts):
        if not account.onchain:
            continue
        moves = load_returns(Path(data_dir), account.id)
        if moves.empty:
            continue
        value, _ = wallet_daily_value(Path(data_dir), account.id)
        close = moves.index + pd.Timedelta(days=1)
        # Value at each day's close is the next day's holdings base, so value and PnL come from the same balances
        # (the rebuilt value series can miss a token a day, which would be read as a flow); the last day uses
        # the wallet's value now.
        held = moves["base_usd"].shift(-1)
        held.iloc[-1] = value.iloc[-1]
        histories[account.wallet_name] = pd.DataFrame({
            "ts": close, "accountValue": held.to_numpy(), "pnl": moves["pnl_usd"].cumsum().to_numpy(),
            "base": moves["base_usd"].to_numpy()})
    return histories


def daily_points(history: pd.DataFrame) -> pd.DatetimeIndex:
    """UTC dates on which an account has at least one saved value."""
    return pd.DatetimeIndex(pd.to_datetime(history["ts"], utc=True).dt.normalize().unique())


def period_returns(index: pd.Series, rule: str, valid_days: pd.DatetimeIndex | None = None) -> pd.Series:
    """Returns between consecutive periods of a growth index; gaps are never one period.

    With ``valid_days``, a daily return needs both its day and the day before in that set,
    so a weekly-sampled account's move is never pinned on a single day.
    """
    closes = index.resample(rule).last().dropna()
    closes.index = closes.index.normalize()
    step = pd.Timedelta(days=1) if rule == "1D" else pd.Timedelta(days=7)
    returns = closes.pct_change(fill_method=None)
    keep = closes.index.to_series().diff().eq(step)
    if valid_days is not None:
        days = set(valid_days)
        keep &= closes.index.to_series().map(lambda day: day in days and day - step in days)
    return returns[keep].dropna()


# Chart legends sit in one row above the plot, under the title.
LEGEND_ON_TOP = {"legend": {"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0},
                 "margin": {"t": 110}}

RISK_NOTE = ("Current holdings (ex-ante) is the live risk: today's positions on every venue, HL spot tokens and "
             "on-chain wallet tokens (staking receipts priced as the token they stake), "
             "valued now and moved by each of the last 90 days' prices (Coverage is the share of gross exposure "
             "with a price history). Current regime is the desk's realized daily return since the book took its "
             f"present shape; Sharpe and alpha appear once it has {MIN_RATIO_DAYS} days. Nothing is pooled across "
             "regimes. VaR is one day's bootstrap loss on today's desk value; betas use 2,000 bootstrap resamples.")


class DeskAccounts:
    """Every account in accounts.json as one desk: current state, performance and risk."""

    def __init__(self, data_dir: str | Path = "data", wallet_data_dir: str | Path | None = None,
                 regime_start: str | pd.Timestamp | None = None):
        """``regime_start`` pins the start of the current regime; by default it is detected from
        the HL book's exposure history (see ``desk.hl.book_exposure.book_regimes``)."""
        self.data_dir = Path(data_dir)
        self.regime_start_override = pd.Timestamp(regime_start, tz="UTC") if regime_start else None
        self.regime_start: pd.Timestamp | None = self.regime_start_override
        self.regime_name = "set by hand" if regime_start else ""
        # Days the HL book traded (fills and priced spot changes), from the regime detection.
        self.hl_activity_days: pd.DatetimeIndex | None = None
        self.wallet_data_dir = Path(wallet_data_dir) if wallet_data_dir else ROOT_DATA
        self.accounts = load_accounts()
        self.overview = DeskOverview(self.accounts, data_dir=self.data_dir)
        self.histories: dict[str, pd.DataFrame] = {}
        self.excluded: list[str] = []
        # Accounts whose history starts where a data window starts (IB Flex keeps a year), as
        # opposed to HL wallets, whose history starts when they were funded.
        self.windowed: set[str] = set()
        self.notes: list[str] = []
        self.index = pd.Series(dtype=float)
        self.benchmarks = pd.DataFrame()
        self._highlights_state = None

    # ------------------------------------------------------------------
    def fetch_data(self, refresh_history: bool = True, record_ib_history: bool = True):
        """Current state of every account, plus each account's value and PnL history.

        ``refresh_history=False`` reuses the saved HL and IB Flex histories and the benchmark prices
        already loaded, so a frequent refresh (the dashboard) only re-reads current balances.
        """
        self.notes = []
        self._highlights_state = None  # a fresh fetch compares with the last saved run
        self.overview.fetch_data(record_ib_history=record_ib_history)
        if refresh_history:
            self._update_wallet_returns()
        self.histories, self.windowed = {}, set()
        self.ib_ids: dict[str, str] = {}  # history label ("IB 8440") -> full IB account ID
        for account in accounts_for("hl", self.accounts):
            try:
                history = (update_portfolio_history(account.id, self.wallet_data_dir) if refresh_history else
                           pd.read_csv(account_file(self.wallet_data_dir, "hl_portfolio_history", account.id)))
                history["ts"] = pd.to_datetime(history["ts"], utc=True, format="mixed")
                self.histories[account.name] = history[["ts", "accountValue", "pnl"]]
            except Exception as exc:
                self.notes.append(f"{account.name}: no value history ({type(exc).__name__}: {exc}).")
        for account in accounts_for("ib", self.accounts):
            if refresh_history:
                try:
                    from desk.venues import ib_flex

                    ib_flex.update_nav_history(account, ib_data_dir())
                except Exception as exc:
                    self.notes.append(f"{account.name}: IB Flex not refreshed ({str(exc) or type(exc).__name__}).")
            for path in sorted(ib_data_dir().glob("ib_nav_history_*.csv")):
                account_id = path.stem.rsplit('_', 1)[-1]
                # Shown as "IB 8440": only the last four characters of an account ID reach the screen.
                label = f"IB {short_id(account_id)}"
                self.ib_ids[label] = account_id
                nav = pd.read_csv(path)
                # Days before IB's Flex window, rebuilt from an activity statement (desk/venues/ib_statement.py).
                backfill = account_file(ib_data_dir(), "ib_nav_backfill", account_id)
                if backfill.exists() and len(nav):
                    earlier = pd.read_csv(backfill)
                    earlier = earlier[pd.to_datetime(earlier["date"]) < pd.to_datetime(nav["date"]).min()]
                    if len(earlier):
                        nav = pd.concat([earlier, nav], ignore_index=True)
                        self.notes.append(f"{label}: values before {nav['date'].iloc[len(earlier)]} are rebuilt from the "
                                          "activity statement's transactions at daily closes (IB's Flex history starts there).")
                self.histories[label] = ib_history(nav)
                self.windowed.add(label)
        self.excluded = self._drop_unreadable()
        if not self.histories:
            raise RuntimeError("No readable account has value history yet.")
        # The combined desk starts where every account's value is known. An HL wallet's history
        # starts when it was funded (it was worth nothing before), so it can join later as an
        # inflow; an IB history starts where the Flex window starts, which limits the desk start.
        firsts = {name: history["ts"].min() for name, history in self.histories.items()}
        start = max([firsts[name] for name in self.windowed] or [min(firsts.values())])
        clipped = []
        for history in self.histories.values():
            part = history[history["ts"] >= start].copy()
            # HL PnL is cumulative since the wallet opened; measure it from the first point in the
            # window, or the lifetime PnL so far would land as one day's loss or gain.
            if len(part):
                part["pnl"] = part["pnl"] - part["pnl"].iloc[0]
            clipped.append(part)
        self.index = time_weighted_index(clipped)
        self.index.index = pd.to_datetime(self.index.index, utc=True)
        self.common_start = start
        joined = [f"{name} ({first:%Y-%m-%d})" for name, first in firsts.items() if first > start + pd.Timedelta(days=1)]
        if joined:
            self.notes.append("Joined the desk after it starts, so their first funding counts as an inflow: "
                              + ", ".join(joined) + ".")
        # A day counts for daily returns when every account has a value that day, or did not exist yet.
        self.daily_days = None
        for name, history in self.histories.items():
            days = set(daily_points(history))
            if name not in self.windowed:
                first = pd.Timestamp(firsts[name]).normalize()
                days |= set(pd.date_range(start.normalize(), first - pd.Timedelta(days=1), freq="D", tz="UTC"))
            self.daily_days = days if self.daily_days is None else self.daily_days & days
        self.daily_days = pd.DatetimeIndex(sorted(self.daily_days or []))
        # Reload benchmarks when the desk start moves earlier (an account dropped out of the window).
        if refresh_history or self.benchmarks.empty or self.benchmarks.index.min() > start - pd.Timedelta(days=5):
            self.benchmarks = self._benchmarks(start - pd.Timedelta(days=10))
        if refresh_history or self.hl_activity_days is None:
            self._detect_regime()
        if any(account.venue == "hl" and account.onchain for account in self.accounts):
            self.notes.append("Wallet tokens on other chains are left out of returns (they have no deposit and "
                              "withdrawal record); their value over time is the dotted desk-value line, from saved "
                              "snapshots and the one-off rebuild (python -m desk.venues.onchain_history).")
        for note in self.notes:
            print(f"note: {note}")
        return self.index

    def _detect_regime(self):
        """Find where the HL book's current regime began (risk statistics start there) and the days it traded.

        A start pinned with ``regime_start`` is kept; the trading days are still read, for
        clipping idle stretches out of the value chart.
        """
        wallets = [account.id for account in accounts_for("hl", self.accounts)]
        pinned = self.regime_start_override is not None
        if not wallets:
            if not pinned:
                self.regime_start, self.regime_name = None, ""
            self.hl_activity_days = pd.DatetimeIndex([], tz="UTC")
            return
        from desk.hl import book_exposure

        values = []
        for account in accounts_for("hl", self.accounts):
            history = self.histories.get(account.name)
            if history is not None:
                values.append(history.set_index("ts")["accountValue"].resample("1D").last())
        try:
            equity = pd.concat(values, axis=1).ffill().fillna(0.0).sum(axis=1)
            daily, weekly, _ = book_exposure.book_regimes(wallets, equity)
            self.hl_activity_days = daily.attrs.get("activity_days", pd.DatetimeIndex([], tz="UTC"))
            if not pinned:
                self.regime_start, self.regime_name = book_exposure.current_regime(weekly)
        except Exception as exc:
            self.hl_activity_days = None
            if not pinned:
                self.regime_start, self.regime_name = None, ""
            self.notes.append(f"Current regime not detected ({type(exc).__name__}: {exc}); "
                              "realized statistics use the recent daily history.")

    def idle_periods(self, min_gap_days: int = 30) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
        """Stretches over ``min_gap_days`` in which no account in the desk history traded.

        HL wallets count as active on days they traded (fills or priced spot changes). IB keeps
        only about a week of executions, so an IB account counts as active on days its NAV
        changed (any PnL, deposit or withdrawal) plus its saved fill days: a flat NAV, as when
        it held nothing, is idle.
        Returns half-open (start, end) day ranges, as ``inactive_periods`` gives for a chart.
        """
        from desk.hl.trade_analytics import inactive_periods

        if self.hl_activity_days is None or self.index.empty:
            return []
        active = set(pd.DatetimeIndex(self.hl_activity_days).tz_convert("UTC").normalize())
        for label, history in self.histories.items():
            if not label.startswith("IB "):
                continue
            ordered = history.assign(ts=pd.to_datetime(history["ts"], utc=True)).sort_values("ts")
            moved = ordered["accountValue"].diff().abs().fillna(np.inf) > 0.005  # the first day counts
            active |= set(pd.DatetimeIndex(ordered.loc[moved, "ts"]).normalize())
            fills_path = account_file(ib_data_dir(), "ib_fills", getattr(self, "ib_ids", {}).get(label, label.split(" ", 1)[1]))
            fills = pd.read_csv(fills_path) if fills_path.exists() else pd.DataFrame()
            if len(fills) and "time" in fills:
                active |= set(pd.DatetimeIndex(pd.to_datetime(fills["time"], utc=True, errors="coerce").dropna()).normalize())
        start = pd.Timestamp(self.common_start).normalize()
        window = sorted(day for day in active if day >= start)
        return inactive_periods(pd.DatetimeIndex(window), min_gap_days)

    def _ib_closes(self, refs: list[str]) -> dict[str, pd.Series]:
        """Daily closes for IB positions by conId, over about six months (one TWS connection)."""
        ib = accounts_for("ib", self.accounts)
        if not ib:
            return {}
        from desk.venues import ib as ib_venue

        def read():
            from ib_async import Contract

            client = ib_venue.connect(ib[0], "analytics", readonly=True, timeout=20)
            closes = {}
            try:
                for ref in dict.fromkeys(refs):
                    try:
                        contract = ib_venue.qualify_for_history(client, Contract(conId=int(ref)))[0]
                        series = ib_venue.adjusted_daily_closes(client, contract, "6 M")
                        series.index = pd.to_datetime(series.index, utc=True).normalize()
                        closes[ref] = series
                    except Exception:
                        if getattr(client, "desk_history_down", False):
                            break
            finally:
                client.disconnect()
            return closes
        return ib_venue.run_off_event_loop(read)

    def _drop_unreadable(self) -> list[str]:
        """Leave accounts the overview could not read out of the combined history, as they are out of
        every current total: a saved IB history while TWS is down would otherwise still show IB in the
        charts and performance with stale values."""
        failed = {problem.split(":")[0].strip() for problem in self.overview.problems}
        dropped = []
        for account in self.accounts:
            if account.name not in failed:
                continue
            if account.venue == "hl":
                labels = [account.name] if account.name in self.histories else []
            else:  # IB histories are labelled by account number; an account without an ID owns them all
                labels = [label for label in self.histories if label.startswith(f"{account.venue.upper()} ")
                          and (not account.id or getattr(self, "ib_ids", {}).get(label, label[3:]) in
                               {account.id, short_id(account.id)})]
            for label in labels:
                self.histories.pop(label)
                self.windowed.discard(label)
                dropped.append(label)
        if dropped:
            self.notes.append(f"Left out of history, performance and risk because the account could not be read "
                              f"this time: {', '.join(dropped)}.")
        return dropped

    def _benchmarks(self, start: pd.Timestamp) -> pd.DataFrame:
        """Daily BTC and SPY closes for the comparison period."""
        frame = pd.DataFrame()
        try:
            candles = fetch_btc_candles(int(start.timestamp() * 1000),
                                        int(pd.Timestamp.now(tz="UTC").timestamp() * 1000), interval="1d")
            dates = pd.to_datetime(candles["t"], unit="ms", utc=True).dt.normalize()
            frame["BTC"] = pd.Series(candles["c"].astype(float).to_numpy(), index=dates)
        except Exception as exc:
            self.notes.append(f"BTC prices unavailable ({type(exc).__name__}).")
        spy, ib_problem = None, "no IB account configured"
        ib = accounts_for("ib", self.accounts)
        if ib:
            try:
                from desk.venues import ib as ib_venue

                def read():
                    if ib_venue.contract_lookups_down():
                        raise ib_venue.HistoryUnavailable("IB is not answering contract lookups")
                    client = ib_venue.connect(ib[0], "analytics", readonly=True, timeout=20)
                    try:
                        contract, _ = ib_venue.resolve_contract(client, ib_venue.ContractSpec("SPY"))
                        return ib_venue.adjusted_daily_closes(client, contract, "2 Y")
                    finally:
                        client.disconnect()
                spy = ib_venue.run_off_event_loop(read)
            except Exception as exc:
                spy, ib_problem = None, str(exc).split(";")[0].rstrip(".") or type(exc).__name__
        if spy is None and RESEARCH_PRICES.exists():
            prices = pd.read_csv(RESEARCH_PRICES, parse_dates=["date"]).set_index("date")
            if "SPY" in prices:
                spy = pd.to_numeric(prices["SPY"], errors="coerce").dropna()
                spy.index = pd.to_datetime(spy.index, utc=True).normalize()
                self.notes.append(f"SPY from the saved research prices, through {spy.index.max():%Y-%m-%d} "
                                  f"(IB: {ib_problem}).")
        if spy is not None:
            frame = frame.join(spy.rename("SPY"), how="outer") if len(frame) else spy.rename("SPY").to_frame()
        return frame.sort_index()

    def _require(self) -> bool:
        if self.index.empty:
            print("Call fetch_data() first.")
            return False
        return True

    # ------------------------------------------------------------------
    def show_accounts(self):
        self.overview.show_accounts()

    def show_exposure(self):
        self.overview.show_exposure()

    def show_positions(self):
        self.overview.show_positions()

    def show_risk(self):
        self.overview.show_risk()

    def account_pnl(self) -> pd.DataFrame:
        """Each account's PnL (net of deposits) and return over standard windows."""
        now = pd.Timestamp.now(tz="UTC")
        rows = []
        for name, history in self.histories.items():
            frame = history.set_index(pd.to_datetime(history["ts"], utc=True)).sort_index()
            row = {"Account": name, "Value": float(frame["accountValue"].iloc[-1])}
            index = time_weighted_index([history])
            index.index = pd.to_datetime(index.index, utc=True)
            for label, days in WINDOWS.items():
                cutoff = now - pd.Timedelta(days=days)
                before = through(frame, cutoff)
                if before.empty:
                    row[f"PnL {label}"] = row[f"Return {label}"] = np.nan
                    continue
                row[f"PnL {label}"] = float(frame["pnl"].iloc[-1] - before["pnl"].iloc[-1])
                prior = through(index, cutoff)
                row[f"Return {label}"] = float(index.iloc[-1] / prior.iloc[-1] - 1) if len(prior) else np.nan
            row[f"PnL {INCEPTION}"] = float(frame["pnl"].iloc[-1] - frame["pnl"].iloc[0])
            row[f"Return {INCEPTION}"] = float(index.iloc[-1] / index.iloc[0] - 1) if len(index) else np.nan
            row["Since"] = f"{index.index[0]:%Y-%m-%d}" if len(index) else ""  # the return since inception starts here
            rows.append(row)
        return pd.DataFrame(rows)

    def _update_wallet_returns(self) -> None:
        """Extend each wallet's daily price-move performance to yesterday (a few requests once a day)."""
        from desk.venues.onchain_history import update_wallet_returns

        for account in accounts_for("hl", self.accounts):
            if account.onchain:
                try:
                    update_wallet_returns(account, self.data_dir)
                except Exception as exc:  # the saved days stay; the row is just a day behind
                    self.notes.append(f"{account.wallet_name}: performance not extended ({type(exc).__name__}: {exc}).")

    def wallet_histories(self) -> dict[str, pd.DataFrame]:
        """Each HL wallet's tokens outside HL as ``ts, accountValue, pnl, base`` (see ``wallet_performance``)."""
        histories = wallet_performance(self.accounts, self.data_dir)
        if not histories and any(account.venue == "hl" and account.onchain for account in self.accounts):
            self.notes.append("Wallet tokens outside HL have no performance yet: run "
                              "python -m desk.venues.onchain_history --returns")
        return histories

    def wallet_pnl(self, histories: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """Wallet rows like ``account_pnl``: return is the compounded daily price move of the tokens held."""
        now = pd.Timestamp.now(tz="UTC")
        rows = []
        for name, history in histories.items():
            daily = (history["pnl"].diff().fillna(history["pnl"].iloc[0]) / history["base"].where(history["base"] > 0)).fillna(0.0)
            level = pd.Series((1 + daily.clip(lower=-1.0)).cumprod().to_numpy(), index=history["ts"])
            pnl = pd.Series(history["pnl"].to_numpy(), index=history["ts"])
            row = {"Account": name, "Value": float(history["accountValue"].iloc[-1])}
            for label, days in WINDOWS.items():
                cutoff = now - pd.Timedelta(days=days)
                before = through(level, cutoff)
                if before.empty:
                    row[f"PnL {label}"] = row[f"Return {label}"] = np.nan
                    continue
                row[f"PnL {label}"] = float(pnl.iloc[-1] - through(pnl, cutoff).iloc[-1])
                row[f"Return {label}"] = float(level.iloc[-1] / before.iloc[-1] - 1)
            row[f"PnL {INCEPTION}"] = float(pnl.iloc[-1])
            row[f"Return {INCEPTION}"] = float(level.iloc[-1] - 1)
            row["Since"] = f"{level.index[0] - pd.Timedelta(days=1):%Y-%m-%d}"  # the first day's return starts at the day before's close
            rows.append(row)
        return pd.DataFrame(rows)

    def performance_frame(self) -> pd.DataFrame:
        """Per-account PnL and return over each window: the accounts, the wallets' tokens outside HL, the
        combined HL and IB row (the headline return) and, last, the whole desk with the wallets."""
        trading = self.account_pnl()
        wallets_history = self.wallet_histories()
        wallets = self.wallet_pnl(wallets_history) if wallets_history else pd.DataFrame(columns=trading.columns)
        returns = self.desk_returns()

        def combined(name, parts, index_returns, since):
            row = {"Account": name, "Value": parts["Value"].sum(), "Since": since}
            for label in PERIODS:
                row[f"Return {label}"] = index_returns.get(label, np.nan)
                row[f"PnL {label}"] = parts[f"PnL {label}"].sum(min_count=1)
            return row

        # The headline return, as the page's first tile: since the first date every account has history.
        rows = [combined("Desk (HL + IB)", trading, {**returns, INCEPTION: returns["since start"]},
                         f"{self.common_start:%Y-%m-%d}")]
        if len(wallets):
            index = time_weighted_index(list(self.histories.values()) + list(wallets_history.values()))
            index.index = pd.to_datetime(index.index, utc=True)
            now = pd.Timestamp.now(tz="UTC")
            every = {INCEPTION: float(index.iloc[-1] / index.iloc[0] - 1) if len(index) else np.nan}
            for label, days in WINDOWS.items():
                prior = through(index, now - pd.Timedelta(days=days))
                every[label] = float(index.iloc[-1] / prior.iloc[-1] - 1) if len(prior) else np.nan
            rows.append(combined("Desk incl. wallets", pd.concat([trading, wallets]), every,
                                 f"{index.index[0]:%Y-%m-%d}" if len(index) else ""))
        table = pd.concat([trading, wallets, pd.DataFrame(rows)], ignore_index=True)
        return table[["Account", "Value"] + [f"{kind} {label}" for label in PERIODS for kind in ("PnL", "Return")] + ["Since"]]

    def show_strategies(self):
        """HL results by strategy: directional, conviction, hedge and the ladders' market making (desk/journal.py)."""
        from desk.journal import show_strategies

        show_strategies()

    def show_market_making(self):
        """The market-making ladders' fills, volume, spread captured and PnL (desk/market_making.py)."""
        from desk.market_making import show_market_making

        show_market_making()

    def show_performance(self):
        """Desk and per-account returns and PnL over 7, 30 and 90 days, a year and since inception."""
        if not self._require():
            return
        table = self.performance_frame()
        columns = list(table.columns)
        money = lambda value: "n/a" if not np.isfinite(value) else f"${value:,.2f}"
        percent = lambda value: "n/a" if not np.isfinite(value) else f"{value:.2%}"
        display_interactive_table(table[columns], bold_last=True, title="DESK PERFORMANCE (NET OF DEPOSITS)",
                                  key_columns={"Return 30D": "abs"},
                                  formatters={column: (percent if column.startswith("Return") else money)
                                              for column in columns[1:] if column != "Since"})
        since = float(self.index.iloc[-1] / self.index.iloc[0] - 1)
        print(f"Desk time-weighted return since {self.common_start:%Y-%m-%d} (first date every account has history): "
              f"{since:.2%}. PnL is each account's value change less deposits and withdrawals; transfers between "
              "desk accounts cancel in the combined return. Wallet tokens on other chains are not included.")

    def desk_returns(self) -> dict[str, float]:
        """Desk time-weighted return over each window and since the shared start."""
        now = pd.Timestamp.now(tz="UTC")
        returns = {"since start": float(self.index.iloc[-1] / self.index.iloc[0] - 1)}
        for label, days in WINDOWS.items():
            prior = through(self.index, now - pd.Timedelta(days=days))
            returns[label] = float(self.index.iloc[-1] / prior.iloc[-1] - 1) if len(prior) else np.nan
        return returns

    def money_weighted(self) -> dict[str, float]:
        """Dollar PnL and the money-weighted (Modified Dietz) return since the desk start.

        The time-weighted return in ``desk_returns`` chains each period's percentage, so it ignores
        how much money was in the desk; this one weighs every dollar by how long it was invested,
        so a loss on a small balance and a gain on a large one net out as they do in dollars.
        Both come from the same value and PnL histories over the same window. Deposits and
        withdrawals are the value changes that PnL does not explain; an account joining after the
        start enters as a deposit.
        """
        start = pd.Timestamp(self.common_start).normalize()
        values, pnls = [], []
        for history in self.histories.values():
            frame = history.assign(ts=pd.to_datetime(history["ts"], utc=True)).set_index("ts").sort_index()
            frame = frame[frame.index >= start]
            if frame.empty:
                continue
            daily = frame[["accountValue", "pnl"]].resample("1D").last()
            values.append(daily["accountValue"])
            pnls.append(daily["pnl"] - daily["pnl"].dropna().iloc[0])
        if not values:
            return {"pnl": np.nan, "return": np.nan, "flows": np.nan}
        # Before an account's first point it held nothing; after, its last value carries forward.
        value = pd.concat(values, axis=1).ffill().fillna(0.0).sum(axis=1)
        pnl = pd.concat(pnls, axis=1).ffill().fillna(0.0).sum(axis=1)
        flows = (value.diff() - pnl.diff()).fillna(0.0)
        days = (value.index[-1] - value.index[0]).days or 1
        weight = (value.index[-1] - value.index).days / days  # share of the period each flow was invested
        capital = float(value.iloc[0] + (flows * weight).sum())
        total = float(pnl.iloc[-1])
        return {"pnl": total, "return": total / capital if capital > 0 else np.nan, "flows": float(flows.sum())}

    def show_highlights(self):
        """Desk performance and risk highlights plus the overview's current-state ones, with changes since the last run."""
        if not self._require():
            return
        well, attention, metrics, items = self.highlight_rows()
        if self._highlights_state is None:
            self._highlights_state = HighlightsState(self.data_dir / ACCOUNTS_HIGHLIGHTS_STATE)
        since = self._highlights_state.compare(metrics, items)
        show_highlights(well, attention, since,
                        footnote="Returns are time-weighted and net of deposits and withdrawals. Betas here are "
                                 "simple weekly estimates; the risk metrics section has bootstrap ranges.")

    def highlight_rows(self) -> tuple[list, list, dict, dict]:
        """What stands out and needs attention (performance, risk and current state), and the key numbers."""
        well, attention, metrics, items = self.overview.highlight_rows() if self.overview.snapshots else ([], [], {}, {})
        returns = self.desk_returns()
        text = (f"{percent(returns['since start'], signed=True)} since {self.common_start:%Y-%m-%d}; "
                + ", ".join(f"{label} {percent(returns[label], signed=True)}" for label in WINDOWS) + ".")
        (well if returns.get("30D", 0) >= 0 else attention).insert(0, ("Desk return (net of deposits)", text))
        table = self.account_pnl()
        recent = table.dropna(subset=["Return 30D"]) if "Return 30D" in table else table.iloc[0:0]
        if len(recent) > 1:
            best, worst = recent.loc[recent["Return 30D"].idxmax()], recent.loc[recent["Return 30D"].idxmin()]
            well.insert(1, ("Best account, 30D", f"{best['Account']} {percent(best['Return 30D'], signed=True)} "
                                                 f"({signed_money(best['PnL 30D'])})."))
            if worst["Return 30D"] < 0:
                attention.insert(1, ("Worst account, 30D", f"{worst['Account']} {percent(worst['Return 30D'], signed=True)} "
                                                           f"({signed_money(worst['PnL 30D'])})."))
        drawdown = float(self.index.iloc[-1] / self.index.cummax().iloc[-1] - 1)
        peak_day = self.index.idxmax()
        text = f"{percent(drawdown)} below the peak of {peak_day:%Y-%m-%d}." if drawdown < 0 else "At its peak."
        (attention if drawdown <= DEEP_DRAWDOWN else well).append(("Drawdown now", text))
        sharpe = beta_spy = beta_btc = np.nan
        risk = getattr(self, "risk_table", None)
        if risk is not None and len(risk):
            by_basis = risk.set_index("Basis")
            current = by_basis[by_basis.index.str.startswith("Current holdings")]
            regime = by_basis[by_basis.index.str.contains("realized")]
            parts = []
            if len(current):
                row = current.iloc[0]
                beta_btc, beta_spy = row.get("Beta BTC", np.nan), row.get("Beta SPY", np.nan)
                betas = "".join(f"; beta {name} {value:.2f}" for name, value in (("BTC", beta_btc), ("SPY", beta_spy))
                                if pd.notna(value))
                if pd.notna(row.get("Volatility (/yr)", np.nan)):
                    parts.append(f"Current book: volatility {row['Volatility (/yr)']:.0%}/yr, 1-day VaR 95% "
                                 f"{signed_money(-row.get('VaR 95%', np.nan)) if pd.notna(row.get('VaR 95%', np.nan)) else 'n/a'}"
                                 + betas)
            if len(regime):
                row = regime.iloc[0]
                sharpe = row.get("Sharpe", np.nan)
                if pd.notna(sharpe):
                    parts.append(f"since {row['From']}: Sharpe {sharpe:.2f} over {int(row['Periods'])} days")
                else:
                    parts.append(f"current regime {int(row['Periods'])} days old, too short for a Sharpe "
                                 f"(needs {MIN_RATIO_DAYS})")
            if parts:
                text = "; ".join(parts) + "."
                (well if not pd.notna(sharpe) or sharpe > 0 else attention).append(("Risk-adjusted", text))
        for note in self.notes:
            if "not refreshed" in note or ": no value history" in note:
                attention.append(("History", note))
        metrics.update({"Desk return since start": (returns["since start"], "pct"),
                        "Desk return 30D": (returns.get("30D", np.nan), "pct"),
                        "Drawdown now": (drawdown, "pct"), "Sharpe (current regime)": (sharpe, "num"),
                        "Beta SPY (current book)": (beta_spy, "num"), "Beta BTC (current book)": (beta_btc, "num")})
        try:  # alt season or equity sell-off forming (desk/market_regime.py; read at most every 6 hours)
            from desk import market_regime

            for row in reversed(market_regime.notices(market_regime.current())):
                attention.insert(0, row)
        except Exception as exc:
            self.notes.append(f"Market regime not read ({type(exc).__name__}: {exc}).")
        return well, attention, metrics, items

    def risk_metrics(self) -> pd.DataFrame:
        """Risk of the book as it is now, then realized statistics for the current regime.

        * Current holdings (ex-ante): today's positions on every venue, plus HL spot tokens and
          on-chain wallet tokens, times each of the last 90 days' price moves. This is the live
          risk of what is held, on the same desk equity that includes those wallets.
        * Current regime (realized): the desk's own daily returns since the current regime began.
          Sharpe and alpha are left out until it has MIN_RATIO_DAYS days: a shorter stretch is an episode.

        There is no whole-history row: a statistic pooled across regimes averages books the desk
        no longer holds.
        """
        from desk import live_risk

        equity = self.overview.desk_equity
        rows = []
        try:
            wallets = [account.id for account in accounts_for("hl", self.accounts)
                       if account.name not in self.excluded]
            spot, unpriced = live_risk.hl_spot_values(wallets)
            # On-chain wallet tokens (staked VVV and the like) are part of desk equity, so their
            # risk is too.
            mids, pairs = live_risk.hl_markets()
            spot += live_risk.wallet_token_holdings(self.overview._wallets(), mids, pairs)
            holdings = live_risk.position_values(self.overview._trading(), spot)
            if len(holdings):
                returns, missing = live_risk.holding_returns(holdings, live_risk.EX_ANTE_DAYS, self._ib_closes)
                # Tokens with no HL price at all have no value to weigh, so they are named, not covered.
                rows.append(live_risk.ex_ante_row(holdings, returns, equity, self.benchmarks, missing + unpriced))
        except Exception as exc:
            self.notes.append(f"Ex-ante risk unavailable ({type(exc).__name__}: {exc}).")
        daily = period_returns(self.index, "1D", self.daily_days)
        realized = []
        if self.regime_start is not None:
            name = f"Current regime: {self.regime_name}" if self.regime_name else "Current regime"
            realized.append((f"{name} (realized, daily)", daily[daily.index >= self.regime_start], 365))
        else:
            realized.append(("Recent days (realized, daily; regime not detected)", daily, 365))
        for label, returns, periods in realized:
            if len(returns) < 8:
                continue
            path = (1 + returns).cumprod()
            long_enough = len(returns) >= live_risk.MIN_RATIO_DAYS
            row = {"Basis": label, "Periods": len(returns),
                   "From": f"{returns.index.min():%Y-%m-%d}", "To": f"{returns.index.max():%Y-%m-%d}",
                   "Return (total)": float(path.iloc[-1] - 1),
                   "Volatility (/yr)": float(returns.std() * math.sqrt(periods)),
                   "Sharpe": (float(returns.mean() / returns.std() * math.sqrt(periods))
                              if long_enough and returns.std() > 0 else np.nan),
                   "Max drawdown": float((path / path.cummax() - 1).min())}
            if not long_enough:
                row["Note"] = (f"{len(returns)} of {live_risk.MIN_RATIO_DAYS} days: too short for Sharpe or alpha")
            if len(returns) >= 20:
                for confidence in (0.95, 0.99):
                    var = bootstrap_var(returns, confidence)
                    row[f"VaR {confidence:.0%}"] = var["var"] * equity
            for name in ("BTC", "SPY"):
                if name in self.benchmarks:
                    pair = live_risk.paired_returns(returns, self.benchmarks[name])
                    if len(pair) < 8:
                        continue
                    stats = bootstrap_btc_regression(pair["book"], pair["bench"], periods_per_year=periods, samples=2000)
                    row[f"Beta {name}"] = stats["beta"]
                    row[f"Beta {name} 90% range"] = (f"{stats['beta_low']:.2f} to {stats['beta_high']:.2f}"
                                                     if np.isfinite(stats.get("beta_low", np.nan)) else "n/a")
                    if long_enough:
                        row[f"Alpha vs {name} (/yr)"] = stats["alpha"] / 100
            rows.append(row)
        table = pd.DataFrame(rows)
        self.risk_table = table
        return table

    def show_risk_metrics(self):
        if not self._require():
            return
        table = self.risk_metrics()
        if table.empty:
            print("Not enough shared history for risk statistics yet.")
            return
        formats = {"Return (total)": "{:.1%}", "Volatility (/yr)": "{:.1%}", "Sharpe": "{:.2f}", "Max drawdown": "{:.1%}",
                   "VaR 95%": "${:,.2f}", "VaR 99%": "${:,.2f}", "Beta BTC": "{:.2f}", "Beta SPY": "{:.2f}",
                   "Alpha vs BTC (/yr)": "{:.1%}", "Alpha vs SPY (/yr)": "{:.1%}", "Periods": "{:.0f}"}
        formats["Coverage"] = "{:.0%}"
        basis = table.set_index("Basis")
        styler = basis.style.format({k: v for k, v in formats.items() if k in table}, na_rep="")
        # Dollar VaR and the BTC beta are what the risk row is read for.
        display(emphasis.style_frame(styler, basis, {"VaR 95%": None, "Beta BTC": None}, title="DESK RISK METRICS"))
        print(RISK_NOTE)
        self.risk_table = table

    def plot_value(self):
        """Desk value stacked by account, and the desk return against BTC and SPY."""
        if not self._require():
            return
        figure = go.Figure()
        daily = {name: history.set_index(pd.to_datetime(history["ts"], utc=True))["accountValue"].sort_index()
                 .resample("1D").last().ffill() for name, history in self.histories.items()}
        # Each layer runs to the last day of any account: a stacked layer with no point reads as $0,
        # and IB's Flex NAV stops at the last completed session. A layer starts at its own first day.
        last_day = max(series.index.max() for series in daily.values())
        for name, series in daily.items():
            series = series.reindex(pd.date_range(series.index.min(), last_day, freq="D")).ffill()
            figure.add_trace(go.Scatter(x=series.index, y=series, name=name, stackgroup="value", mode="lines"))
        # Collapse stretches in which no account traded, as the dashboard and HL account charts do.
        from desk.hl.account_analytics import clip_inactive_periods

        clip_inactive_periods(figure, self.idle_periods())
        figure.update_layout(title="Desk value by account (USD; wallet tokens excluded)", template="plotly_dark",
                             hovermode="x unified", yaxis_tickprefix="$", legend=LEGEND_ON_TOP["legend"],
                             margin={**LEGEND_ON_TOP["margin"], "b": 90})
        show_chart(figure)
        index = self.index.resample("1D").last().dropna()
        index.index = index.index.normalize()
        comparison = go.Figure(go.Scatter(x=index.index, y=100 * index / index.iloc[0], name="Desk (net of deposits)"))
        for name, color in (("BTC", "#F7931A"), ("SPY", "#16a34a")):
            if name in self.benchmarks:
                prices = self.benchmarks[name].dropna()
                prices = prices[prices.index >= index.index[0]]
                if len(prices):
                    comparison.add_trace(go.Scatter(x=prices.index, y=100 * prices / prices.iloc[0], name=name,
                                                    line={"color": color}))
        comparison.update_layout(title=f"Desk time-weighted return vs BTC and SPY (start = 100, from {index.index[0]:%Y-%m-%d})",
                                 template="plotly_dark", hovermode="x unified", **LEGEND_ON_TOP)
        show_chart(comparison)