"""Run after the six workspace/Vue builds and the independent Brief build."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1] / "ury"
SHELLS = ("pos", "urypos", "mosaic", "order", "serve", "ury", "brief_app/index")


# Public literal render properties in compiled JSX/Vue output. Do not scan
# imports, route strings or DocType arguments as visible copy.
VISIBLE_LITERAL = re.compile(
    r"""(?:\b(?:children|alt|title|placeholder|textContent|innerText)\b|["']aria-label["'])"""
    r"""\s*[:=]\s*(["'])((?:(?!\1)[^\\\r\n]|\\.){0,220})\1"""
)
VENDOR = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra)\b", re.I)


class BuiltClientBrand(unittest.TestCase):
    def test_emitted_titles_and_known_visible_bundle_leaks(self):
        leaks = re.compile(r"built on URY|URY (?:POS logo|Serve|Brief)|Sync with ERPNext|Switch to Desk|Open in ERPNext Desk", re.I)
        for page in SHELLS:
            with self.subTest(page=page):
                html = (ROOT / "www" / (page + ".html")).read_text()
                self.assertIn("Maximus", re.search(r"<title>(.*?)</title>", html)[1])
                self.assertIsNone(leaks.search(html))
                directory = ROOT / "public" / ("brief/dist" if page.startswith("brief") else page)
                bundles = list(directory.rglob("*.js"))
                self.assertTrue(bundles, f"Missing built JS for {page}")
                for bundle in bundles:
                    source = bundle.read_text()
                    for value in VISIBLE_LITERAL.finditer(source):
                        self.assertNotRegex(value[2], VENDOR, bundle.name)
                    match = leaks.search(source)
                    self.assertIsNone(match, f"{bundle.name}: {match}")


if __name__ == "__main__":
    unittest.main()
