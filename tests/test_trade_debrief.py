import unittest

import numpy as np
import pandas as pd

from desk import trade_debrief as td


def fill(direction, px, sz, t, crossed=False, fee=0.01, pnl=0.0):
    return {"coin": "XYZ", "dir": direction, "px": str(px), "sz": str(sz), "time": t, "crossed": crossed, "fee": str(fee),
            "closedPnl": str(pnl)}


class Pieces(unittest.TestCase):
    def test_entry_and_exit_fills_follow_the_trades_direction(self):
        fills = [fill("Open Short", 10, 1, 1000), fill("Open Long", 9, 1, 1500), fill("Close Short", 8, 1, 90_000_000)]
        entries, exits = td.trade_fills(fills, "XYZ", "SHORT", 1000, None)
        self.assertEqual((len(entries), len(exits)), (1, 1))

    def test_execution_numbers(self):
        entries = [fill("Open Short", 10, 2, 1000, crossed=False), fill("Open Short", 10.2, 1, 4000, crossed=True)]
        exits = [fill("Close Short", 9, 3, 90_000, pnl=3.6)]
        ex = td.execution(entries, exits, arrival=10.0, side="SHORT")
        self.assertAlmostEqual(ex["entry_px"], 10.0666667, places=4)
        self.assertAlmostEqual(ex["slippage"], -0.00667, places=4)      # a short sold above the arrival price: a gain
        self.assertAlmostEqual(ex["maker_share"], 20 / 30.2, places=4)
        self.assertAlmostEqual(ex["entry_seconds"], 3.0)
        self.assertAlmostEqual(ex["closed_pnl"], 3.6)

    def test_market_excursions_for_a_short(self):
        coin = pd.DataFrame({"o": [10.0, 9.5], "h": [10.5, 9.8], "l": [9.4, 8.6], "c": [9.5, 9.0], "t": [0, 1]})
        btc = pd.DataFrame({"o": [100.0, 99.0], "h": [101, 100], "l": [98, 97], "c": [99.0, 98.0], "t": [0, 1]})
        m = td.market(coin, btc, "SHORT", entry_px=10.0)
        self.assertAlmostEqual(m["best"], 0.14)       # low 8.6: the short is up 14%
        self.assertAlmostEqual(m["worst"], -0.05)     # high 10.5: down 5%
        self.assertAlmostEqual(m["move"], 0.10)
        self.assertAlmostEqual(m["btc_move"], -0.02)

    def test_thesis_is_flagged_when_unwritten(self):
        entry = {"side": "SHORT", "coin": "XYZ", "strategy": "directional", "opened": "2026-11-16T10:00:00", "signals": {}}
        text, written = td.thesis_text(entry, [])
        self.assertFalse(written)
        self.assertIn("No written thesis", text)
        self.assertEqual(td.thesis_text({**entry, "thesis": "fade the pump"}, [])[1], True)


if __name__ == "__main__":
    unittest.main()
