import importlib
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]


class TestPosFloorPad(unittest.TestCase):
    def test_pos_template_defaults_to_unpaired_without_floor_context(self):
        template = Environment().from_string((ROOT / "pos/index.html").read_text())
        html = template.render(
            csrf_token="session-token",
            boot="{}",
            brand={"name": "URY POS", "favicon": "/ury.ico"},
            frappe=SimpleNamespace(lang="en"),
        )
        self.assertIn('window.floor_pad', html)
        self.assertIn('"paired": false', html)
        self.assertIn('"till_open": false', html)
        self.assertNotRegex(html.lower(), r'"(?:pin|hash|device[_ ]?id)"\s*:')

    def test_pos_template_exposes_floor_pad_context(self):
        template = Environment().from_string((ROOT / "pos/index.html").read_text())
        html = template.render(
            csrf_token="session-token",
            boot="{}",
            floor_pad={"paired": True, "till_open": True},
            brand={"name": "URY POS", "favicon": "/ury.ico"},
            frappe=SimpleNamespace(lang="en"),
        )
        self.assertIn("window.floor_pad", html)
        self.assertIn('"paired": true', html)

    def test_pos_context_contains_floor_pad_from_floor_pin_seam(self):
        frappe = MagicMock()
        frappe.conf = {}
        frappe.session.user = "Guest"
        brand = ModuleType("ury.brand")
        brand.get_brand = lambda **_kwargs: {}
        brand.get_brand_context = lambda **_kwargs: {}
        brand.get_theme_context = lambda **_kwargs: {}
        brand.json_for_script = json.dumps
        frappe_utils = ModuleType("frappe.utils")
        frappe_telemetry = ModuleType("frappe.utils.telemetry")
        frappe_telemetry.capture = lambda *_args, **_kwargs: None
        frappe_sessions = frappe.sessions
        frappe_website = ModuleType("frappe.website")
        frappe_website_utils = ModuleType("frappe.website.utils")
        frappe_website_utils.get_boot_data = lambda: {}
        frappe_website.utils = frappe_website_utils
        floor_pin = ModuleType("ury.ury_pos.floor_pin")
        floor_pin.get_floor_pad = lambda: {"paired": True, "till_open": True}
        modules = {
            "frappe": frappe,
            "frappe.sessions": frappe_sessions,
            "frappe.utils": frappe_utils,
            "frappe.utils.telemetry": frappe_telemetry,
            "frappe.website": frappe_website,
            "frappe.website.utils": frappe_website_utils,
            "ury.brand": brand,
            "ury.ury_pos.floor_pin": floor_pin,
        }
        with patch.dict(sys.modules, modules):
            controller = importlib.import_module("ury.www.pos")
        context = {}
        with (
            patch.object(controller, "get_brand_context", return_value={"brand": {"name": "URY POS"}}),
            patch.object(controller, "get_theme_context", return_value={}),
            patch.object(controller, "get_floor_pad", return_value={"paired": True, "till_open": True}, create=True),
            patch.object(controller.frappe.sessions, "get_csrf_token", return_value="session-token"),
            patch.object(controller.frappe, "db", MagicMock()),
            patch.object(controller.frappe, "session", SimpleNamespace(user="Guest")),
            patch.object(controller.frappe.website.utils, "get_boot_data", return_value={}),
        ):
            result = controller.get_context(context)

        self.assertIs(result, context)
        self.assertEqual(context["floor_pad"], {"paired": True, "till_open": True})
        self.assertEqual(set(context["floor_pad"]), {"paired", "till_open"})
        self.assertNotRegex(json.dumps(context, default=str).lower(), r'pin|hash|device[_ ]?id')


if __name__ == "__main__":
    unittest.main()
