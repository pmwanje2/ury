"""Check presentation, not stable document, RPC or route identifiers."""
import hashlib
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

    def test_legacy_favicon_urls_serve_the_shared_maximus_icon(self):
        # SiteOps maximus_branding/public/images/favicon.ico, shared across tenant sites.
        for app in ("pos", "serve", "mosaic", "urypos"):
            with self.subTest(app=app):
                icon = (ROOT / app / "public/ury.ico").read_bytes()
                self.assertEqual(hashlib.sha256(icon).hexdigest(), "f8410ea5598b43087f77e64a6b8732f6a7ae6b0a09c94497b383e1e92df95c9c")

    def test_upstream_wordmark_is_not_shipped(self):
        # The blue upstream wordmark the Vue apps shipped but no page references.
        wordmarks = {"72cafa53f6e2859e66e41157f82365d82acb72395e573cf47ebd54acceed35e3",
                     "9b69718cb3882d6e466683e9d7041943563b29377361e029c17439dd7a8ffccd"}
        for base in [ROOT / app / "public" for app in APPS] + [ROOT / "ury/public/Images"]:
            for file in sorted(path for path in base.rglob("*") if path.is_file()):
                with self.subTest(file=str(file.relative_to(ROOT))):
                    self.assertNotIn(hashlib.sha256(file.read_bytes()).hexdigest(), wordmarks)

    def test_native_html_field_copy(self):
        for file in (ROOT / "ury").glob("*/doctype/*/*.json"):
            for field in json.loads(file.read_text()).get("fields", []):
                if field.get("fieldtype") == "HTML":
                    copy = re.sub("<[^>]+>", "", field.get("options") or "")
                    self.assertNotRegex(copy, VENDOR, f"{file.name}: {field['fieldname']}")
