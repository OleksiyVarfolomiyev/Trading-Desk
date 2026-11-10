"""Only the last four characters of a wallet address or account number appear in files and output."""

import io
import tempfile
import unittest
from pathlib import Path

from desk import privacy
from desk.accounts import hl_wallet, hl_wallets, parse_accounts

WALLET = "0x" + "aB" * 18 + "07f2"  # made up; only the last four characters matter


class MaskTests(unittest.TestCase):
    def test_wallets_and_ib_accounts_keep_only_their_last_four(self):
        text = f"Wallet {WALLET} on IB U98768440 (paper DU1234567) failed."
        self.assertEqual(privacy.mask(text), "Wallet ...07f2 on IB ...8440 (paper ...4567) failed.")

    def test_transaction_hashes_and_short_hex_are_left_alone(self):
        tx = "0x" + "ab" * 32
        self.assertEqual(privacy.mask(f"tx {tx} id 0xdeadbeef"), f"tx {tx} id 0xdeadbeef")

    def test_masked_stream_masks_what_is_printed(self):
        sink = io.StringIO()
        print(f"loaded {WALLET}", file=privacy.MaskedStream(sink))
        self.assertEqual(sink.getvalue(), "loaded ...07f2\n")


class AccountFileTests(unittest.TestCase):
    def test_a_file_named_with_the_full_address_is_renamed_to_the_last_four(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy = Path(folder) / f"hl_account_history_{WALLET.lower()}.csv"
            legacy.write_text("ts,accountValue\n")
            path = privacy.account_file(folder, "hl_account_history", WALLET)
            self.assertEqual(path.name, "hl_account_history_07f2.csv")
            self.assertTrue(path.exists())
            self.assertFalse(legacy.exists())

    def test_ib_files_keep_their_case_sensitive_legacy_name(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "ib_fills_U98768440.csv").write_text("x\n")
            self.assertEqual(privacy.account_file(folder, "ib_fills", "U98768440").read_text(), "x\n")

    def test_an_existing_short_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "onchain_value_history_07f2.csv").write_text("new\n")
            (Path(folder) / f"onchain_value_history_{WALLET.lower()}.csv").write_text("old\n")
            self.assertEqual(privacy.account_file(folder, "onchain_value_history", WALLET).read_text(), "new\n")

    def test_two_accounts_ending_alike_are_refused(self):
        other = "0x" + "1" * 36 + "07F2"
        with self.assertRaisesRegex(ValueError, "07f2"):
            parse_accounts([{"venue": "hl", "id": WALLET}, {"venue": "hl", "id": other}])


class NotebookLookupTests(unittest.TestCase):
    def test_wallets_are_found_by_their_last_four(self):
        accounts = parse_accounts([{"venue": "hl", "id": WALLET}, {"venue": "hl", "id": "0x" + "2" * 36 + "c248"}])
        self.assertEqual(hl_wallet("07F2", accounts), WALLET)
        self.assertEqual(len(hl_wallets(accounts)), 2)
        with self.assertRaises(ValueError):
            hl_wallet("ffff", accounts)


if __name__ == "__main__":
    unittest.main()
