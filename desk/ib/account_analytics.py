"""IB account analytics: the IB counterpart of the HL account notebook.

Reads one IB account through a read-only TWS connection: balances, today's PnL, open
positions with ATR stop/target levels, and the risk of the current book (beta to SPY,
Sharpe, volatility, bootstrap VaR, drawdown) from a year of IB daily prices. IB's API has
no account-value history, so book statistics price today's holdings over the past year,
and real account history accumulates from the snapshots saved on each run.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from IPython.display import display

from desk.accounts import Account, accounts_for, ib_data_dir, load_accounts, short_id
from desk.privacy import account_file
from desk import emphasis
from desk.hl.account_analytics import display_interactive_table
from desk.hl.trade_analytics import bootstrap_btc_regression, bootstrap_var, wilder_atr
from desk.notebook_highlights import HighlightsState, signed_money
from desk.notebook_highlights import show as show_highlights
from desk.overview import merge_ib_fills, save_ib_history
from desk.risk import ATR_PERIOD, REWARD_RISK_RATIO, STOP_ATR_MULTIPLE, ib_position_risk
from desk.venues import ib as ib_venue
from desk.charts import show as show_chart
from desk.numbers import DISPLAY_TIMEZONE, clock

BENCHMARK = "SPY"
TRADING_DAYS = 252
BETA_LOOKBACK_DAYS = 60
HIGHLIGHT_MOVERS = 3
CONCENTRATION_PCT = 0.25   # one position above this share of the account is flagged
ACCOUNT_RISK_PCT = 0.20    # open risk above this share of the account is flagged (the desk cap's level)
SUMMARY_TAGS = ("NetLiquidation", "TotalCashValue", "BuyingPower", "GrossPositionValue",
                "InitMarginReq", "MaintMarginReq", "AvailableFunds", "ExcessLiquidity")


def _money(value):
    from desk.numbers import usd

    return usd(value)


def _percent(value):
    return "n/a" if value is None or not np.isfinite(value) else f"{value:.2%}"


def _number(value):
    return "n/a" if value is None or not np.isfinite(value) else f"{value:,.6g}"


def _neutral(row_index, column, value, row):
    return "color:inherit;"


def _pnl_style(row_index, column, value, row):
    if column == "Side" and value in ("LONG", "SHORT"):
        return "color:#188038;" if value == "LONG" else "color:#c62828;"
    signed = "PnL" in column or column in ("24h change (%)", "Value")
    if isinstance(value, (int, float)) and np.isfinite(value) and signed:
        return "color:#188038;" if value > 0 else "color:#c62828;" if value < 0 else "color:inherit;"
    return "color:inherit;"


def _mover(row) -> str:
    share = row["uPnL (%)"]
    return f"{row['Symbol']} {signed_money(row['uPnL'])}" + (f" ({share:.1%})" if np.isfinite(share) else "")


def _returns(bars: pd.DataFrame) -> pd.Series:
    frame = bars.assign(date=pd.to_datetime(bars["date"]).dt.tz_localize(None)).set_index("date")
    return frame["close"].pct_change().dropna()


def book_returns(values: dict[str, float], returns: dict[str, pd.Series], equity: float) -> tuple[pd.Series, list[str]]:
    """Daily returns of the account if it had held today's positions over the sample.

    Each day's PnL is the sum of current position values times that symbol's return, over
    account value (so cash dilutes it). A symbol without a price that day contributes 0.
    """
    notes = []
    priced = {symbol: series for symbol, series in returns.items() if symbol in values and len(series)}
    missing = [symbol for symbol in values if symbol not in priced]
    if missing:
        notes.append(f"no price history for {', '.join(missing)}; left out of the book returns")
    if not priced or equity <= 0:
        return pd.Series(dtype=float), notes
    frame = pd.DataFrame(priced).sort_index()
    short_history = [symbol for symbol in frame if frame[symbol].notna().sum() < len(frame) * 0.9]
    if short_history:
        notes.append(f"shorter price history for {', '.join(short_history)}; missing days count as 0")
    pnl = frame.fillna(0.0).mul(pd.Series({symbol: values[symbol] for symbol in frame}), axis=1).sum(axis=1)
    return pnl / equity, notes


def intraday_return(values: dict[str, float], last_closes: dict[str, float], prices: dict[str, float],
                    equity: float) -> float:
    """Today's return of the current holdings since each symbol's last completed close.

    Weighted like ``book_returns`` (position value over account value); symbols without a last
    close or a current price are left out. NaN when nothing can be priced.
    """
    pnl, priced = 0.0, 0
    for symbol, value in values.items():
        close, price = last_closes.get(symbol), prices.get(symbol)
        if close and price and close > 0 and price > 0:
            pnl += value * (price / close - 1)
            priced += 1
    return pnl / equity if priced and equity > 0 else float("nan")


def max_drawdown(returns: pd.Series) -> float:
    if returns.empty:
        return float("nan")
    path = (1 + returns).cumprod()
    return float((path / path.cummax() - 1).min())


def simple_beta(returns: pd.Series, benchmark: pd.Series) -> float:
    pairs = pd.concat([returns, benchmark], axis=1).dropna()
    if len(pairs) < 10 or pairs.iloc[:, 1].var() <= 0:
        return float("nan")
    return float(pairs.cov().iloc[0, 1] / pairs.iloc[:, 1].var())


class IBAccountAnalytics:
    """Report for one IB account, mirroring the HL account notebook's sections."""

    def __init__(self, account: str | None = None, data_dir: str | Path | None = None, port: int | None = None):
        configured = accounts_for("ib", load_accounts())
        chosen = next((item for item in configured if account in (None, "", item.id, item.label)), None)
        if chosen is None:
            chosen = Account("ib", account or "")
        if port:
            chosen = Account(chosen.venue, chosen.id, chosen.label, chosen.host, port)
        self.account = chosen
        # IB records live outside the repository unless a folder is given explicitly.
        self.data_dir = Path(data_dir) if data_dir is not None else ib_data_dir()
        self.snapshot = None
        self.summary: dict[str, float] = {}
        self.pnl: dict[str, float] = {}
        self.bars: dict[str, pd.DataFrame] = {}
        self.benchmark = pd.Series(dtype=float)
        self.positions = pd.DataFrame()
        self.fills = pd.DataFrame()
        self.history = pd.DataFrame()
        self.book = pd.Series(dtype=float)
        # The book chart's last point, from live prices since the last completed close (or None).
        self.live: dict | None = None
        self.benchmark_close = float("nan")
        self.notes: list[str] = []
        self._highlights_state = None

    # ------------------------------------------------------------------
    def fetch_data(self):
        """Read the account from TWS (read-only), save its history, and price the book."""
        snapshot = ib_venue.run_off_event_loop(self._fetch_data)
        for note in self.notes:
            print(f"note: {note}")
        return snapshot

    def _fetch_data(self):
        self.notes = []
        self._highlights_state = None  # a fresh fetch compares with the last saved run
        self.data_dir.mkdir(parents=True, exist_ok=True)
        ib = ib_venue.connect(self.account, "analytics", readonly=True, timeout=20)
        try:
            snapshot = ib_venue.snapshot_from_ib(ib, self.account)
            acct = snapshot.account
            base, usd_per = ib_venue.fx_rates_to_usd(ib.accountValues(acct))
            to_usd = usd_per[base]
            for item in ib.accountSummary(acct):
                if item.tag in SUMMARY_TAGS and ib_venue.is_set(item.value):
                    self.summary[item.tag] = float(item.value) * to_usd
            self.pnl = self._daily_pnl(ib, acct, to_usd)
            self.bars = {}
            from ib_async import Contract

            skipped, outage = [], ""
            for position in snapshot.positions:
                try:
                    contract = Contract(conId=int(position.ref))
                    ib_venue.qualify_for_history(ib, contract)
                    self.bars[position.symbol] = ib_venue.daily_bars(ib, contract, "1 Y")
                except ib_venue.HistoryUnavailable as exc:
                    skipped.append(position.symbol)
                    outage = outage or str(exc)
                except Exception as exc:  # one symbol's history never blocks the report
                    self.notes.append(f"{position.symbol}: no daily history ({str(exc) or type(exc).__name__})")
            try:
                spy, _ = ib_venue.resolve_contract(ib, ib_venue.ContractSpec(BENCHMARK))
                spy_bars = ib_venue.daily_bars(ib, spy, "1 Y")
                self.benchmark = _returns(spy_bars)
                self.benchmark_close = float(spy_bars["close"].iloc[-1]) if len(spy_bars) else float("nan")
            except ib_venue.HistoryUnavailable as exc:
                skipped.append(BENCHMARK)
                outage = outage or str(exc)
            except Exception as exc:
                self.notes.append(f"{BENCHMARK} benchmark unavailable ({str(exc) or type(exc).__name__})")
            if skipped:
                self.notes.append(f"{outage.split(';')[0]}. No daily history for {', '.join(skipped)}: ATR stops, "
                                  "1D change, beta, VaR and the book chart are unavailable this run.")
            self.fills = merge_ib_fills(ib_venue.recent_fills(ib, acct), self.data_dir, acct)
        finally:
            ib.disconnect()
        self.snapshot = snapshot
        save_ib_history(snapshot, self.data_dir)
        history_path = account_file(self.data_dir, "ib_account_history", acct)
        self.history = pd.read_csv(history_path) if history_path.exists() else pd.DataFrame()
        self.positions = self._position_table()
        values = {position.symbol: position.notional_usd for position in snapshot.positions}
        returns = {symbol: _returns(bars) for symbol, bars in self.bars.items() if len(bars) > 1}
        self.book, notes = book_returns(values, returns, snapshot.equity_usd)
        self.notes.extend(notes)
        self.live = self._live_point(snapshot, values)
        return snapshot

    def _live_point(self, snapshot, values: dict[str, float]) -> dict | None:
        """Today's move of the current holdings and SPY since the last completed close.

        IB's daily bars stop at the last completed session, so the chart would end there. Prices
        come from Yahoo Finance's free real-time quotes (IB's own mark if Yahoo has none). None
        when nothing is newer than the last close (weekend, holiday, before the open).
        """
        if self.book.empty:
            return None
        from desk.venues import yahoo

        last_day = pd.Timestamp(self.book.index.max()).normalize()
        closes = {symbol: float(bars["close"].iloc[-1]) for symbol, bars in self.bars.items() if len(bars)}
        prices, times, sources = {}, [], set()
        for position in snapshot.positions:
            try:
                quote = yahoo.last_price(position.symbol)
                if quote.currency != position.currency:
                    raise ValueError("currency differs")
                prices[position.symbol] = quote.price
                times.append(quote.time)
                sources.add("Yahoo")
            except Exception:  # Yahoo lacks it: IB's own mark (may be delayed without a data subscription)
                if np.isfinite(position.mark_price) and position.mark_price > 0:
                    prices[position.symbol] = position.mark_price
                    sources.add("IB mark")
        if not times:
            return None
        newest = max(times).tz_convert(ib_venue.ACCOUNTING_TIMEZONE)
        if newest.tz_localize(None).normalize() <= last_day:
            return None  # nothing traded since the last completed close
        book = intraday_return(values, closes, prices, snapshot.equity_usd)
        benchmark = float("nan")
        try:
            spy = yahoo.last_price(BENCHMARK)
            if spy.time.tz_convert(ib_venue.ACCOUNTING_TIMEZONE).tz_localize(None).normalize() > last_day and self.benchmark_close > 0:
                benchmark = spy.price / self.benchmark_close - 1
        except Exception:
            pass
        if not np.isfinite(book):
            return None
        return {"time": newest, "book": book, "benchmark": benchmark, "source": " and ".join(sorted(sources))}

    def _daily_pnl(self, ib, acct: str, to_usd: float) -> dict[str, float]:
        """Today's PnL from IB's account PnL subscription (empty if TWS does not send it)."""
        try:
            pnl = ib.reqPnL(acct)
            ib.sleep(2)
            values = {name: float(getattr(pnl, name)) * to_usd for name in ("dailyPnL", "unrealizedPnL", "realizedPnL")
                      if ib_venue.is_set(getattr(pnl, name, None))}
            ib.cancelPnL(acct)
            return values
        except Exception as exc:
            self.notes.append(f"today's PnL unavailable ({str(exc) or type(exc).__name__})")
            return {}

    def _position_table(self) -> pd.DataFrame:
        equity = self.snapshot.equity_usd
        rows = []
        try:  # the 24-hour move, as on the desk dashboard (Yahoo); the move since the last close if not
            from desk.day_change import position_changes

            moves = position_changes([self.snapshot])
        except Exception:
            moves = {}
        for position in self.snapshot.positions:
            bars = self.bars.get(position.symbol)
            atr = float("nan")
            change = float("nan")
            beta = float("nan")
            if bars is not None and len(bars) > ATR_PERIOD:
                try:
                    atr = wilder_atr(bars, ATR_PERIOD)
                except ValueError:
                    pass
                closes = bars["close"].to_numpy()
                change = position.mark_price / closes[-1] - 1 if closes[-1] else float("nan")
                beta = simple_beta(_returns(bars).tail(BETA_LOOKBACK_DAYS), self.benchmark)
            risk, basis = ib_position_risk(position, atr if np.isfinite(atr) else None)
            direction = 1 if position.quantity > 0 else -1
            distance = atr * STOP_ATR_MULTIPLE
            cost = position.avg_price * abs(position.quantity) * position.multiplier * position.fx_to_usd
            rows.append({
                "Symbol": position.symbol, "Class": position.asset_class, "Side": position.side,
                "Quantity": position.quantity, "Avg cost": position.avg_price, "Mark": position.mark_price,
                "Value": position.notional_usd, "% of account": abs(position.notional_usd) / equity if equity else np.nan,
                "uPnL": position.unrealized_usd,
                "uPnL (%)": position.unrealized_usd / cost if cost else np.nan,
                "24h change (%)": moves.get((self.snapshot.label, position.symbol), change),
                f"ATR({ATR_PERIOD})": atr,
                "Stop": position.mark_price - direction * distance if np.isfinite(atr) else np.nan,
                "Target": position.mark_price + direction * distance * REWARD_RISK_RATIO if np.isfinite(atr) else np.nan,
                "Risk to stop": risk, "Risk basis": basis,
                f"Beta {BETA_LOOKBACK_DAYS}d": beta,
            })
        return pd.DataFrame(rows)

    def _require(self) -> bool:
        if self.snapshot is None:
            print("Call fetch_data() first.")
            return False
        return True

    # ------------------------------------------------------------------
    def show_highlights(self):
        """What stands out and what needs attention now, and what changed since the last run."""
        if not self._require():
            return
        snap, well, attention = self.snapshot, [], []
        equity = snap.equity_usd
        today = self.pnl.get("dailyPnL", np.nan)
        text = f"Account value {_money(equity)}, {len(self.positions)} open positions"
        if np.isfinite(today):
            text += f"; today {signed_money(today)} ({today / (equity - today):.2%})" if equity - today else ""
        (well if not np.isfinite(today) or today >= 0 else attention).append(("Account", text + "."))
        positions = self.positions
        if not positions.empty:
            unrealized = float(positions["uPnL"].sum())
            (well if unrealized >= 0 else attention).append(
                ("Unrealized PnL", f"{signed_money(unrealized)} across open positions."))
            ranked = positions.sort_values("uPnL")
            winners = ranked[ranked["uPnL"] > 0].tail(HIGHLIGHT_MOVERS).iloc[::-1]
            losers = ranked[ranked["uPnL"] < 0].head(HIGHLIGHT_MOVERS)
            if len(winners):
                well.append(("Largest winners", ", ".join(
                    _mover(row) for _, row in winners.iterrows()) + "."))
            if len(losers):
                attention.append(("Largest losers", ", ".join(
                    _mover(row) for _, row in losers.iterrows()) + "."))
            moves = positions.dropna(subset=["24h change (%)"])
            if len(moves):
                moves = moves.reindex(moves["24h change (%)"].abs().sort_values(ascending=False).index).head(HIGHLIGHT_MOVERS)
                well.append(("Biggest moves since last close", ", ".join(
                    f"{row['Symbol']} {row['24h change (%)']:.1%}" for _, row in moves.iterrows()) + "."))
            top = positions.loc[positions["% of account"].idxmax()]
            if top["% of account"] > CONCENTRATION_PCT:
                attention.append(("Concentration", f"{top['Symbol']} {top['Side'].lower()} is "
                                                   f"{top['% of account']:.0%} of the account."))
            risk = float(positions["Risk to stop"].sum())
            text = f"{_money(risk)} to ATR stops ({risk / equity:.1%} of the account)." if equity else _money(risk)
            (attention if equity and risk / equity > ACCOUNT_RISK_PCT else well).append(("Open risk", text))
            unpriced = positions.loc[positions["Risk basis"].str.contains("no price history", na=False), "Symbol"]
            if len(unpriced):
                attention.append(("No price history", ", ".join(unpriced) + ": risk uses a fallback estimate."))
        stats = bootstrap_btc_regression(self.book, self.benchmark, periods_per_year=TRADING_DAYS, samples=500)
        var95 = bootstrap_var(self.book, 0.95) if len(self.book) >= 20 else None
        if np.isfinite(stats.get("beta", np.nan)):
            text = f"Beta to {BENCHMARK} {stats['beta']:.2f}"
            if var95:
                text += f"; 1-day VaR 95% {_money(var95['var'] * equity)}"
            well.append(("Current book", text + " (today's holdings priced over the past year)."))
        realized_7d = np.nan
        if not self.fills.empty:
            fills = self.fills.assign(time=pd.to_datetime(self.fills["time"], utc=True, format="mixed"))
            week = fills[fills["time"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)]
            if len(week):
                realized_7d = float(week["realized_pnl"].sum())
                (well if realized_7d >= 0 else attention).append(
                    ("Last 7 days of fills", f"{len(week)} fills, realized {signed_money(realized_7d)} after commission."))
        for note in self.notes:
            attention.append(("Data", note))
        metrics = {"Account value": (equity, "usd"), "Unrealized PnL": (snap.unrealized_usd, "usd"),
                   "Open risk to stops": (float(positions["Risk to stop"].sum()) if not positions.empty else 0.0, "usd"),
                   f"Beta to {BENCHMARK}": (stats.get("beta", np.nan), "num"),
                   "Gross position value": (snap.gross_usd, "usd")}
        items = {"Positions": [f"{row['Symbol']} {row['Side'].lower()}" for _, row in positions.iterrows()]}
        if self._highlights_state is None:
            self._highlights_state = HighlightsState(account_file(self.data_dir, "ib_highlights_state", snap.account, ".json"))
        since = self._highlights_state.compare(metrics, items)
        show_highlights(well, attention, since,
                        footnote="Changes in account value include deposits and withdrawals.")

    def show_account(self):
        """Balances, today's PnL, and current-book risk statistics."""
        if not self._require():
            return
        snap, summary = self.snapshot, self.summary
        equity = snap.equity_usd
        stats = bootstrap_btc_regression(self.book, self.benchmark, periods_per_year=TRADING_DAYS)
        var95 = var99 = None
        if len(self.book) >= 20:
            var95, var99 = bootstrap_var(self.book, 0.95), bootstrap_var(self.book, 0.99)
        risk_total = float(self.positions["Risk to stop"].sum()) if not self.positions.empty else 0.0

        def ranged(key, formatter):
            if not np.isfinite(stats.get(key, np.nan)):
                return "n/a"
            return f"{formatter(stats[key])} (90% range {formatter(stats[key + '_low'])} to {formatter(stats[key + '_high'])})"

        def var_text(result):
            if result is None:
                return "n/a (needs 20 days)"
            return (f"{_money(result['var'] * equity)} ({result['var']:.2%}; 90% range "
                    f"{_money(result['low'] * equity)}-{_money(result['high'] * equity)})")

        rows = [
            # IB calls this net liquidation value: cash plus positions at market, what the account is worth.
            ("Account value", _money(equity)),
            ("Cash", _money(summary.get("TotalCashValue", snap.cash_usd))),
            ("Buying power", _money(summary.get("BuyingPower", np.nan))),
            ("Gross position value", _money(summary.get("GrossPositionValue", snap.gross_usd))),
            ("Initial / maintenance margin", f"{_money(summary.get('InitMarginReq', np.nan))} / "
                                            f"{_money(summary.get('MaintMarginReq', np.nan))}"),
            ("Available funds", _money(summary.get("AvailableFunds", snap.available_usd))),
            ("Unrealized PnL", _money(snap.unrealized_usd)),
            ("Today's PnL", _money(self.pnl.get("dailyPnL", np.nan))),
            ("Realized PnL today", _money(self.pnl.get("realizedPnL", np.nan))),
            ("--- RISK (current book) ---", ""),
            ("Open risk to ATR stops", f"{_money(risk_total)} ({risk_total / equity:.2%} of account)" if equity else "n/a"),
            (f"Beta to {BENCHMARK}", ranged("beta", lambda value: f"{value:.2f}")),
            (f"Alpha vs {BENCHMARK} / year", ranged("alpha", lambda value: f"{value:.2f}%")),
            ("Annualized Sharpe", ranged("sharpe", lambda value: f"{value:.2f}")),
            ("Volatility", f"{self.book.std():.2%}/day; {self.book.std() * math.sqrt(TRADING_DAYS):.2%}/year"
             if len(self.book) > 1 else "n/a"),
            ("1-day bootstrap VaR 95%", var_text(var95)),
            ("1-day bootstrap VaR 99%", var_text(var99)),
            ("Maximum drawdown (current book, 1 year)", _percent(max_drawdown(self.book))),
            ("Return sample", f"{len(self.book)} trading days, {stats.get('n', 0)} matched with {BENCHMARK}"),
            ("Saved account snapshots", f"{len(self.history)}"),
        ]
        display_interactive_table(pd.DataFrame(rows, columns=["Metric", "Value"]), cell_style=_neutral,
                                  title=f"IB ACCOUNT {short_id(snap.account)}: PNL AND RISK",
                                  key_rows={"Account value", "Today's PnL", "Unrealized PnL", "Open risk to ATR stops",
                                            "Maximum drawdown (current book, 1 year)"})
        print(f"Risk statistics price today's holdings over the past year of IB daily closes, against {BENCHMARK}; "
              "they describe the current book, not past account performance, because IB's API has no "
              "account-value history. Ranges come from bootstrap resamples. Open risk is each position's "
              f"{STOP_ATR_MULTIPLE:g} x ATR({ATR_PERIOD}) stop distance plus round-trip commission, the same "
              "measure the executors cap desk-wide.")

    def show_positions(self):
        """Open positions with ATR stop/target levels, risk, and beta."""
        if not self._require():
            return
        if self.positions.empty:
            print("No open positions.")
            return
        frame = self.positions.sort_values("Value", key=abs, ascending=False).reset_index(drop=True)
        total = {column: np.nan for column in frame.columns}
        total.update({"Symbol": "Total", "Class": "", "Side": "", "Risk basis": "",
                      "Value": frame["Value"].sum(), "% of account": frame["% of account"].sum(),
                      "uPnL": frame["uPnL"].sum(), "Risk to stop": frame["Risk to stop"].sum()})
        cost = (frame["Avg cost"] * frame["Quantity"].abs()).sum()
        total["uPnL (%)"] = frame["uPnL"].sum() / cost if cost else np.nan
        frame = pd.concat([frame, pd.DataFrame([total])], ignore_index=True)
        frame = frame[emphasis.key_columns_first(list(frame.columns), {"uPnL", "Risk to stop"},
                                                 {"Side", "Value", "uPnL", "uPnL (%)", "24h change (%)"},
                                                 text={"Symbol", "Class"})]
        last = len(frame) - 1
        blanks = {(last, column) for column in frame.columns if pd.isna(total[column])}
        formatters = {column: _number for column in ("Quantity", "Avg cost", "Mark", f"ATR({ATR_PERIOD})",
                                                     "Stop", "Target")}
        formatters.update({column: _money for column in ("Value", "uPnL", "Risk to stop")})
        formatters["% of account"] = _percent
        formatters.update({column: (lambda value: "n/a" if not np.isfinite(value) else f"{value:.2%}")
                           for column in ("uPnL (%)", "24h change (%)")})
        formatters[f"Beta {BETA_LOOKBACK_DAYS}d"] = lambda value: "n/a" if not np.isfinite(value) else f"{value:.2f}"
        display_interactive_table(frame, formatters=formatters, cell_style=_pnl_style, bold_last=True,
                                  blank_missing_cells=blanks, title="IB OPEN POSITIONS",
                                  key_columns={"uPnL": "abs", "Risk to stop": "max"})
        print(f"Stop and target are {STOP_ATR_MULTIPLE:g} x ATR({ATR_PERIOD}) from the current mark (1:1), as the "
              f"executor would place them now; they are not orders. 24h change is the price move since this time "
              f"yesterday (Yahoo Finance; the move since the last daily close when Yahoo has none). Beta uses the last {BETA_LOOKBACK_DAYS} trading days against {BENCHMARK}.")

    def show_book_chart(self):
        """The current book over the past year against the benchmark (both start at 100)."""
        if not self._require() or self.book.empty:
            print("No book returns to chart.")
            return
        book = 100 * (1 + self.book).cumprod()
        bench = None
        if not self.benchmark.empty:
            bench = 100 * (1 + self.benchmark.reindex(book.index).fillna(0.0)).cumprod()
        live = self.live
        if live:
            # Today's move since the last close, from live prices: the line runs to now.
            when = live["time"].tz_localize(None)
            book = pd.concat([book, pd.Series([book.iloc[-1] * (1 + live["book"])], index=[when])])
            if bench is not None and np.isfinite(live["benchmark"]):
                bench = pd.concat([bench, pd.Series([bench.iloc[-1] * (1 + live["benchmark"])], index=[when])])
        colors = ("#636efa", "#ef553b")
        figure = go.Figure(go.Scatter(x=book.index, y=book, name="Current book", line={"color": colors[0]}))
        if bench is not None:
            figure.add_trace(go.Scatter(x=bench.index, y=bench, name=BENCHMARK, line={"color": colors[1]}))
        title = "Current IB holdings over the past year (hypothetical: today's positions held throughout)"
        if live:
            stamp = clock(live['time'].tz_localize(DISPLAY_TIMEZONE) if live['time'].tzinfo is None else live['time'])
            for series, color, name in ((book, colors[0], "Current book"), (bench, colors[1], BENCHMARK)):
                if series is not None and series.index[-1] == live["time"].tz_localize(None):
                    figure.add_trace(go.Scatter(
                        x=[series.index[-1]], y=[series.iloc[-1]], mode="markers", showlegend=False,
                        marker={"color": color, "size": 8, "line": {"color": "#ffffff", "width": 1}},
                        hovertemplate=f"{name} live {stamp}: %{{y:.2f}}<extra></extra>"))
            title += f"; last point live at {stamp} ({live['source']} prices)"
        else:
            title += f"; through the {book.index[-1]:%Y-%m-%d} close"
        figure.update_layout(title=title, yaxis_title="Index (start = 100)", template="plotly_dark", height=420)
        show_chart(figure)
    def show_account_history(self):
        """Saved account-value snapshots (one per run of this notebook or the desk overview)."""
        if not self._require():
            return
        if len(self.history) < 2:
            print(f"{len(self.history)} saved snapshot(s); the history chart appears after the second run.")
            return
        history = self.history.assign(ts=pd.to_datetime(self.history["ts"], utc=True, format="mixed"))
        figure = go.Figure(go.Scatter(x=history["ts"], y=history["equity_usd"], name="Account value", mode="lines+markers"))
        figure.update_layout(title="IB account value (saved snapshots)", yaxis_title="USD",
                             template="plotly_dark", height=380)
        show_chart(figure)
    def show_realized_pnl(self, days: int = 90):
        """Realized PnL and commissions from saved IB fills, by symbol."""
        if not self._require():
            return
        if self.fills.empty:
            print("No saved IB fills yet. TWS returns about a week of executions; each run saves them.")
            return
        fills = self.fills.assign(time=pd.to_datetime(self.fills["time"], utc=True, format="mixed"))
        fills = fills[fills["time"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)]
        if fills.empty:
            print(f"No saved IB fills in the last {days} days.")
            return
        summary = fills.groupby("symbol").agg(
            Fills=("exec_id", "count"), Bought=("quantity", lambda q: q[fills.loc[q.index, "side"] == "BUY"].sum()),
            Sold=("quantity", lambda q: q[fills.loc[q.index, "side"] == "SELL"].sum()),
            Commission=("commission", "sum"), **{"Realized PnL": ("realized_pnl", "sum")},
        ).reset_index().rename(columns={"symbol": "Symbol"})
        summary["Net realized PnL"] = summary["Realized PnL"]  # IB's realized PnL is already after commission
        total = {"Symbol": "Total", **summary.drop(columns="Symbol").sum(numeric_only=True).to_dict()}
        summary = pd.concat([summary, pd.DataFrame([total])], ignore_index=True)
        display_interactive_table(summary, formatters={"Commission": _money, "Realized PnL": _money,
                                                       "Net realized PnL": _money, "Fills": "{:.0f}".format,
                                                       "Bought": _number, "Sold": _number},
                                  cell_style=_pnl_style, bold_last=True, title=f"IB REALIZED PNL: LAST {days} DAYS",
                                  key_columns={"Net realized PnL": "abs"})
        print("From fills saved by this notebook and the desk overview. IB reports realized PnL on closing "
              "fills after commission.")

    def show_fills(self, days: int = 30):
        """Saved executions, newest first."""
        if not self._require():
            return
        if self.fills.empty:
            print("No saved IB fills yet.")
            return
        fills = self.fills.assign(time=pd.to_datetime(self.fills["time"], utc=True, format="mixed"))
        fills = fills[fills["time"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)]
        if fills.empty:
            print(f"No IB fills in the last {days} days.")
            return
        from desk.overview import ib_fills_table

        table = ib_fills_table(fills.sort_values("time", ascending=False))
        display_interactive_table(table, formatters={"Quantity": _number, "Price": _money, "Commission": _money,
                                                     "Realized PnL": _money},
                                  cell_style=_pnl_style, title=f"IB FILLS: LAST {days} DAYS",
                                  key_columns={"Realized PnL": "abs"})

    def show_pnl_attribution(self):
        """Unrealized PnL of open positions plus saved realized PnL, by symbol."""
        if not self._require():
            return
        open_pnl = self.positions.set_index("Symbol")["uPnL"] if not self.positions.empty else pd.Series(dtype=float)
        realized = (self.fills.groupby("symbol")["realized_pnl"].sum() if not self.fills.empty
                    else pd.Series(dtype=float))
        frame = pd.DataFrame({"Unrealized PnL": open_pnl, "Realized PnL": realized}).fillna(0.0)
        if frame.empty:
            print("Nothing to attribute yet.")
            return
        frame["Total PnL"] = frame.sum(axis=1)
        frame = frame.sort_values("Total PnL").reset_index(names="Symbol")
        total = {"Symbol": "Total", **frame.drop(columns="Symbol").sum().to_dict()}
        frame = pd.concat([frame, pd.DataFrame([total])], ignore_index=True)
        display_interactive_table(frame, formatters={column: _money for column in ("Unrealized PnL", "Realized PnL", "Total PnL")},
                                  cell_style=_pnl_style, bold_last=True, title="IB PNL ATTRIBUTION",
                                  key_columns={"Total PnL": "abs"})
        print("Realized PnL covers only fills saved so far (TWS keeps about a week of executions).")
