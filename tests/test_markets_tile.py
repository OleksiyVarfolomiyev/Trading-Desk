"""The Markets tile: Hyperliquid's market selector in the desk (desk/app_static/app.js, the MARKETS_HELPERS block)."""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "desk" / "app_static" / "app.js").read_text()
NODE = shutil.which("node")


def helpers() -> str:
    return re.search(r"// MARKETS_HELPERS_START\n(.*?)// MARKETS_HELPERS_END", APP, re.S).group(1)


class MarketsTileTests(unittest.TestCase):
    def test_the_card_is_on_both_pages_and_on_the_research_screen(self):
        for page in ("desk/app_static/index.html", "web/index.html"):
            self.assertIn('id="t-markets"', (ROOT / page).read_text(), page)
        self.assertRegex(APP, r"research: \[[^\]]*\"t-markets\"")
        self.assertIn("setupMarkets]", APP)   # set up with the other sections

    def test_the_tile_shares_one_fetch_and_only_runs_while_on_screen(self):
        self.assertIn("marketsData.request", APP)          # one request shared by every caller
        self.assertIn("MARKETS_TTL_MS", APP)
        self.assertIn("IntersectionObserver", re.search(r"function setupMarkets\(.*?\n}\n", APP, re.S).group(0))

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_rows_filters_sorting_and_funding_text(self):
        script = helpers() + """
const perp = (name, leverage, px, prev, funding, vol, oi, extra) => Object.assign({ name, maxLeverage: leverage }, extra || {});
const main = { dex: "", meta: { universe: [perp("BTC", 40), perp("ABTC", 5), perp("DEAD", 3, 0, 0, 0, 0, 0, { isDelisted: true }), perp("ZERO", 3)] },
  ctxs: [{ markPx: "100000", prevDayPx: "98000", funding: "0.0000125", dayNtlVlm: "2000000000", openInterest: "20000" },
         { markPx: "2", prevDayPx: "2", funding: "0", dayNtlVlm: "500", openInterest: "10" },
         { markPx: "1", prevDayPx: "1", funding: "0", dayNtlVlm: "1", openInterest: "1" },
         { markPx: "0", prevDayPx: "0", funding: "0", dayNtlVlm: "0", openInterest: "0" }] };
const xyz = { dex: "xyz", meta: { universe: [perp("xyz:TSLA", 10)] }, ctxs: [{ markPx: "250", prevDayPx: "260", funding: "-0.00002", dayNtlVlm: "9000000", openInterest: "40000" }] };
const spot = [{ tokens: [{ name: "USDC" }, { name: "HYPE" }, { name: "PURR" }], universe: [{ name: "@107", tokens: [1, 0] }, { name: "PURR/USDC", tokens: [2, 0] }] },
  [{ markPx: "30", prevDayPx: "27", dayNtlVlm: "7000000" }, { markPx: "0.2", prevDayPx: "0.25", dayNtlVlm: "100000" }]];
const rows = buildMarketRows([main, xyz], spot);
const names = (list) => list.map((r) => r.name);
const out = {
  names: names(rows), kinds: rows.map((r) => r.kind), types: rows.map((r) => r.type),
  btc: rows.find((r) => r.name === "BTC"), tsla: rows.find((r) => r.name === "TSLA"),
  tradfi: names(filterMarkets(rows, "", "tradfi", [])), spotTab: names(filterMarkets(rows, "", "spot", [])), perps: names(filterMarkets(rows, "", "perps", [])).length,
  crypto: names(filterMarkets(rows, "", "crypto", [])), favs: names(filterMarkets(rows, "", "favs", ["xyz:TSLA"])), search: names(filterMarkets(rows, "bt", "all", [])),
  cryptoPerps: names(filterMarkets(rows, "", ["crypto", "perps"], [])), perpsSpot: names(filterMarkets(rows, "", ["perps", "spot"], [])),
  cryptoTradfi: names(filterMarkets(rows, "", ["crypto", "tradfi"], [])), tradfiFavs: names(filterMarkets(rows, "", ["tradfi", "favs"], ["xyz:TSLA", "BTC"])),
  spotTradfi: names(filterMarkets(rows, "", ["spot", "tradfi"], [])), none: names(filterMarkets(rows, "", [], [])).length, allTab: names(filterMarkets(rows, "", ["all"], [])).length,
  byVolume: names(sortMarkets(rows, "volume", "desc")).slice(0, 2), byChange: names(sortMarkets(rows, "funding", "asc")),
  funding: [fundingText(0.0001), fundingText(0), fundingText(0.00013), fundingText(-0.02), fundingText(null)],
};
console.log(JSON.stringify(out));
"""
        out = json.loads(subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(out["names"], ["BTC", "ABTC", "TSLA", "HYPE/USDC", "PURR/USDC"])   # delisted and zero-price markets are left out; @107 reads HYPE/USDC
        self.assertEqual(out["kinds"], ["crypto", "crypto", "tradfi", "crypto", "crypto"])
        self.assertEqual(out["types"], ["perp", "perp", "perp", "spot", "spot"])
        self.assertAlmostEqual(out["btc"]["funding"], 0.0001)                              # the hourly rate x 8
        self.assertAlmostEqual(out["btc"]["apr"], 0.0000125 * 24 * 365)
        self.assertAlmostEqual(out["btc"]["change"], 100000 / 98000 - 1)
        self.assertEqual(out["btc"]["oi"], 20000 * 100000)                                   # open interest in dollars
        self.assertEqual(out["tsla"]["leverage"], 10)
        self.assertEqual(out["tradfi"], ["TSLA"])
        self.assertEqual(out["spotTab"], ["HYPE/USDC", "PURR/USDC"])
        self.assertEqual(out["perps"], 3)
        self.assertEqual(out["crypto"], ["BTC", "ABTC", "HYPE/USDC", "PURR/USDC"])
        self.assertEqual(out["favs"], ["TSLA"])
        self.assertEqual(out["search"], ["BTC", "ABTC"])                                    # starts with the text first
        self.assertEqual(out["cryptoPerps"], ["BTC", "ABTC"])                                # several categories at once narrow down across groups
        self.assertEqual(out["perpsSpot"], ["BTC", "ABTC", "TSLA", "HYPE/USDC", "PURR/USDC"])  # and add up within one group
        self.assertEqual(out["cryptoTradfi"], ["BTC", "ABTC", "TSLA", "HYPE/USDC", "PURR/USDC"])
        self.assertEqual(out["tradfiFavs"], ["TSLA"])
        self.assertEqual(out["spotTradfi"], [])                                              # spot pairs are all crypto
        self.assertEqual((out["none"], out["allTab"]), (5, 5))                               # no selection, or All: every market
        self.assertEqual(out["byVolume"], ["BTC", "TSLA"])
        self.assertEqual(out["byChange"][-2:], ["HYPE/USDC", "PURR/USDC"])                   # no funding sorts last
        self.assertEqual(out["funding"], ["0.01%", "", "0.013%", "-2%", ""])               # absent is absent


if __name__ == "__main__":
    unittest.main()
