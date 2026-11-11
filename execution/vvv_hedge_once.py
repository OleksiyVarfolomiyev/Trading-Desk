"""One-off: turn the 07f2 VVV short into a plain hedge of the staked VVV (2026-11-11).

Cancels the short's stop and take-profit, then buys back the excess with a reduce-only limit
order chased to a full fill, so the short matches the staked amount. The daemon's VVV guard was
already removed. Run from the repository folder:

    .venv/bin/python execution/vvv_hedge_once.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hl_spot  # noqa: E402
import hl_trade as ht  # noqa: E402
from desk.accounts import hl_wallet  # noqa: E402
from desk.privacy import mask_output  # noqa: E402

STAKED = 100.06  # sVVV on Base in wallet c248

mask_output()
wallet = hl_wallet("07f2")
state = ht.post_info({"type": "clearinghouseState", "user": wallet})
short = next(-float(p["position"]["szi"]) for p in state["assetPositions"] if p["position"]["coin"] == "VVV")
excess = round(short - STAKED, 2)
print(f"VVV short {short:g}, staked {STAKED:g}: cancel its stop and take-profit, buy back {excess:g}.")
if input("Type LIVE TRADING to go ahead: ").strip() != "LIVE TRADING":
    sys.exit("Cancelled.")
exchange = ht.build_exchange(wallet, ht._load_key_from_keychain(wallet), [])
for order in ht.post_info({"type": "frontendOpenOrders", "user": wallet}):
    if order["coin"] == "VVV":
        exchange.cancel("VVV", int(order["oid"]))
        print(f"cancelled {order['orderType']}")
left = [order["orderType"] for order in ht.post_info({"type": "frontendOpenOrders", "user": wallet}) if order["coin"] == "VVV"]
print("VVV orders left:", left or "none")
if excess > 0:
    market = {"name": "VVV", "token": "VVV", "sz_decimals": 2}
    filled, average = hl_spot.chase(exchange, wallet, market, "buy", excess, 30.0, reduce_only=True)
    print(f"bought back {filled:g} VVV at {average:g}")
state = ht.post_info({"type": "clearinghouseState", "user": wallet})
for item in state["assetPositions"]:
    p = item["position"]
    if p["coin"] == "VVV":
        print(f"VVV now {p['szi']} (entry {p['entryPx']}), liquidation {p.get('liquidationPx')}, margin {p['marginUsed']}")
print(f"account value {state['marginSummary']['accountValue']}, withdrawable {state.get('withdrawable')}")
