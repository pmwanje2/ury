"""Kitchen display window as configuration; site-less like test_cancel_order_reason.py.

Oracle Simphony basis: KDS display and reset behaviour is configuration. The
window is a POS Profile setting beside the other KOT settings, and the
current three hours stay the default.
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

NOW = datetime(2026, 11, 18, 12, 0, 0)
APP = Path(__file__).resolve().parents[2]


def _site_less_frappe():
    framework = MagicMock()
    framework._ = lambda *messages: messages[0]
    framework.whitelist = lambda *args, **kwargs: (lambda function: function)
    framework.utils.now.return_value = NOW
    framework.utils.add_to_date.side_effect = lambda value, hours=0, **kwargs: value + timedelta(hours=hours)
    utils = ModuleType("frappe.utils")
    utils.get_datetime = lambda value: value
    utils.cint = lambda value: int(value or 0)
    pos_api = ModuleType("ury.ury_pos.api")
    pos_api.getBranch = MagicMock(return_value="Branch A")
    return framework, {"frappe": framework, "frappe.utils": utils, "ury.ury_pos.api": pos_api}


_framework, _modules = _site_less_frappe()
with patch.dict(sys.modules, _modules):
    from ury.ury.api import ury_kot_display


class TestKdsDisplayWindow(unittest.TestCase):
    def setUp(self):
        self.window = None
        self.framework = MagicMock()
        self.framework.utils = _framework.utils
        self.framework.db.get_value.side_effect = self.get_value
        patcher = patch.object(ury_kot_display, "frappe", self.framework)
        patcher.start()
        self.addCleanup(patcher.stop)

    def get_value(self, doctype, filters, fieldname=None, *args, **kwargs):
        if (doctype, fieldname) == ("POS Profile", "ury_kds_display_window_hours"):
            self.assertEqual(filters, {"branch": "Branch A"})
            return self.window
        return None

    def cutoff(self):
        return ury_kot_display._kot_display_filters("Ready For Prepare", "Branch A")["creation"]

    def test_default_window_is_three_hours(self):
        self.assertEqual(self.cutoff(), (">=", NOW - timedelta(hours=3)))

    def test_window_comes_from_the_branch_pos_profile(self):
        self.window = 8
        self.assertEqual(self.cutoff(), (">=", NOW - timedelta(hours=8)))

    def test_zero_or_negative_window_falls_back_to_three_hours(self):
        for value in (0, -2, "", None):
            self.window = value
            self.assertEqual(self.cutoff(), (">=", NOW - timedelta(hours=3)), value)

    def test_window_is_capped_at_one_business_day(self):
        self.window = 500
        self.assertEqual(self.cutoff(), (">=", NOW - timedelta(hours=24)))

    def test_without_a_branch_the_default_applies(self):
        filters = ury_kot_display._kot_display_filters("Served")
        self.assertEqual(filters["creation"], (">=", NOW - timedelta(hours=3)))
        self.assertNotIn("branch", filters)


class TestKdsWindowFieldIsDeclared(unittest.TestCase):
    def test_field_ships_in_setup_and_a_patch(self):
        setup = (APP / "setup_customizations.py").read_text()
        self.assertTrue('"fieldname": "ury_kds_display_window_hours"' in setup, "field missing from setup")
        declared = setup[setup.index("ury_kds_display_window_hours"):][:400]
        self.assertTrue('"default": "3"' in declared, "field default is not 3")
        patches = (APP / "patches.txt").read_text()
        self.assertIn("ury.patches.v2_0.kds_display_window_field", patches)
        self.assertTrue((APP / "patches/v2_0/kds_display_window_field.py").exists())


if __name__ == "__main__":
    unittest.main()
