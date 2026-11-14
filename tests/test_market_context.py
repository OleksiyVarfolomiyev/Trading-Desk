"""Market context: mood, crowding, hacks and headlines for the big-move alerts."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from desk import market_context as mc

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin jumps as CFTC approves spot trading</title><link>https://x/1</link><pubDate>{recent}</pubDate></item>
<item><title>Old story</title><link>https://x/2</link><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
</channel></rss>"""


class MarketContextTests(unittest.TestCase):
    def test_headlines_keep_the_window_and_drop_duplicates(self):
        from email.utils import formatdate

        feed = RSS.format(recent=formatdate(time.time() - 3600, usegmt=True))
        with patch.object(mc, "_get", return_value=feed):
            items = mc.headlines(hours=24)
        self.assertEqual([item["title"] for item in items], ["Bitcoin jumps as CFTC approves spot trading"])  # once
        self.assertIn("1 h · ", mc.hover(items))

    def test_hacks_in_the_window_and_above_the_size_largest_first(self):
        now = 1_800_000_000.0
        items = [{"name": "Small", "amount": 1e6, "date": now - 3600}, {"name": "Big", "amount": 6e8, "date": now - 3600},
                 {"name": "Mid", "amount": 9e7, "date": now - 3600}, {"name": "Old", "amount": 9e8, "date": now - 5 * 86400}]
        with patch.object(mc, "_get", return_value=items):
            found = mc.recent_hacks(now=now)
        self.assertEqual([item["name"] for item in found], ["Big", "Mid"])
        self.assertEqual(mc.hack_alert(found[0], now)[0], "Major hack")
        self.assertEqual(mc.hack_alert(found[1], now)[0], "Hack")

    def test_crowding_counts_shorts_paying_among_the_liquid_alts(self):
        meta = {"universe": [{"name": "BTC"}, {"name": "SOL"}, {"name": "DOGE"}]}
        contexts = [{"funding": "-0.001", "dayNtlVlm": "9e9"}, {"funding": "-0.0001", "dayNtlVlm": "1e8"},
                    {"funding": "0.0001", "dayNtlVlm": "1e8"}]
        self.assertEqual(mc.crowding(meta, contexts), (1, 2))                 # BTC is not an alt

    def test_summary_and_event_log(self):
        line = mc.summary((20, "Extreme Fear"), (12, 40), [{"name": "Bybit", "amount": 1.4e9}])
        self.assertEqual(line, "mood extreme fear 20; shorts pay on 12 of 40 alts; hack: Bybit $1.4B")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events.jsonl"
            mc.record_event("alt rally", 0.06, {"mood": (20, "Extreme Fear")}, path=path)
            self.assertIn('"kind": "alt rally"', path.read_text())


if __name__ == "__main__":
    unittest.main()
