import unittest

from desk import degraded


class DegradedTest(unittest.TestCase):
    def setUp(self):
        degraded.clear()

    def test_reported_failure_becomes_a_failed_chip_row(self):
        degraded.report("Funding", ValueError("boom"))
        degraded.report("Funding", "later")   # the first failure explains the rest
        (row,) = degraded.rows()
        self.assertEqual((row["name"], row["ok"], row["label"], row["problem"]), ("Funding", False, "failed", "ValueError: boom"))

    def test_clear_removes_rows(self):
        degraded.report("Stress", "x")
        degraded.clear()
        self.assertEqual(degraded.rows(), [])


if __name__ == "__main__":
    unittest.main()
