import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

import numpy as np
import pandas as pd

from desk.dashboard import json_safe, make_handler, table


class PayloadTests(unittest.TestCase):
    def test_json_safe_turns_nan_and_numpy_values_into_plain_json(self):
        value = {"a": np.float64("nan"), "b": np.int64(3), "c": [float("inf"), 1.5],
                 "d": pd.Timestamp("2026-11-07", tz="UTC"), "e": pd.NaT, "f": np.bool_(True)}
        cleaned = json_safe(value)
        self.assertEqual(cleaned, {"a": None, "b": 3, "c": [None, 1.5], "d": "2026-11-07T00:00:00+00:00",
                                   "e": None, "f": True})
        json.dumps(cleaned, allow_nan=False)

    def test_table_splits_the_total_row_and_keeps_formats(self):
        frame = pd.DataFrame({"Account": ["A", "B", "Total"], "Value": [1.0, 2.0, 3.0]})
        spec = table(frame, {"Value": "usd"}, total_last=True)
        self.assertEqual([column["fmt"] for column in spec["columns"]], ["text", "usd"])
        self.assertEqual(len(spec["rows"]), 2)
        self.assertEqual(spec["total"]["Value"], 3.0)
        self.assertEqual(table(pd.DataFrame(), {})["rows"], [])


class _StubDashboard:
    def __init__(self):
        self.wake = threading.Event()

    def state_json(self):
        return b'{"status": {}, "data": null}'


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.dashboard = _StubDashboard()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        self.port = self.server.server_address[1]
        self.server.RequestHandlerClass = make_handler(self.dashboard, self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, host=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{self.port}")
        for key, value in (headers or {}).items():
            connection.putheader(key, value)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, response.read()

    def test_serves_state_and_page_to_localhost(self):
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"], None)
        status, body = self.request("GET", "/", host=f"localhost:{self.port}")
        self.assertEqual(status, 200)
        self.assertIn(b"Trading desk", body)

    def test_rejects_other_host_names(self):
        status, _ = self.request("GET", "/api/state", host=f"evil.example:{self.port}")
        self.assertEqual(status, 403)

    def test_refresh_wakes_the_loop_and_checks_origin(self):
        status, _ = self.request("POST", "/api/refresh", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(self.dashboard.wake.is_set())
        status, _ = self.request("POST", "/api/refresh", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        self.assertTrue(self.dashboard.wake.is_set())


if __name__ == "__main__":
    unittest.main()


class WalletHistoryTests(unittest.TestCase):
    def test_transfer_balances_roll_today_back_through_transfers(self):
        from desk.venues.onchain_history import transfer_balances

        days = pd.date_range("2026-01-01", "2026-01-04", freq="1D", tz="UTC")
        activity = pd.DataFrame({
            "chain": ["ethereum"] * 3, "token": ["0xA", "0xA", "0xB"],
            "time": ["2026-01-02 12:00:00+00:00", "2026-01-03 12:00:00+00:00", "2026-01-02 06:00:00+00:00"],
            "direction": ["in", "out", "out"], "amount": [100.0, 40.0, 5.0],
        })
        balances = transfer_balances(activity, {("ethereum", "0xa"): 60.0}, days, {"ethereum"})
        # 0xA: 60 today; before the 40 out it was 100; before the 100 in it was 0.
        self.assertEqual(balances["ethereum:0xa"].tolist(), [0.0, 100.0, 60.0, 60.0])
        # 0xB is no longer held; 5 went out on Jan 2, so it held 5 at the close of Jan 1, and an
        # untracked shortfall never shows as a negative balance.
        self.assertEqual(balances["ethereum:0xb"].tolist(), [5.0, 0.0, 0.0, 0.0])

    def test_llama_closes_take_the_point_nearest_each_midnight(self):
        from unittest import mock

        from desk.venues import onchain_history

        days = pd.date_range("2026-01-18", "2026-01-21", freq="1D", tz="UTC")
        stamps = ["2026-01-18 23:59:54", "2026-01-20 00:00:03", "2026-01-20 23:59:56"]  # Jan 21 close missing
        points = [{"timestamp": int(pd.Timestamp(s, tz="UTC").timestamp()), "price": float(i + 1)}
                  for i, s in enumerate(stamps)]
        reply = mock.Mock(ok=True)
        reply.json.return_value = {"coins": {"k": {"prices": points, "confidence": 0.99}}}
        with mock.patch.object(onchain_history.onchain, "_get", return_value=reply):
            prices = onchain_history.llama_closes("k", days)
        # Each date is its close (the next midnight); the missing last close carries the last price.
        self.assertEqual(prices.tolist(), [1.0, 2.0, 3.0, 3.0])

    def test_dashboard_series_uses_rebuilt_values_only_before_recording(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from desk.accounts import Account
        from desk.dashboard import _value_with_wallets, _wallet_value_series

        wallet = "0x" + "1" * 40
        with tempfile.TemporaryDirectory() as folder:
            pd.DataFrame({"date": ["2026-01-01", "2026-01-02", "2026-01-03"], "value_usd": [10.0, 20.0, 99.0]}) \
                .to_csv(Path(folder) / f"onchain_value_backfill_{wallet}.csv", index=False)
            pd.DataFrame({"ts": ["2026-01-03T05:00:00+00:00", "2026-01-04T05:00:00+00:00"], "value_usd": [30.0, 40.0]}) \
                .to_csv(Path(folder) / f"onchain_value_history_{wallet}.csv", index=False)
            desk = SimpleNamespace(accounts=[Account("hl", wallet)], data_dir=Path(folder))
            series = _wallet_value_series(desk, pd.Timestamp("2026-01-01", tz="UTC"))
        self.assertEqual(series["x"], ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"])
        self.assertEqual(series["y"], [10.0, 20.0, 30.0, 40.0])  # the recorded Jan 3 wins over the rebuilt one
        self.assertIn("rebuilt", series["note"])
        accounts = [{"x": ["2026-01-01", "2026-01-04"], "y": [100.0, 200.0]}]
        total = _value_with_wallets(accounts, series)
        self.assertEqual(total["y"], [110.0, 120.0, 130.0, 240.0])
