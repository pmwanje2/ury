"""Both CI shard lists run the tender, tip and till-cash modules (stdlib only)."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TILL_MONEY = (
	"ury.ury.hooks.test_ury_pos_tender",
	"ury.ury.hooks.test_ury_cash_movement",
	"ury.ury.tests.test_settlement_tip",
)


class V16NightlyTillMoneyModules(unittest.TestCase):
	def test_shards_list_the_till_money_modules_once(self):
		for workflow_file in ("test-v16-nightly.yml", "test.yml"):
			workflow = (ROOT / ".github/workflows" / workflow_file).read_text()
			listed = [line.strip() for line in workflow.splitlines() if line.strip().startswith("ury.")]
			for module in TILL_MONEY:
				with self.subTest(workflow=workflow_file, module=module):
					self.assertEqual(listed.count(module), 1)
					self.assertTrue((ROOT / (module.replace(".", "/") + ".py")).is_file(), module)


if __name__ == "__main__":
	unittest.main()
