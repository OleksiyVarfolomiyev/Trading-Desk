"""A day or week view of a chart runs over exactly its window (desk/app_static/app.js windowedLayout).

On the "Desk vs benchmarks" chart the D view showed since October: the thin "managed" line is a shape anchored to the
x axis at the managed start, the hourly view copied it, and Plotly's autorange stretched the axis to include it.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "desk" / "app_static" / "app.js").read_text()
NODE = shutil.which("node")


def window_function() -> str:
    return re.search(r"// WINDOWED_LAYOUT_START\n(.*?)// WINDOWED_LAYOUT_END", APP, re.S).group(1)


class WindowedLayoutTests(unittest.TestCase):
    def test_the_hourly_views_use_it(self):
        for name in ("drawIntraday", "drawIntradayValue"):
            body = re.search(r"function %s\(.*?\n}\n" % name, APP, re.S).group(0)
            self.assertIn("windowedLayout(node.layout", body, name)
            self.assertNotIn("autorange: true, range: null }),\n    yaxis", body, name)   # no x autorange left behind

    @unittest.skipUnless(NODE, "node is not installed here (the desk server has it)")
    def test_a_marker_outside_the_window_is_dropped_and_the_range_is_explicit(self):
        script = window_function() + """
const day = 86400000, now = Date.parse("2026-11-20T12:00:00Z");
const layout = { xaxis: { autorange: true, range: null, type: "date" }, shapes: [
  { type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0 },
  { type: "line", xref: "x", yref: "paper", x0: "2026-10-16", x1: "2026-10-16", y0: 0, y1: 1 },
  { type: "line", xref: "x", yref: "paper", x0: "2026-11-20T06:00:00Z", x1: "2026-11-20T06:00:00Z", y0: 0, y1: 1 }] };
const out = windowedLayout(layout, now - day, now);
const result = { shapes: out.shapes.length, kept: out.shapes.map((s) => s.xref + ":" + s.x0), autorange: out.xaxis.autorange,
  range: out.xaxis.range, type: out.xaxis.type };
console.log(JSON.stringify(result));
"""
        out = subprocess.run([NODE, "-e", script], check=True, capture_output=True, text=True).stdout
        import json

        result = json.loads(out)
        self.assertEqual(result["shapes"], 2)                       # the zero line and the in-window marker; October is gone
        self.assertNotIn("x:2026-10-16", result["kept"])
        self.assertFalse(result["autorange"])
        self.assertEqual(result["range"], ["2026-11-19T12:00:00.000Z", "2026-11-20T12:00:00.000Z"])
        self.assertEqual(result["type"], "date")


if __name__ == "__main__":
    unittest.main()
