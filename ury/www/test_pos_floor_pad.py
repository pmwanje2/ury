"""The /pos page exposes only paired/till_open floor-pad booleans (theme T4, fork #62)."""

import importlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[2]


def _render(**context):
    template = Environment().from_string((ROOT / "pos/index.html").read_text())
    return template.render(csrf_token="session-token", boot='"{}"', frappe=SimpleNamespace(lang="en"), **context)


class TestPosFloorPad(unittest.TestCase):
    def test_pos_template_defaults_to_unpaired_without_floor_context(self):
        html = _render()
        self.assertIn("window.floor_pad", html)
        self.assertIn('"paired": false', html)
        self.assertIn('"till_open": false', html)
        self.assertNotRegex(html.lower(), r'"(?:pin|hash|device[_ ]?id)"\s*:')

    def test_pos_template_exposes_floor_pad_context(self):
        html = _render(floor_pad={"paired": True, "till_open": True})
        self.assertIn("window.floor_pad", html)
        self.assertIn('"paired": true', html)
        self.assertIn('"till_open": true', html)

    def test_pos_context_contains_floor_pad_from_floor_pin_seam(self):
        controller = importlib.import_module("ury.www.pos")
        self.assertTrue(hasattr(controller, "get_floor_pad"), "ury/www/pos.py must read the floor_pin seam")
        context = {}
        with (
            patch.object(controller, "get_floor_pad", return_value={"paired": True, "till_open": True}),
            patch.object(controller.frappe, "get_cached_doc", return_value=SimpleNamespace(app_name=None, app_logo=None, favicon=None)),
            patch.object(controller.frappe, "get_hooks", return_value={}),
            patch("frappe.website.doctype.website_theme.website_theme.get_active_theme", return_value=None),
            patch.object(controller.frappe.sessions, "get_csrf_token", return_value="session-token"),
            patch.object(controller.frappe, "db", MagicMock()),
            patch.object(controller.frappe, "session", SimpleNamespace(user="Guest")),
            patch.object(controller.frappe.website.utils, "get_boot_data", return_value={}),
            # Site-free: frappe.conf is a werkzeug local proxy that is unbound without frappe.init.
            patch.object(controller.frappe, "conf", frappe._dict()),
            patch.object(controller.frappe.utils, "get_build_version", return_value="build"),
        ):
            result = controller.get_context(context)

        self.assertIs(result, context)
        self.assertEqual(context["floor_pad"], {"paired": True, "till_open": True})
        self.assertEqual(set(context["floor_pad"]), {"paired", "till_open"})
        self.assertNotRegex(json.dumps(context["floor_pad"]).lower(), r"pin|hash|device[_ ]?id")
        self.assertEqual(context["csrf_token"], "session-token")


if __name__ == "__main__":
    unittest.main()
