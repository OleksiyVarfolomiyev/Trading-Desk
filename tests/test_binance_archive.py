import datetime as dt
import io
import zipfile
from unittest import mock

import tempfile
import unittest
from pathlib import Path

from desk.venues import binance_archive as ba


def _zip(path, text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("x.csv", text)
    path.write_bytes(buffer.getvalue())
    return path


class BinanceArchive(unittest.TestCase):
    def test_read_handles_header_and_headerless(self):
        tmp_path = Path(tempfile.mkdtemp())
        row = "1704067200000,1,2,0.5,1.5,10,1704067499999,15,3,4,6,0\n"
        old = ba._read(_zip(tmp_path / "old.zip", row), ba.KLINE_COLUMNS)
        new = ba._read(_zip(tmp_path / "new.zip", ",".join(ba.KLINE_COLUMNS) + "\n" + row), ba.KLINE_COLUMNS)
        assert list(old.columns) == ba.KLINE_COLUMNS
        assert old.iloc[0]["close"] == new.iloc[0]["close"] == 1.5


    def test_months_spans_inclusive(self):
        assert ba._months(dt.date(2024, 11, 15), dt.date(2025, 2, 1)) == [
            dt.date(2024, 11, 1), dt.date(2024, 12, 1), dt.date(2025, 1, 1), dt.date(2025, 2, 1)]


    def test_klines_indexes_by_open_time(self):
        tmp_path = Path(tempfile.mkdtemp())
        rows = "".join(f"{1704067200000 + i * 300000},1,2,0.5,{i},10,0,15,3,4,6,0\n" for i in range(3))
        path = _zip(tmp_path / "k.zip", rows + rows)        # duplicates dropped
        with mock.patch.object(ba, "_files", return_value=[path]):
            frame = ba.klines("BTCUSDT", "5m", "2024-01-01", "2024-01-01")
        assert list(frame["close"]) == [0, 1, 2]
        assert str(frame.index[0]) == "2024-01-01 00:00:00+00:00"


    def test_fetch_caches_and_remembers_missing(self):
        tmp_path = Path(tempfile.mkdtemp())
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=404, ok=False)
        assert ba._fetch("u", tmp_path / "a.zip", session) is None
        session.get.return_value = mock.Mock(status_code=200, ok=True, content=b"zip")
        assert ba._fetch("u", tmp_path / "b.zip", session) == tmp_path / "b.zip"
        session.get.reset_mock()
        assert ba._fetch("u", tmp_path / "b.zip", session) == tmp_path / "b.zip"
        session.get.assert_not_called()
