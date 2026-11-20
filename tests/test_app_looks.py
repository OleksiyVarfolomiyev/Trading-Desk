import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "desk" / "app_static"
CSS = (STATIC / "app.css").read_text()
THEMES = ["light", "dark", "hl", "hl-light", "apple-light", "apple-dark", "viridis"]


class Looks(unittest.TestCase):
    def test_every_theme_has_a_colour_block_with_the_core_tokens(self):
        for theme in THEMES:
            match = re.search(r':root\[data-theme="%s"\] \{(.*?)\n\}' % theme, CSS, re.S) if theme != "light" else \
                re.search(r"^:root \{(.*?)\n\}", CSS, re.S | re.M)
            self.assertIsNotNone(match, theme)
            for token in ("--page", "--surface", "--ink", "--good-text", "--bad-text", "--series-1"):
                self.assertIn(token + ":", match.group(1), f"{theme} lacks {token}")

    def test_both_pages_set_mode_style_and_theme_before_paint(self):
        for page in (STATIC / "index.html", Path(__file__).resolve().parents[1] / "web" / "index.html"):
            text = page.read_text()
            for attribute in ("data-mode", "data-style", "data-theme"):
                self.assertIn(f'setAttribute("{attribute}"', text, f"{page.name} lacks {attribute}")

    def test_the_script_and_the_page_name_themes_the_same_way(self):
        script = (STATIC / "app.js").read_text()
        for theme in ("hl-light", "apple-"):
            self.assertIn(theme, script)
        self.assertIn('"hl-light"', (STATIC / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
