import unittest
from datetime import date, time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.class_overrides.pos_invoice_merge_log import CustomPOSInvoiceMergeLog


class TestCustomPOSInvoiceMergeLog(FrappeTestCase):
	def test_get_sales_closing_time_and_date_with_closing_entry(self):
		orig_get_value = frappe.db.get_value

		def mock_db_get_value(doctype, filters, fieldname=None, as_dict=False, **kwargs):
			if doctype == "POS Closing Entry":
				return {
					"pos_profile": "Test POS Profile",
					"period_start_date": "2026-10-21 10:00:00",
				}
			if doctype == "POS Profile":
				return {
					"custom_sales_closing_time": "23:00:00",
					"custom_enable_opening_date": 1,
				}
			return orig_get_value(doctype, filters, fieldname=fieldname, as_dict=as_dict, **kwargs)

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			merge_log = CustomPOSInvoiceMergeLog({"doctype": "POS Invoice Merge Log"})
			merge_log.pos_closing_entry = "POS-CLO-0001"

			closing_time, closing_date = merge_log.get_sales_closing_time_and_date()
			self.assertEqual(closing_time, time(23, 0, 0))
			self.assertEqual(closing_date, date(2026, 10, 21))

	def test_set_sales_closing_date_and_time(self):
		orig_get_value = frappe.db.get_value

		def mock_db_get_value(doctype, filters, fieldname=None, as_dict=False, **kwargs):
			if doctype == "POS Closing Entry":
				return {
					"pos_profile": "Test POS Profile",
					"period_start_date": "2026-10-21 10:00:00",
				}
			if doctype == "POS Profile":
				return {
					"custom_sales_closing_time": "23:00:00",
					"custom_enable_opening_date": 1,
				}
			return orig_get_value(doctype, filters, fieldname=fieldname, as_dict=as_dict, **kwargs)

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			merge_log = CustomPOSInvoiceMergeLog({"doctype": "POS Invoice Merge Log"})
			merge_log.pos_closing_entry = "POS-CLO-0001"
			merge_log.posting_time = "14:30:00"
			merge_log.posting_date = "2026-10-22"

			merge_log.set_sales_closing_date_and_time()

			self.assertEqual(merge_log.posting_time, time(23, 0, 0))
			self.assertEqual(merge_log.posting_date, date(2026, 10, 21))

	def test_preserve_posting_when_not_configured(self):
		orig_get_value = frappe.db.get_value

		def mock_db_get_value(doctype, filters, fieldname=None, as_dict=False, **kwargs):
			if doctype == "POS Closing Entry":
				return {
					"pos_profile": "Standard Profile",
					"period_start_date": "2026-10-21 10:00:00",
				}
			if doctype == "POS Profile":
				return {
					"custom_sales_closing_time": None,
					"custom_enable_opening_date": 0,
				}
			return orig_get_value(doctype, filters, fieldname=fieldname, as_dict=as_dict, **kwargs)

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			merge_log = CustomPOSInvoiceMergeLog({"doctype": "POS Invoice Merge Log"})
			merge_log.pos_closing_entry = "POS-CLO-0001"
			merge_log.posting_time = time(15, 0, 0)
			merge_log.posting_date = date(2026, 10, 22)

			merge_log.set_sales_closing_date_and_time()

			self.assertEqual(merge_log.posting_time, time(15, 0, 0))
			self.assertEqual(merge_log.posting_date, date(2026, 10, 22))

	def test_process_merging_into_sales_invoice(self):
		orig_get_value = frappe.db.get_value

		def mock_db_get_value(doctype, filters, fieldname=None, as_dict=False, **kwargs):
			if doctype == "POS Closing Entry":
				return {
					"pos_profile": "Test POS Profile",
					"period_start_date": "2026-10-21 10:00:00",
				}
			if doctype == "POS Profile":
				return {
					"custom_sales_closing_time": "23:00:00",
					"custom_enable_opening_date": 1,
				}
			return orig_get_value(doctype, filters, fieldname=fieldname, as_dict=as_dict, **kwargs)

		mock_sales_invoice = frappe._dict(
			name="SINV-TEST-0001",
			save=lambda: None,
			submit=lambda: None,
		)

		with patch("frappe.db.get_value", side_effect=mock_db_get_value):
			merge_log = CustomPOSInvoiceMergeLog({"doctype": "POS Invoice Merge Log"})
			merge_log.pos_closing_entry = "POS-CLO-0001"
			merge_log.get_new_sales_invoice = lambda: mock_sales_invoice
			merge_log.merge_pos_invoice_into = lambda sinv, data: sinv

			result = merge_log.process_merging_into_sales_invoice([])

			self.assertEqual(result.is_consolidated, 1)
			self.assertEqual(result.set_posting_time, 1)
			self.assertEqual(result.posting_date, date(2026, 10, 21))
			self.assertEqual(result.posting_time, time(23, 0, 0))
			self.assertEqual(merge_log.consolidated_invoice, "SINV-TEST-0001")

	def test_foreign_currency_consolidation_preserves_invoice_or_party_receivable_account(self):
		accounts = {
			"Debtors - MHL": frappe._dict(
				company="Maximus Hospitality Ltd",
				account_type="Receivable",
				account_currency="UGX",
				is_group=0,
				disabled=0,
			),
			"Debtors USD - MHL": frappe._dict(
				company="Maximus Hospitality Ltd",
				account_type="Receivable",
				account_currency="USD",
				is_group=0,
				disabled=0,
			),
		}

		def mock_get_value(doctype, filters, fieldname=None, as_dict=False, **kwargs):
			if doctype == "Account":
				acct = accounts.get(filters)
				if not acct:
					return None
				if as_dict:
					return acct
				if isinstance(fieldname, str):
					return acct.get(fieldname)
			if doctype == "Customer" and fieldname == "customer_group":
				return "Individual"
			return None

		def mock_get_all(doctype, filters=None, pluck=None, **kwargs):
			if doctype == "Party Account" and filters and filters.get("parent") == "USD Guest":
				return ["Debtors USD - MHL"]
			return []

		merge_log = CustomPOSInvoiceMergeLog({"doctype": "POS Invoice Merge Log"})
		merge_log.company = "Maximus Hospitality Ltd"
		merge_log.customer = "Walk-in"

		with (
			patch("frappe.get_cached_value", return_value="UGX"),
			patch("frappe.db.get_value", side_effect=mock_get_value),
			patch("frappe.get_all", side_effect=mock_get_all),
			patch("frappe.db.get_single_value", return_value=0),
			patch(
				"erpnext.accounts.party.get_party_gle_currency",
				return_value=None,
			),
		):
			# 1. Source POS Invoice carries USD debit_to while consolidated SI defaulted to UGX Debtors.
			si_from_pos = frappe._dict(
				company="Maximus Hospitality Ltd",
				customer="Walk-in",
				currency="USD",
				debit_to="Debtors - MHL",
				party_account_currency="UGX",
			)
			merge_log._resolve_consolidated_receivable_account(
				si_from_pos, [frappe._dict(debit_to="Debtors USD - MHL", currency="USD")]
			)
			self.assertEqual(si_from_pos.debit_to, "Debtors USD - MHL")
			self.assertEqual(si_from_pos.party_account_currency, "USD")

			# 2. Source POS Invoice had UGX Debtors, but Customer carries a USD Party Account.
			si_from_party = frappe._dict(
				company="Maximus Hospitality Ltd",
				customer="USD Guest",
				currency="USD",
				debit_to="Debtors - MHL",
				party_account_currency="UGX",
			)
			merge_log._resolve_consolidated_receivable_account(
				si_from_party, [frappe._dict(debit_to="Debtors - MHL", currency="USD")]
			)
			self.assertEqual(si_from_party.debit_to, "Debtors USD - MHL")
			self.assertEqual(si_from_party.party_account_currency, "USD")

			# 3. Neither USD receivable nor Accounts Settings flag enabled -> actionable coded error.
			si_bad = frappe._dict(
				company="Maximus Hospitality Ltd",
				customer="Walk-in",
				currency="USD",
				debit_to="Debtors - MHL",
				party_account_currency="UGX",
			)
			with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CLOSE-FOREIGN-RECEIVABLE\]"):
				merge_log._resolve_consolidated_receivable_account(
					si_bad, [frappe._dict(debit_to="Debtors - MHL", currency="USD")]
				)
