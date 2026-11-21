"""Both CI shard lists run the site-less kitchen display contract modules (stdlib only)."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KITCHEN = (
    "ury.ury.api.test_kitchen_actions",
    "ury.ury.api.test_kds_display_window",
    "ury.ury.api.test_kot_dashboard_counts",
)


class V16NightlyKitchenModules(unittest.TestCase):
    def test_shards_list_the_kitchen_display_modules_once(self):
        for workflow_file in ("test-v16-nightly.yml", "test.yml"):
            workflow = (ROOT / ".github/workflows" / workflow_file).read_text()
            listed = [line.strip() for line in workflow.splitlines() if line.strip().startswith("ury.")]
            for module in KITCHEN:
                with self.subTest(workflow=workflow_file, module=module):
                    self.assertEqual(listed.count(module), 1)
                    self.assertTrue((ROOT / (module.replace(".", "/") + ".py")).is_file(), module)


if __name__ == "__main__":
    unittest.main()
