"""Check presentation, not stable document, RPC or route identifiers."""
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala)\b", re.I)
# Visible HUF strings in the chat components are owned by sibling PR #144.
CHAT_HUF_OWNED_BY_144 = {"frontend/src/components/chat/AskBar.tsx", "frontend/src/components/chat/ChatWidget.tsx"}
VENDOR_WITHOUT_CHAT_HUF = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra|Tridz|HeyKoala)\b", re.I)
APPS = ("frontend", "pos", "self-order", "serve", "urypos", "mosaic", "brief_app")
IMAGE_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".ico", ".webp", ".gif"}


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
            rel = file.relative_to(ROOT).as_posix()
            pattern = VENDOR_WITHOUT_CHAT_HUF if rel in CHAT_HUF_OWNED_BY_144 else VENDOR
            for copy in re.findall(r"__\('((?:[^'\\]|\\.)*)'", file.read_text()):
                with self.subTest(file=file.name, copy=copy):
                    self.assertNotRegex(copy, pattern)

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
        # Upstream blue "URY" wordmarks (SVG and raster) that must never ship in any image directory.
        wordmarks = {
            "72cafa53f6e2859e66e41157f82365d82acb72395e573cf47ebd54acceed35e3",  # urypos/public/URY.svg
            "9b69718cb3882d6e466683e9d7041943563b29377361e029c17439dd7a8ffccd",  # mosaic/public/URY.svg
            "d97820af77e1a005305a5853db722f8db7c017879bfeadbed54afc99ad71394f",  # frontend/Public/URY-bg.png
            "4c0498d99179b4e581ea813180ac6fea1e3fdf611f07d9526108612dfe464157",  # frontend/Public/photo_2026-08-19_13-24-09.jpg
            "6ddda4524dd5f0d6922bc861ccf2e671d7a111fb6fa8166deb4076168d2a8300",  # urypos/src/assets/logos/URY_POS.jpg
            "f03f3ce7c0240d9aa3a96c72ffb67537d7935198a9a01e4eb47326d727a96dbe",  # mosaic/src/assets/logos/mosaic.jpg
        }
        scanned = 0
        for base in [ROOT / app for app in APPS] + [ROOT / "ury"]:
            for file in sorted(path for path in base.rglob("*") if path.is_file()):
                if "node_modules" in file.parts or file.suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                scanned += 1
                with self.subTest(file=str(file.relative_to(ROOT))):
                    self.assertNotIn(hashlib.sha256(file.read_bytes()).hexdigest(), wordmarks)
        self.assertGreater(scanned, 0)

    def test_return_to_app_sanitizes_all_vendor_labels(self):
        script_path = ROOT / "ury/public/js/return_to_app.js"
        probe = r"""
const fs = require('fs');
const code = fs.readFileSync(process.argv[1], 'utf8');
const labels = JSON.parse(process.argv[2]);
const out = {};
for (const label of labels) {
  let rendered = null;
  const storage = new Map();
  const makeEl = (tag) => ({
    tag, id: '', style: {}, attrs: {}, children: [],
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {},
    appendChild(child) { this.children.push(child); },
    remove() {},
  });
  global.window = {
    location: { href: 'https://site.test/desk/ury?ury_return_to=/pos&ury_return_label=' + encodeURIComponent(label),
                search: '?ury_return_to=/pos&ury_return_label=' + encodeURIComponent(label) },
    history: { state: null, replaceState() {} },
    sessionStorage: {
      getItem: (k) => storage.has(k) ? storage.get(k) : null,
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
  };
  global.document = {
    readyState: 'complete',
    getElementById: () => null,
    createElement: makeEl,
    body: { appendChild(el) { rendered = el; } },
  };
  new Function(code)();
  out[label] = rendered && rendered.children[0] ? rendered.children[0].textContent : null;
}
process.stdout.write(JSON.stringify(out));
"""
        cases = ["URY", "Frappe", "ERPNext", "Kamra", "HUF", "huf", "Tridz", "TRIDZ", "HeyKoala", "heykoala", "Maximus POS"]
        result = subprocess.run(["node", "-e", probe, str(script_path), json.dumps(cases)],
                                text=True, capture_output=True, check=True)
        rendered = json.loads(result.stdout)
        for vendor in cases[:-1]:
            with self.subTest(vendor=vendor):
                self.assertEqual(rendered[vendor], "← Back to Maximus")
        self.assertEqual(rendered["Maximus POS"], "← Back to Maximus POS")

    def test_brief_app_dev_shell_offers_agpl_source(self):
        html = (ROOT / "brief_app/index.html").read_text()
        self.assertIn("AGPL-3.0", html)
        self.assertIn("Source", html)
        self.assertNotRegex(re.sub("<[^>]+>", " ", html), VENDOR)

    def test_desk_en_translations_cover_ury_labels_without_duplicating_bill_copy(self):
        en_csv = ROOT / "ury/translations/en.csv"
        self.assertTrue(en_csv.is_file(), "Missing ury/translations/en.csv for Desk Maximus labels")
        with en_csv.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertGreater(len(rows), 1)
        self.assertEqual(rows[0], ["source", "target", "context"])
        mapping = {row[0]: row[1] for row in rows[1:] if len(row) >= 2 and row[0]}
        self.assertEqual(mapping.get("URY"), "Maximus")
        self.assertEqual(mapping.get("URY KOT"), "Kitchen Order Ticket")
        self.assertEqual(mapping.get("URY Waiter Order Slip"), "Waiter Order Slip")
        # Coordinated by name with siteops #8596 (maximus_branding owns the URY Bill Copy picker/label).
        self.assertNotIn("URY Bill Copy", mapping)
        for dt_file in sorted((ROOT / "ury").glob("*/doctype/*/*.json")):
            name = json.loads(dt_file.read_text()).get("name", "")
            if re.search(r"\bury\b", name, re.I):
                with self.subTest(doctype=name):
                    self.assertIn(name, mapping, f"Missing English Desk translation for DocType {name}")
        for source, target in mapping.items():
            with self.subTest(source=source, target=target):
                self.assertTrue(target.strip())
                self.assertNotRegex(target, VENDOR)

    def test_no_hardcoded_ury_refusal_codes_or_kot_ury_series_in_production(self):
        kot_schema = json.loads((ROOT / "ury/ury/doctype/ury_kot/ury_kot.json").read_text())
        naming_field = next(f for f in kot_schema["fields"] if f["fieldname"] == "naming_series")
        self.assertEqual(naming_field.get("default"), "KOT-")
        for py in sorted((ROOT / "ury").rglob("*.py")):
            if py.name.startswith("test_") or "/tests/" in py.as_posix():
                continue
            text = py.read_text(encoding="utf-8")
            with self.subTest(file=str(py.relative_to(ROOT))):
                self.assertNotRegex(text, r"\[URY-[A-Z0-9-]+")
                self.assertNotRegex(text, r"""['"]URY-CREDIT-\d+['"]""")
                self.assertNotRegex(text, r"""['"]KOT-URY-['"]""")

    def test_native_html_field_copy(self):
        for file in (ROOT / "ury").glob("*/doctype/*/*.json"):
            for field in json.loads(file.read_text()).get("fields", []):
                if field.get("fieldtype") == "HTML":
                    copy = re.sub("<[^>]+>", "", field.get("options") or "")
                    self.assertNotRegex(copy, VENDOR, f"{file.name}: {field['fieldname']}")
