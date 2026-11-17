import os
import unittest
from unittest.mock import patch

from desk import push


class PushTests(unittest.TestCase):
    def test_unset_sends_nothing(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DESK_PUSH_URL", None)
            with patch.object(push, "SECRET_FILE", push.Path("/nonexistent/push_url")):
                self.assertFalse(push.send("t", "x"))

    def test_rate_limited(self):
        push._last = 0.0
        with patch.dict(os.environ, {"DESK_PUSH_URL": "http://x"}), patch.object(push, "_post", return_value=True):
            self.assertTrue(push.send("a", "b"))
            self.assertFalse(push.send("c", "d"))


if __name__ == "__main__":
    unittest.main()
