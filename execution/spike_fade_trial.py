"""A single live spike-fade test through the desk executor."""

from __future__ import annotations

import dataclasses
import json
import math
import time
from pathlib import Path

from desk.hl.daemon_link import RUN_DIR
from desk.risk import per_position_budget
from desk.stress import desk_exposures
from research.spike_fade import BAR_MS, HOUR_MS, MAX_BOOK_SLIPPAGE, MEMES, NOTICE_LOSS

TRIAL = RUN_DIR / "spike_fade_trial.json"
PAPER = Path(__file__).resolve().parents[1] / "data" / "spike_fade_paper.json"


def candidates(paper: dict, armed_at: int, now: int) -> list[str]:
    return [signal["coin"] for signal in paper.get("signals", [])
            if signal.get("spike") is True and signal["coin"] in MEMES
            and armed_at <= signal["bar"] + BAR_MS <= now < signal["bar"] + 2 * BAR_MS]


def save(path: Path, trial: dict) -> None:
    pending = path.with_suffix(".tmp")
    pending.write_text(json.dumps(trial) + "\n")
    pending.replace(path)


def attempt(daemon, ht, path: Path, trial: dict, paper: dict, now: int) -> None:
    wallet = daemon.wallet(trial["wallet"])
    budget = float(trial["budget"])
    if not math.isfinite(budget) or budget <= 0 or wallet not in daemon.exchanges:
        raise ValueError("A live spike-fade test needs a positive allocation and a signing account")
    if ht.kill_switch_engaged():
        return
    coins = candidates(paper, trial["armed_at"], now)
    if not coins:
        return
    orders = {order["coin"] for order in ht.fetch_open_orders(wallet, [""])}
    coin = next((coin for coin in coins if coin not in orders and (wallet, coin) not in daemon.ladders.ladders
                 and ht._current_position_quantity(wallet, "", coin) == 0), None)
    if coin is None:
        return
    markets = daemon.state.markets([(coin, "")])
    wallets = [wallet, *(other for other in daemon.state.wallets if other != wallet)]
    snapshot = ht.urgent_fees(daemon.state.snapshot(wallets, markets), True)
    book = daemon._other_book(False, True)
    snapshot = dataclasses.replace(snapshot, external_equity=book.equity_usd, external_risk=book.risk_usd,
                                   external_exposures=tuple(desk_exposures(book)))
    stops = ht.resting_stops(wallets, sorted({position.dex for position in snapshot.positions}))
    limits = {coin: [math.inf, per_position_budget(budget)]}
    [plan], _ = ht.plan_trades([{"coin": coin, "dex": "", "side": "SHORT"}], markets, snapshot,
                            stops=stops, strategy="fade", reviewed_limits=limits)
    if plan.block_reason or plan.quantity * markets[coin].mark_price * (1 - MAX_BOOK_SLIPPAGE) < ht.MIN_ORDER_VALUE_USD:
        return
    # Check eligibility and account again after network reads. Persist before submitting so a
    # restart or uncertain exchange response can never send a second opening order.
    started = int(time.time() * 1000)
    if (coin not in candidates(paper, trial["armed_at"], started) or ht.kill_switch_engaged()
            or ht._current_position_quantity(wallet, "", coin) != 0
            or any(order["coin"] == coin for order in ht.fetch_open_orders(wallet, [""]))):
        return
    trial["attempt"] = {"coin": coin, "started": started, "quantity": plan.quantity,
                        "risk_usd": plan.risk_budget, "hold_ms": int(paper["rule"][2] * HOUR_MS),
                        "taker_fee_rate": plan.taker_fee_rate}
    save(path, trial)
    _, failures = ht.execute_plans(daemon.exchanges[wallet], wallet, [plan], markets, urgent=True,
                                  slippage=MAX_BOOK_SLIPPAGE)
    daemon.alerts.set("spike-fade:attempt", "Spike fade live test",
                      f"{coin} short IOC processed. " + ("; ".join(failures) if failures else
                       f"Planned risk ${plan.risk_budget:.2f}. Watching actual fills and the timed exit."), ttl=86400)


def monitor(daemon, ht, path: Path, trial: dict, now: int) -> None:
    opened = trial["attempt"]
    coin, started = opened["coin"], opened["started"]
    wallet = daemon.wallet(trial["wallet"])
    fills = ht.post_info({"type": "userFillsByTime", "user": wallet, "startTime": started}, timeout=8)
    fills = sorted((fill for fill in fills if fill["coin"] == coin), key=lambda fill: (fill["time"], fill["tid"]))
    if not fills:
        if now - started > BAR_MS:
            opened["status"] = "no fill observed"
        trial["updated_at"] = now
        save(path, trial)
        return
    shorts = [fill for fill in fills if fill["side"] == "A"]
    covers = [fill for fill in fills if fill["side"] == "B"]
    size = round(sum(float(fill["sz"]) for fill in shorts) - sum(float(fill["sz"]) for fill in covers), 8)
    if (len({fill["oid"] for fill in shorts}) != 1 or float(shorts[0]["startPosition"]) != 0
            or sum(float(fill["sz"]) for fill in shorts) > opened["quantity"] + 1e-8 or size < 0):
        raise RuntimeError(f"{coin} fills no longer identify a standalone trial position")
    live = ht._current_position_quantity(wallet, "", coin)
    if not math.isclose(live, -size, rel_tol=0, abs_tol=1e-8):
        return  # Wait for the fills and position reads to agree.
    market = daemon.state.markets([(coin, "")])[coin] if size else None
    if size and now >= started + opened["hold_ms"] and not ht.kill_switch_engaged():
        # Buy-only reduce-only keeps this safe if a held target fills or the account flips sides.
        limit = ht._round_price(ht._book_mid(coin, "") * (1 + MAX_BOOK_SLIPPAGE), market.size_decimals)
        response = daemon.exchanges[wallet].order(coin, True, size, limit, {"limit": {"tif": "Ioc"}}, reduce_only=True)
        status = ht._status_rows(response)[0]
        if "error" in status:
            daemon.alerts.set("spike-fade:exit", "Spike fade timed exit",
                              f"{coin} close attempt did not fill. {status['error']}", ttl=3600)
    end = now if size else int(fills[-1]["time"])
    funding = ht.post_info({"type": "userFunding", "user": wallet, "startTime": started, "endTime": end}, timeout=8)
    pnl = sum(float(fill["closedPnl"]) - float(fill["fee"]) for fill in fills)
    pnl += sum(float(event["delta"]["usdc"]) for event in funding if event["delta"]["coin"] == coin)
    if size:
        entry = sum(float(fill["sz"]) * float(fill["px"]) for fill in shorts) / sum(float(fill["sz"]) for fill in shorts)
        pnl += size * (entry - market.mark_price * (1 + opened["taker_fee_rate"]))
    opened.update(status="holding" if size else "closed", pnl_usd=pnl,
                  open_notional_usd=size * market.mark_price if size else 0)
    trial["updated_at"] = now
    if pnl <= -NOTICE_LOSS * trial["budget"] and not opened.get("noticed"):
        daemon.alerts.set("spike-fade:drawdown", "Live spike fade drawdown",
                          f"The ${trial['budget']:g} trial is down ${-pnl:.2f}, at least 10% of its allocation.", ttl=86400)
        opened["noticed"] = True
    save(path, trial)


def run(daemon, ht, log, path: Path = TRIAL) -> None:
    while not daemon.stopped.wait(30):
        if not path.exists():
            continue
        try:
            trial = json.loads(path.read_text())
            with daemon.trade_lock:
                now = int(time.time() * 1000)
                if "attempt" not in trial:
                    if "watching_since" not in trial:
                        trial["watching_since"] = now
                    paper = json.loads(PAPER.read_text())
                    attempt(daemon, ht, path, trial, paper, now)
                    trial["updated_at"] = now
                    trial["signal_at"] = paper["updated_at"]
                    save(path, trial)
                elif trial["attempt"].get("status") not in {"closed", "no fill observed"}:
                    monitor(daemon, ht, path, trial, now)
        except Exception as exc:
            log(f"spike fade live test: {type(exc).__name__}: {exc}")
