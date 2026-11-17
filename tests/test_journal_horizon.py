import calendar
import time
import unittest

from desk import journal


class Horizon(unittest.TestCase):
    def test_median_age_of_open_trades(self):
        now = calendar.timegm(time.strptime("2026-11-17T00:00:00", "%Y-%m-%dT%H:%M:%S"))
        entries = [{"opened": "2026-11-16T00:00:00", "closed": None}, {"opened": "2026-11-10T00:00:00", "closed": None},
                   {"opened": "2026-11-14T00:00:00", "closed": None}, {"opened": "2026-10-01T00:00:00", "closed": "2026-10-02"}]
        self.assertAlmostEqual(journal.horizon_days(entries, now), 3.0, places=3)
        self.assertIsNone(journal.horizon_days([], now))


if __name__ == "__main__":
    unittest.main()
