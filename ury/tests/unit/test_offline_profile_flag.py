"""The POS reads its till profile from getPosProfileFull; the offline opt-in must be in it."""
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class _Profile(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)


def _profile(**overrides):
	values = dict(
		name="Till A", owner="Administrator", creation=None, modified=None, modified_by="Administrator",
		docstatus=0, idx=0, company="URY", customer=None, country="India", disabled=0, warehouse="Stores",
		company_address=None, restaurant="URY", branch="Main", currency="INR", paid_limit=0,
		role_allowed_for_billing=[], role_restricted_for_table_order=[], transfer_role_permissions=[],
		print_format=None,
	)
	values.update(overrides)
	return _Profile(**values)


class OfflineProfileFlagTest(unittest.TestCase):
	def full(self, profile):
		api = importlib.import_module("ury.ury_pos.api")
		with patch.object(api, "getBranch", return_value="Main"), \
			patch("frappe.get_doc", return_value=profile), \
			patch.object(api, "get_default_customer_config", return_value={}):
			return api.getPosProfileFull("Till A")

	def test_flag_is_returned_strictly(self):
		for stored, expected in ((1, 1), (0, 0), (None, 0)):
			with self.subTest(stored=stored):
				self.assertEqual(self.full(_profile(ury_offline_capture=stored)).get("ury_offline_capture"), expected)

	def test_missing_field_reads_as_off(self):
		self.assertEqual(self.full(_profile()).get("ury_offline_capture"), 0)


if __name__ == "__main__":
	unittest.main()
