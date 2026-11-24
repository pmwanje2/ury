"""Non-mocked Frappe/MariaDB integration regression test for POS-PR-8 R3 P1 #3.

Verifies that `ury.ury.api.ury_availability.set_item_availability` restores a
hash-named `URY Item Availability` record (`Sold Out` -> `Available`) and sets a
countdown on a brand-new item without raising `frappe.exceptions.LinkValidationError`
when `POS Authorisation Log` runs real Frappe `_validate_links()` against MariaDB.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from ury.ury.api.ury_availability import set_item_availability


class TestAvailabilityRestoreAuthorisationLogLinks(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		suffix = frappe.generate_hash(length=6)
		self.company = frappe.get_list("Company", pluck="name")[0]
		self.branch = frappe.get_doc(
			{
				"doctype": "Branch",
				"branch": f"R3 Avail Branch {suffix}",
				"company": self.company,
			}
		).insert(ignore_permissions=True)

		self.manager_email = f"r3-mgr-{suffix}@example.com"
		self.manager = frappe.get_doc(
			{
				"doctype": "User",
				"email": self.manager_email,
				"first_name": "R3 Manager",
				"send_welcome_email": 0,
				"user_type": "System User",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
		self.manager.add_roles("URY Manager", "URY Cashier")
		self.room = frappe.get_doc(
			{
				"doctype": "URY Room",
				"name": f"R3 Room {suffix}",
				"branch": self.branch.name,
			}
		).insert(ignore_permissions=True)
		self.branch.append("user", {"user": self.manager.name, "room": self.room.name})
		self.branch.save(ignore_permissions=True)

		self.regular_item = self._make_item(f"R3-RIBEYE-{suffix}", "R3 Ribeye Steak")
		self.modifier_item = self._make_item(f"R3-MOD-BACON-{suffix}", "R3 Smoked Bacon")

		self.mod_group = frappe.get_doc(
			{
				"doctype": "URY Modifier Group",
				"group_name": f"R3 Extras {suffix}",
				"display_title": "R3 Extras",
				"group_type": "Add-on",
				"options": [
					{
						"option_code": f"BACON-{suffix}",
						"label": "Smoked Bacon",
						"item": self.modifier_item.name,
						"price_mode": "Menu Item Price",
						"qty": 1,
					}
				],
			}
		).insert(ignore_permissions=True)
		self.regular_item.append(
			"custom_modifier_groups",
			{"modifier_group": self.mod_group.name},
		)
		self.regular_item.save(ignore_permissions=True)

		self.menu = frappe.get_doc(
			{
				"doctype": "URY Menu",
				"name": f"R3 Menu {suffix}",
				"branch": self.branch.name,
				"enabled": 1,
				"items": [
					{
						"item": self.regular_item.name,
						"item_name": self.regular_item.item_name,
						"rate": 45000,
					}
				],
			}
		).insert(ignore_permissions=True)

		self.restaurant = frappe.get_doc(
			{
				"doctype": "URY Restaurant",
				"name": f"R3 Rest {suffix}",
				"company": self.company,
				"branch": self.branch.name,
				"invoice_series_prefix": f"R3-{suffix}-",
				"default_room": self.room.name,
				"active_menu": self.menu.name,
			}
		).insert(ignore_permissions=True)

		frappe.set_user(self.manager.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _make_item(self, item_code: str, item_name: str):
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
		uom = frappe.db.exists("UOM", "Nos") or frappe.get_list("UOM", pluck="name", limit=1)[0]
		return frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_name,
				"item_group": item_group,
				"stock_uom": uom,
				"is_stock_item": 0,
				"is_sales_item": 1,
			}
		).insert(ignore_permissions=True)

	def test_sold_out_to_available_restore_and_first_countdown_pass_real_link_validation(self):
		# 1. Modifier-only item: Sold Out -> creates hash-named URY Item Availability, then restore Available
		sold_res = set_item_availability(self.branch.name, self.modifier_item.name, status="Sold Out")
		self.assertFalse(sold_res["sellable"])

		avail_name = frappe.db.get_value(
			"URY Item Availability",
			{"branch": self.branch.name, "item": self.modifier_item.name},
			"name",
		)
		self.assertTrue(avail_name)

		restored_res = set_item_availability(self.branch.name, self.modifier_item.name, status="Available")
		self.assertTrue(restored_res["sellable"])

		log_row = frappe.db.get_value(
			"POS Authorisation Log",
			{
				"privilege": "item_86_restore",
				"reference_doctype": "URY Item Availability",
				"reference_name": avail_name,
			},
			["name", "outcome", "reference_name"],
			as_dict=True,
		)
		self.assertIsNotNone(log_row)
		self.assertEqual(log_row.outcome, "Approved")

		# 2. Brand-new regular item (no existing URY Item Availability doc yet): setting count=5
		#    must also pass real Frappe link validation without LinkValidationError!
		count_res = set_item_availability(self.branch.name, self.regular_item.name, count=5)
		self.assertTrue(count_res["sellable"])
		self.assertEqual(count_res["available_qty"], 5)
