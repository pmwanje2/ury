"""getPosProfileFull feeds the POS store; the till's settlement and void UI read these fields from it.

PaymentDialog lets a named account settle short only when ``allow_partial_payment === 1``
(native ERPNext POS Profile field), and Orders offers configured void reasons from
``custom_void_reasons`` (one per line; absent on sites without the field). Both must be
in the payload, or the UI silently treats every till as "no partial payment" and
"free-text void reason".
"""
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


class ProfileFullSettlementFieldsTest(unittest.TestCase):
	def full(self, profile):
		api = importlib.import_module("ury.ury_pos.api")
		with patch.object(api, "getBranch", return_value="Main"), \
			patch("frappe.get_doc", return_value=profile), \
			patch.object(api, "get_default_customer_config", return_value={}):
			return api.getPosProfileFull("Till A")

	def test_allow_partial_payment_is_returned_strictly(self):
		for stored, expected in ((1, 1), (0, 0), (None, 0)):
			with self.subTest(stored=stored):
				payload = self.full(_profile(allow_partial_payment=stored))
				self.assertIn("allow_partial_payment", payload)
				self.assertEqual(payload["allow_partial_payment"], expected)

	def test_configured_void_reasons_are_returned(self):
		payload = self.full(_profile(custom_void_reasons="Kitchen error\nGuest changed mind"))
		self.assertEqual(payload.get("custom_void_reasons"), "Kitchen error\nGuest changed mind")

	def test_void_reasons_read_as_none_on_a_site_without_the_field(self):
		payload = self.full(_profile())
		self.assertIn("custom_void_reasons", payload)
		self.assertIsNone(payload["custom_void_reasons"])

	def test_allow_discount_change_is_returned_strictly(self):
		for stored, expected in ((1, 1), (0, 0), (None, 0)):
			with self.subTest(stored=stored):
				payload = self.full(_profile(allow_discount_change=stored))
				self.assertIn("allow_discount_change", payload)
				self.assertEqual(payload["allow_discount_change"], expected)


if __name__ == "__main__":
	unittest.main()
