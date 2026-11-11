"""Sell (or buy) an HL spot token with a limit order chased to a full fill.

The order rests at the best price on its own side and is repriced every few seconds as the book
moves. If it is still open after ``--patience`` seconds, it steps to the mid, and then to the far
side (a limit at the best bid for a sale), so it finishes. Only whole size increments trade: dust
below the token's size step stays in the wallet.

    .venv/bin/python execution/hl_spot.py --wallet 07f2 --sell HYPE            # plan only
    .venv/bin/python execution/hl_spot.py --wallet 07f2 --sell HYPE --live     # asks LIVE TRADING, SUBMIT ORDERS
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hl_trade  # noqa: E402
from hl_trade import post_info  # noqa: E402

REPRICE_SECONDS = 2.0
GIVE_UP_SECONDS = 300.0


def spot_market(token: str) -> dict:
    """The token's USDC pair: its order name, size decimals and the wallet-facing token name."""
    meta = post_info({"type": "spotMeta"})
    tokens = {item["index"]: item for item in meta["tokens"]}
    usdc = next(item["index"] for item in meta["tokens"] if item["name"] == "USDC")
    for pair in meta["universe"]:
        base, quote = pair["tokens"]
        if quote == usdc and tokens[base]["name"].upper() == token.upper():
            return {"name": pair["name"], "token": tokens[base]["name"], "sz_decimals": tokens[base]["szDecimals"]}
    raise ValueError(f"No {token}/USDC spot market on HL.")


def spot_balance(wallet: str, token: str) -> tuple[float, float]:
    """(total, on hold in open orders) of a spot token."""
    state = post_info({"type": "spotClearinghouseState", "user": wallet})
    for balance in state.get("balances", []):
        if balance["coin"] == token:
            return float(balance["total"]), float(balance.get("hold") or 0)
    return 0.0, 0.0


def book(name: str) -> tuple[float, float]:
    levels = post_info({"type": "l2Book", "coin": name})["levels"]
    if not levels[0] or not levels[1]:
        raise ValueError(f"{name} has no two-sided book.")
    return float(levels[0][0]["px"]), float(levels[1][0]["px"])


def spot_price(price: float, sz_decimals: int) -> float:
    """HL spot prices: at most 5 significant figures and 8 - szDecimals decimals."""
    decimals = max(0, min(8 - sz_decimals, 4 - math.floor(math.log10(price))))
    return round(price, decimals)


def target_price(side: str, bid: float, ask: float, elapsed: float, patience: float, sz_decimals: int) -> float:
    """Own side of the book first, then the mid, then the far side."""
    selling = side == "sell"
    if elapsed < patience:
        price = ask if selling else bid
    elif elapsed < 2 * patience:
        price = (bid + ask) / 2
    else:
        price = bid if selling else ask
    return spot_price(price, sz_decimals)


def chase(exchange, wallet: str, market: dict, side: str, quantity: float, patience: float) -> tuple[float, float]:
    """Rest a limit order and reprice it until filled; returns (filled quantity, average price)."""
    is_buy = side == "buy"
    started, oid, price = time.time(), None, None
    filled_before, notional_before = 0.0, 0.0
    remaining = quantity
    while remaining > 0 and time.time() - started < GIVE_UP_SECONDS:
        bid, ask = book(market["name"])
        wanted = target_price(side, bid, ask, time.time() - started, patience, market["sz_decimals"])
        if oid is not None:
            status = hl_trade._order_status(wallet, oid)
            state, done = hl_trade._order_fill(status, market["sz_decimals"])
            if state == "filled":
                filled_before += done
                notional_before += done * price
                remaining = round(quantity - filled_before, market["sz_decimals"])
                oid = None
                continue
            if wanted == price:
                time.sleep(REPRICE_SECONDS)
                continue
            exchange.cancel(market["name"], oid)
            state, done = hl_trade._order_fill(hl_trade._order_status(wallet, oid), market["sz_decimals"])
            filled_before += done
            notional_before += done * price
            remaining = round(quantity - filled_before, market["sz_decimals"])
            oid = None
            if remaining <= 0:
                break
        response = exchange.order(market["name"], is_buy, remaining, wanted, {"limit": {"tif": "Gtc"}})
        status = hl_trade._status_rows(response)[0]
        if "error" in status:
            raise RuntimeError(f"HL refused the order: {status['error']}")
        price = wanted
        if "filled" in status:
            done = float(status["filled"]["totalSz"])
            filled_before += done
            notional_before += done * float(status["filled"]["avgPx"])
            remaining = round(quantity - filled_before, market["sz_decimals"])
            print(f"  filled {done:g} at {float(status['filled']['avgPx']):g}")
            continue
        oid = int(status["resting"]["oid"])
        print(f"  {side} {remaining:g} {market['token']} resting at {wanted:g} (bid {bid:g}, ask {ask:g})")
        time.sleep(REPRICE_SECONDS)
    if oid is not None:
        exchange.cancel(market["name"], oid)
        _, done = hl_trade._order_fill(hl_trade._order_status(wallet, oid), market["sz_decimals"])
        filled_before += done
        notional_before += done * (price or 0)
    return filled_before, (notional_before / filled_before if filled_before else float("nan"))


def main(argv: list[str] | None = None) -> int:
    from desk.privacy import mask_output, shown

    mask_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wallet", required=True, help="main wallet (last four characters, e.g. 07f2)")
    side = parser.add_mutually_exclusive_group(required=True)
    side.add_argument("--sell", metavar="TOKEN", help="token to sell for USDC")
    side.add_argument("--buy", metavar="TOKEN", help="token to buy with USDC (needs --quantity)")
    parser.add_argument("--quantity", type=float, help="amount; a sale defaults to the whole balance")
    parser.add_argument("--patience", type=float, default=30.0, help="seconds at each price step (default 30)")
    parser.add_argument("--live", action="store_true", help="send the order after the typed confirmations")
    args = parser.parse_args(argv)
    wallet = hl_trade.resolve_wallet(args.wallet)
    action = "sell" if args.sell else "buy"
    market = spot_market(args.sell or args.buy)
    total, hold = spot_balance(wallet, market["token"])
    step = 10 ** -market["sz_decimals"]
    if action == "sell":
        quantity = args.quantity if args.quantity else total - hold
        quantity = math.floor(min(quantity, total - hold) / step + 1e-9) * step
    else:
        if not args.quantity:
            parser.error("--buy needs --quantity")
        quantity = math.floor(args.quantity / step + 1e-9) * step
    quantity = round(quantity, market["sz_decimals"])
    bid, ask = book(market["name"])
    print(f"Wallet {shown(wallet)}: {market['token']} balance {total:g} ({hold:g} in open orders).")
    print(f"Plan: {action} {quantity:g} {market['token']} spot, limit at the {'ask' if action == 'sell' else 'bid'} "
          f"(now bid {bid:g} / ask {ask:g}, about ${quantity * (bid + ask) / 2:,.2f}), repriced every "
          f"{REPRICE_SECONDS:g} s; mid after {args.patience:g} s, {'bid' if action == 'sell' else 'ask'} after "
          f"{2 * args.patience:g} s; gives up after {GIVE_UP_SECONDS:g} s.")
    if action == "sell" and total - quantity > 0:
        print(f"Left in the wallet: {total - quantity:g} {market['token']} (below the {step:g} size step, or on hold).")
    if quantity <= 0:
        print("Nothing to trade.")
        return 1
    if not args.live:
        print("Plan only; add --live to send it.")
        return 0
    if input("Type LIVE TRADING to submit this mainnet order: ").strip() != "LIVE TRADING":
        print("Cancelled.")
        return 1
    if input("Type SUBMIT ORDERS to send it: ").strip() != "SUBMIT ORDERS":
        print("Cancelled.")
        return 1
    exchange = hl_trade.build_exchange(wallet, hl_trade._load_key_from_keychain(wallet), [])
    filled, average = chase(exchange, wallet, market, action, quantity, args.patience)
    total_after, _ = spot_balance(wallet, market["token"])
    print(f"Done: {action} {filled:g} of {quantity:g} {market['token']} at an average {average:g} "
          f"(about ${filled * average:,.2f}). {market['token']} balance now {total_after:g}.")
    return 0 if filled >= quantity - step / 2 else 2


if __name__ == "__main__":
    raise SystemExit(main())
