"""Block lookups follow real block timestamps, not a nominal block time."""

import unittest
from unittest.mock import patch

import pandas as pd

from desk.venues import onchain_history as oh

NOW = 1_800_000_000
LATEST = 60_000_000


def stamp_of(number):
    """Blocks were 2 s apart until block 40,000,000, then 1.2 s apart (the nominal time drifts)."""
    if number >= 40_000_000:
        return NOW - int((LATEST - number) * 1.2)
    return NOW - int((LATEST - 40_000_000) * 1.2) - (40_000_000 - number) * 2


class BlockLookupTests(unittest.TestCase):
    def test_a_drifting_block_time_still_finds_the_day(self):
        moments = [pd.Timestamp(NOW - days * 86400, unit="s", tz="UTC") for days in (400, 250, 54, 3, 0)]

        def fake_rpc(url, method, params):
            return {"number": hex(LATEST), "timestamp": hex(NOW)}

        def fake_batch(url, calls):
            return {call["id"]: {"timestamp": hex(stamp_of(int(call["params"][0], 16)))} for call in calls}

        with patch.object(oh, "_rpc", fake_rpc), patch.object(oh.onchain, "_rpc_batch", fake_batch):
            blocks = oh.blocks_at("base", moments)
        for block, moment in zip(blocks, moments):
            late = moment.timestamp() - stamp_of(block)
            self.assertTrue(0 <= late <= oh.BLOCK_TOLERANCE_SECONDS, (late, moment))


if __name__ == "__main__":
    unittest.main()
