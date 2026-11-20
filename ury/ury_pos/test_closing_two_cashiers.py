# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# See license.txt
"""Whole-till closing on the real v16 lifecycle (database-backed).

One open till, two cashiers (a waiter and the till holder) each create and
submit a POS Invoice. The till holder submits the POS Closing Entry. Every
submitted, unconsolidated invoice of the till must be consolidated through
URY's ``CustomPOSInvoiceMergeLog`` -- not only the invoices the closing user
created, which is what v16's owner-based ``populate_pos_transactions`` and
ERPNext's own ``validate_pos_invoices`` owner check select.

Same convention as ``ury_pos/test_e2e_p0_p1_flow.py``: real Branch, POS
Profile, Users and POS Opening Entry; the class rolls back afterwards.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.class_overrides.pos_invoice_merge_log import CustomPOSInvoiceMergeLog

COMPANY = "_Test Company"
BRANCH = "_Test Two Cashier Close Branch"
ROOM = "_Test Two Cashier Close Room"
RESTAURANT = "_Test Two Cashier Close Restaurant"
POS_PROFILE = "_Test Two Cashier Close Profile"
CUSTOMER = "_Test Two Cashier Close Customer"
ITEM = "_Test Non Stock Item"
HOLDER = "two-cashier-close-holder@example.com"
WAITER = "two-cashier-close-waiter@example.com"
# ERPNext init_user_and_profile role set (test_pos_closing_entry.py).
ROLES = ("Accounts Manager", "Accounts User", "Sales Manager", "Stock User", "Item Manager")


class TestClosingConsolidatesEveryCashier(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")
		# URY's POS Invoice stock check is out of scope here; stock is not under test.
		cls._negative_stock = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1)
		for email in (HOLDER, WAITER):
			cls._make_user(email)
		cls._make_masters()
		cls._make_room_and_restaurant()
		cls.profile = cls._make_pos_profile()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", cls._negative_stock)
		super().tearDownClass()

	@classmethod
	def _make_user(cls, email):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		frappe.get_doc("User", email).add_roles(*ROLES)

	@classmethod
	def _make_masters(cls):
		if not frappe.db.exists("Branch", BRANCH):
			branch = frappe.get_doc({"doctype": "Branch", "branch": BRANCH})
			branch.flags.ignore_mandatory = True
			branch.insert(ignore_permissions=True)
		if not frappe.db.exists("Customer", CUSTOMER):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": CUSTOMER,
					"customer_group": "Individual",
					"territory": "All Territories",
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)

	@classmethod
	def _make_room_and_restaurant(cls):
		if not frappe.db.exists("URY Room", ROOM):
			frappe.get_doc({"doctype": "URY Room", "name": ROOM, "branch": BRANCH}).insert(
				ignore_permissions=True
			)
		if not frappe.db.exists("URY Restaurant", RESTAURANT):
			frappe.get_doc(
				{
					"doctype": "URY Restaurant",
					"name": RESTAURANT,
					"company": COMPANY,
					"invoice_series_prefix": "TCC",
					"branch": BRANCH,
					"default_room": ROOM,
				}
			).insert(ignore_permissions=True)

	@classmethod
	def _make_pos_profile(cls):
		if frappe.db.exists("POS Profile", POS_PROFILE):
			frappe.delete_doc("POS Profile", POS_PROFILE, ignore_permissions=True, force=1)
		profile = frappe.get_doc(
			{
				"doctype": "POS Profile",
				"name": POS_PROFILE,
				"company": COMPANY,
				"currency": "INR",
				"warehouse": "_Test Warehouse - _TC",
				"cost_center": "_Test Cost Center - _TC",
				"income_account": "Sales - _TC",
				"expense_account": "Cost of Goods Sold - _TC",
				"write_off_account": "_Test Write Off - _TC",
				"write_off_cost_center": "_Test Cost Center - _TC",
				"selling_price_list": "Standard Selling",
				"branch": BRANCH,
				"restaurant": RESTAURANT,
			}
		)
		profile.append("payments", {"mode_of_payment": "Cash", "default": 1})
		profile.append("applicable_for_users", {"user": HOLDER, "default": 1})
		profile.append("applicable_for_users", {"user": WAITER})
		profile.insert(ignore_permissions=True)
		return profile

	def _open_till(self):
		entry = frappe.new_doc("POS Opening Entry")
		entry.pos_profile = POS_PROFILE
		entry.user = HOLDER
		entry.company = COMPANY
		entry.period_start_date = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5)
		entry.posting_date = frappe.utils.nowdate()
		entry.branch = BRANCH
		entry.restaurant = RESTAURANT
		entry.set("balance_details", [{"mode_of_payment": "Cash", "opening_amount": 0}])
		entry.insert(ignore_permissions=True)
		entry.submit()
		return entry

	def _bill_as(self, user, rate):
		frappe.set_user(user)
		try:
			invoice = frappe.new_doc("POS Invoice")
			invoice.pos_profile = POS_PROFILE
			invoice.company = COMPANY
			invoice.customer = CUSTOMER
			invoice.is_pos = 1
			invoice.update_stock = 0
			invoice.append(
				"items",
				{
					"item_code": ITEM,
					"qty": 1,
					"rate": rate,
					"income_account": "Sales - _TC",
					"cost_center": "_Test Cost Center - _TC",
				},
			)
			invoice.append("payments", {"mode_of_payment": "Cash", "amount": rate})
			invoice.insert(ignore_permissions=True)
			invoice.submit()
		finally:
			frappe.set_user("Administrator")
		return invoice

	def test_closing_consolidates_both_cashiers_invoices_through_custom_merge_log(self):
		opening = self._open_till()
		holder_bill = self._bill_as(HOLDER, 300)
		waiter_bill = self._bill_as(WAITER, 200)
		self.assertEqual((holder_bill.owner, waiter_bill.owner), (HOLDER, WAITER))

		frappe.set_user(HOLDER)
		try:
			closing = frappe.new_doc("POS Closing Entry")
			closing.pos_opening_entry = opening.name
			closing.pos_profile = POS_PROFILE
			closing.company = COMPANY
			closing.user = HOLDER
			closing.period_start_date = opening.period_start_date
			closing.period_end_date = frappe.utils.now_datetime()
			closing.posting_date = frappe.utils.nowdate()
			closing.posting_time = frappe.utils.nowtime()
			closing.append(
				"payment_reconciliation",
				{"mode_of_payment": "Cash", "opening_amount": 0, "closing_amount": 500},
			)
			closing.insert(ignore_permissions=True)
			closing.submit()
		finally:
			frappe.set_user("Administrator")

		closing.reload()
		self.assertEqual(
			sorted(row.pos_invoice for row in closing.pos_invoices),
			sorted([holder_bill.name, waiter_bill.name]),
		)
		self.assertEqual(closing.grand_total, 500)
		self.assertEqual(closing.payment_reconciliation[0].expected_amount, 500)

		merge_logs = frappe.get_all(
			"POS Invoice Merge Log", filters={"pos_closing_entry": closing.name}, pluck="name"
		)
		self.assertTrue(merge_logs, "the closing must create a POS Invoice Merge Log")
		for name in merge_logs:
			self.assertIsInstance(frappe.get_doc("POS Invoice Merge Log", name), CustomPOSInvoiceMergeLog)

		for bill in (holder_bill, waiter_bill):
			consolidated = frappe.db.get_value("POS Invoice", bill.name, "consolidated_invoice")
			self.assertTrue(consolidated, f"{bill.owner}'s invoice must be consolidated")
