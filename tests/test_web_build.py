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
            for part in ("web", "desk/dashboard_static"):
                shutil.copytree(ROOT / part, copy / part, ignore=shutil.ignore_patterns("site", "__pycache__"))
            subprocess.run(["bash", str(copy / "web" / "build_site.sh")], check=True, capture_output=True)
            site = copy / "web" / "site"
            self.assertEqual(sorted(path.name for path in site.iterdir()), ["app.css", "app.js", "index.html", "live.js"])
            self.assertEqual((site / "app.js").read_text(), (ROOT / "desk/dashboard_static/app.js").read_text())
            page = (site / "index.html").read_text()
            self.assertIn("plotly-4.1.1.min.js", page)
            self.assertIn('src="live.js"', page)

    def test_wrangler_deploys_the_worker_and_serves_the_site(self):
        text = "\n".join(line for line in (ROOT / "wrangler.jsonc").read_text().splitlines()
                         if not line.lstrip().startswith("//"))
        config = json.loads(text)
        self.assertEqual(config["main"], "web/worker.js")
        self.assertEqual(config["assets"]["directory"], "./web/site")
        self.assertIn("web/build_site.sh", config["build"]["command"])
        self.assertEqual([space["binding"] for space in config["kv_namespaces"]], ["DESK"])


class WalletsSecretTests(unittest.TestCase):
    def test_the_secret_maps_short_ids_to_full_addresses(self):
        with tempfile.TemporaryDirectory() as folder:
            accounts = Path(folder) / "accounts.json"
            accounts.write_text(json.dumps([{"venue": "hl", "id": wallet} for wallet in WALLETS]))
            with patch.dict(os.environ, {"DESK_ACCOUNTS_FILE": str(accounts)}):
                self.assertEqual(json.loads(build.wallets_secret()), {"07f2": WALLETS[0], "c248": WALLETS[1]})


if __name__ == "__main__":
    unittest.main()
