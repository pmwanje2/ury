"""The POS workflow runs every site-less Python test module the till fixes rely on (stdlib only)."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_LESS = (
    "ury.ury.doctype.ury_order.test_price_determination",
    "ury.ury.doctype.ury_menu.test_menu_price_upkeep",
    "ury.ury.api.test_shown_price_is_billed",
    "ury.ury.api.test_kds_display_window",
)


class PosWorkflowPythonModules(unittest.TestCase):
    def test_site_less_step_lists_the_price_and_kitchen_modules(self):
        workflow = (ROOT / ".github/workflows/pos-tests.yml").read_text()
        step = workflow.split("- name: Site-less Python unit tests", 1)[1]
        listed = {line.strip() for line in step.splitlines() if line.strip().startswith("ury.")}
        for module in SITE_LESS:
            with self.subTest(module=module):
                self.assertIn(module, listed)
                self.assertTrue((ROOT / (module.replace(".", "/") + ".py")).is_file(), module)


if __name__ == "__main__":
    unittest.main()
