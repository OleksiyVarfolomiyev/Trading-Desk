"""On-chain wallets: tokens in an unstaking cooldown still count."""

import unittest


class CooldownTests(unittest.TestCase):
    def test_vvv_unstaking_stays_on_the_desk_with_its_unlock_date(self):
        from unittest.mock import patch

        from desk.venues import onchain

        ends, amount = 1_763_600_000, 100_061_400_000_000_000_000   # 100.0614 VVV
        raw = "0x" + "0" * 64 + f"{ends:064x}" + f"{amount:064x}"     # stakes(): rewardDebt, cooldownEnd, cooldownAmount
        with patch.object(onchain, "_rpc_batch", return_value={0: raw}):
            holdings = onchain.cooldown_holdings(onchain.CHAINS["base"], "0x" + "ab" * 20)
        self.assertEqual(len(holdings), 1)
        self.assertAlmostEqual(holdings[0].amount, 100.0614)
        self.assertTrue(holdings[0].token.startswith("cooldown:"))
        self.assertTrue(onchain.price_key(onchain.CHAINS["base"], holdings[0].token).endswith(
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"))                  # valued as VVV
        self.assertIn("unstaking", onchain._unstaking_label(holdings[0]))
        with patch.object(onchain, "_rpc_batch", return_value={0: "0x" + "0" * 192}):
            self.assertEqual(onchain.cooldown_holdings(onchain.CHAINS["base"], "0x" + "ab" * 20), [])


if __name__ == "__main__":
    unittest.main()
