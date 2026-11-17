import unittest

from desk import source_watch


def market(leverage=10, decimals=2, delisted=False):
    return {"maxLeverage": leverage, "szDecimals": decimals, "delisted": delisted}


class Diff(unittest.TestCase):
    def test_new_delisted_and_changed_markets(self):
        old = {"markets": {"": {"BTC": market(40), "OLD": market()}, "xyz": {"AMD": market()}}, "fields": {"candle": ["o", "c"]}}
        new = {"markets": {"": {"BTC": market(50), "NEW": market(), "OLD": market(delisted=True)}, "xyz": {"AMD": market(), "NVDA": market()},
                           "abc": {"X": market()}}, "fields": {"candle": ["o", "c", "n"]}}
        findings = source_watch.diff(old, new, held={"OLD"})
        text = " | ".join(findings)
        self.assertIn("New markets: NEW", text)
        self.assertIn("HELD market delisted: OLD", text)
        self.assertIn("BTC max leverage 40x to 50x", text)
        self.assertIn("New xyz markets: NVDA", text)
        self.assertIn("New HIP-3 DEX abc", text)
        self.assertIn("candle response has new fields: n", text)

    def test_no_change_no_findings(self):
        snap = {"markets": {"": {"BTC": market()}}, "fields": {"candle": ["o"]}, "yahoo": {"SPY": 20}}
        self.assertEqual(source_watch.diff(snap, snap), [])

    def test_yahoo_going_silent_is_reported(self):
        old = {"markets": {}, "fields": {}, "yahoo": {"SPY": 20}}
        new = {"markets": {}, "fields": {}, "yahoo": {"SPY": 0}}
        self.assertIn("Yahoo stopped answering for SPY", source_watch.diff(old, new)[0])


if __name__ == "__main__":
    unittest.main()


class ReferenceDiff(unittest.TestCase):
    def test_new_and_gone_headlines_and_a_free_api_going_paid(self):
        from desk import reference_watch as rw

        old = {"pages": {"RH": {"status": 200, "headlines": ["Cortex scanners", "Old item"]}}, "defillama": {"fees": {"status": 200, "items": 10}}}
        new = {"pages": {"RH": {"status": 200, "headlines": ["Cortex scanners", "Social trading"]}}, "defillama": {"fees": {"status": 402, "items": 0}}}
        text = " | ".join(rw.diff(old, new))
        self.assertIn("RH: new — Social trading", text)
        self.assertIn("RH: gone — Old item", text)
        self.assertIn("DefiLlama fees is no longer free", text)

    def test_headlines_from_html_atom_and_llms_txt(self):
        from desk import reference_watch as rw

        text = "<title>Releases</title><h2>Cortex <b>scanners</b></h2>\n- [Perpetuals](https://x/perps)\n"
        self.assertEqual(rw.headlines(text), ["Cortex scanners", "Releases", "Perpetuals"])


if __name__ == "__main__":
    unittest.main()
