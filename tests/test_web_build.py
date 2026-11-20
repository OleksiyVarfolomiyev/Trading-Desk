"""The web dashboard is assembled from committed files, none holding a full wallet address."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web"))

import build  # noqa: E402

WALLETS = ["0x" + "ab" * 18 + "07f2", "0x" + "cd" * 18 + "c248"]


class WebSiteTests(unittest.TestCase):
    def test_the_site_is_the_page_the_data_builder_and_the_dashboard_renderer(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / "repo"
            for part in ("web", "desk/app_static"):
                shutil.copytree(ROOT / part, copy / part, ignore=shutil.ignore_patterns("site", "__pycache__"))
            subprocess.run(["bash", str(copy / "web" / "build_site.sh")], check=True, capture_output=True)
            site = copy / "web" / "site"
            self.assertEqual(sorted(path.name for path in site.iterdir()), ["app.css", "app.js", "catalog.json", "index.html", "live.js"])
            self.assertEqual((site / "app.js").read_text(), (ROOT / "desk/app_static/app.js").read_text())
            page = (site / "index.html").read_text()
            self.assertIn("plotly-4.1.1.min.js", page)
            self.assertIn('src="live.js"', page)


class OpeningCascadeTests(unittest.TestCase):
    """The page opens as a frame with placeholders, not a blank loading screen, and fills in by stages."""

    def test_both_pages_ship_the_frame_not_a_loading_screen(self):
        for page in ("web/index.html", "desk/app_static/index.html"):
            html = (ROOT / page).read_text()
            self.assertIn('<div id="content" class="skeleton">', html, page)   # visible from the first byte
            self.assertNotIn("Loading the desk", html, page)
            self.assertNotIn('role="progressbar"', html, page)

    def test_the_renderer_fills_the_frame_in_stages_and_a_restored_page_in_one_go(self):
        script = (ROOT / "desk/app_static/app.js").read_text()
        order = [script.index(name) for name in ("renderFirst(doc, data)", "renderPositions(doc, data)",
                                                 "renderTablesAndBenchmarks(doc, data)", "renderChartsAndTiles(doc, data)")]
        self.assertEqual(order, sorted(order))
        self.assertIn("render(latest, { instant: true })", script)   # the restored page
        self.assertIn('classList.remove("skeleton")', script)
        css = (ROOT / "desk/app_static/app.css").read_text()
        self.assertIn("prefers-reduced-motion", css[css.index("desk-fill-in"):])

    def test_the_web_build_starts_the_relay_reads_together(self):
        live = (ROOT / "web/live.js").read_text()
        build = live[live.index("async function build()"):]
        self.assertIn("const relayReads = Promise.all([", build)
        self.assertNotIn('const ibData = await every("ib"', build)   # not ten round trips in a row
        self.assertLess(build.index("const relayReads"), build.index("const hlReads"))


class WalletsSecretTests(unittest.TestCase):
    def test_the_secret_maps_short_ids_to_full_addresses(self):
        with tempfile.TemporaryDirectory() as folder:
            accounts = Path(folder) / "accounts.json"
            accounts.write_text(json.dumps([{"venue": "hl", "id": wallet} for wallet in WALLETS]))
            with patch.dict(os.environ, {"DESK_ACCOUNTS_FILE": str(accounts)}):
                self.assertEqual(json.loads(build.wallets_secret()), {"07f2": WALLETS[0], "c248": WALLETS[1]})


if __name__ == "__main__":
    unittest.main()
