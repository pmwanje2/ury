"""Check presentation, not stable document, RPC or route identifiers."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra)\b", re.I)
APPS = ("frontend", "pos", "self-order", "serve", "urypos", "mosaic", "brief_app")


class ClientBrand(unittest.TestCase):
    def test_install_and_offline_copy(self):
        for base in ("serve/public", "ury/public/brief"):
            manifest = json.loads((ROOT / base / "manifest.webmanifest").read_text())
            for key in ("name", "short_name", "description"):
                self.assertNotRegex(manifest[key], VENDOR)
            self.assertNotRegex(re.sub("<[^>]+>", "", (ROOT / base / "offline.html").read_text()), VENDOR)

    def test_all_application_shell_titles(self):
        for app in APPS:
            with self.subTest(app=app):
                title = re.search(r"<title>(.*?)</title>", (ROOT / app / "index.html").read_text())[1]
                self.assertIn("Maximus", title)
                self.assertNotRegex(title, VENDOR)

    def test_dashboard_translated_copy(self):
        for file in (ROOT / "frontend/src").rglob("*.tsx"):
            if ".test." in file.name:
                continue
            for copy in re.findall(r"__\('((?:[^'\\]|\\.)*)'", file.read_text()):
                with self.subTest(file=file.name, copy=copy):
                    self.assertNotRegex(copy, VENDOR)

    def test_desk_switches_are_absent_from_client_navigation(self):
        for path in ("frontend/src/components/layout/Header.tsx", "pos/src/components/Header.tsx",
                     "frontend/src/pages/Pos/components/Header.tsx", "urypos/src/components/Header.vue",
                     "pos/src/components/POSOpeningDialog.tsx"):
            self.assertNotRegex((ROOT / path).read_text(), r'(?i)switch[ _]to[ _]desk|href="/desk"')

    def test_workspace_display_titles(self):
        for file in (ROOT / "ury").glob("*/workspace/*/*.json"):
            data = json.loads(file.read_text())
            self.assertNotRegex(data["title"], VENDOR, str(file))
