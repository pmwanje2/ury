"""Site-less contract: the POS order lists carry what the open-check cards show.

Simphony's open-check list names the customer (never an internal id), shows
the covers and how long each open check has been open. On sites that name
Customers by a naming series the Customer id is "CUST-0001", so the lists must
return ``customer_name``; they must also return ``no_of_pax`` and, for open
checks, ``creation`` (the till ages a check from it).

A stand-in database answers each query with only the columns it selects (or
the ``fields`` it asks for), so a column the endpoint forgets to read is
missing from its result exactly as it would be on a site.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]

ROW = {
	"name": "POS-INV-0001", "docstatus": 0, "invoice_printed": 1, "custom_printing_time": None,
	"grand_total": 100.0, "restaurant_table": "T-1", "custom_merged_tables": None, "cashier": "cashier@example.test",
	"waiter": "waiter@example.test", "net_total": 100.0, "posting_time": "10:00:00", "total_taxes_and_charges": 0.0,
	"customer": "CUST-0001", "customer_name": "Dr. Elena Rostova", "status": "Draft", "mobile_number": "",
	"posting_date": "2026-10-02", "rounded_total": 100.0, "order_type": "Dine In", "custom_split_group": None,
	"custom_split_from": None, "custom_guest_label": "Rm 408", "custom_merged_pos_invoice": None,
	"custom_merged_total": None, "additional_discount_percentage": 0, "discount_amount": 0,
	"ury_client_order_id": None, "no_of_pax": 3, "creation": "2026-10-02 10:00:00", "branch": "Branch A",
	"currency": "USD", "conversion_rate": 3700.0,
}


class Row(dict):
	__getattr__ = dict.get


def select_columns(query):
	"""The column list of a ``SELECT ... FROM`` statement."""
	match = re.search(r"SELECT\s+(.*?)\s+FROM\s", query, re.S | re.I)
	return [column.strip() for column in match.group(1).split(",") if column.strip()]


def project(columns):
	return Row({column: ROW[column] for column in columns})


class TestPosInvoiceListFields(unittest.TestCase):
	def setUp(self):
		framework = MagicMock()
		framework.whitelist = lambda *args, **kwargs: (lambda function: function)
		framework._ = lambda message, *args, **kwargs: message
		framework.PermissionError = type("PermissionError", (Exception,), {})
		framework.ValidationError = type("ValidationError", (Exception,), {})
		framework.session.user = "cashier@example.test"
		framework.get_roles.return_value = ["URY Cashier"]
		framework.has_permission.return_value = True
		self.queries = []

		def sql(query, values=None, as_dict=False, **kwargs):
			self.queries.append(query)
			if "custom_split_group IN" in query:
				return []
			return [project(select_columns(query))]

		self.all_calls = []

		def get_all(doctype, filters=None, fields=None, **kwargs):
			self.all_calls.append({"doctype": doctype, "filters": filters, "fields": fields, **kwargs})
			return [project(fields or ["name"])]

		framework.db.sql.side_effect = sql
		framework.get_all.side_effect = get_all
		utils = ModuleType("frappe.utils")
		utils.cint = lambda value: int(value or 0)
		utils.nestedset = MagicMock()
		utils.validate_phone_number = MagicMock(return_value=True)
		default_customer = ModuleType("ury.ury_pos.default_customer")
		default_customer.get_default_customer_config = MagicMock(return_value={})
		item_price = ModuleType("ury.ury.api.item_price")
		item_price.item_price_release_states = MagicMock()
		item_price.price_valid_on = MagicMock()
		ury_order = ModuleType("ury.ury.doctype.ury_order.ury_order")
		ury_order.check_split_target_access = MagicMock()
		self.enterContext(patch.dict(sys.modules, {
			"frappe": framework, "frappe.utils": utils,
			"ury.ury_pos.default_customer": default_customer, "ury.ury.api.item_price": item_price,
			"ury.ury.doctype.ury_order.ury_order": ury_order,
		}))
		spec = importlib.util.spec_from_file_location("ury_pos_api_under_test", ROOT / "ury/ury_pos/api.py")
		self.api = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(self.api)
		self.api.getBranch = lambda: "Branch A"
		self.frappe = framework

	def test_every_order_list_names_the_customer_and_its_covers(self):
		for status in ("Draft", "Unbilled", "Recently Paid", "Paid", "Consolidated", "Return"):
			with self.subTest(status=status):
				rows = self.api.getPosInvoice(status, 10, 0)["data"]
				self.assertEqual(len(rows), 1)
				self.assertEqual(rows[0].get("customer_name"), "Dr. Elena Rostova")
				self.assertEqual(rows[0].get("no_of_pax"), 3)
				self.assertEqual(rows[0].get("currency"), "USD")
				self.assertEqual(rows[0].get("conversion_rate"), 3700.0)

	def test_open_check_lists_carry_when_the_check_was_opened(self):
		for status in ("Draft", "Unbilled"):
			with self.subTest(status=status):
				rows = self.api.getPosInvoice(status, 10, 0)["data"]
				self.assertEqual(rows[0].get("creation"), "2026-10-02 10:00:00")

	def test_search_names_the_customer_and_its_covers_and_finds_it_by_name(self):
		rows = self.api.searchPosInvoice("elena", "Draft")["data"]
		self.assertEqual(rows[0].get("customer_name"), "Dr. Elena Rostova")
		self.assertEqual(rows[0].get("no_of_pax"), 3)
		self.assertEqual(rows[0].get("creation"), "2026-10-02 10:00:00")
		self.assertEqual(rows[0].get("currency"), "USD")
		search = next(call for call in self.all_calls if call.get("or_filters"))
		self.assertIn(["customer_name", "like", "%elena%"], search["or_filters"])

	def test_split_siblings_carry_their_covers(self):
		invoice = Row(ROW, custom_split_group="SG-1")
		self.frappe.get_doc.return_value = invoice
		result = self.api.get_split_group("POS-INV-0001")
		self.assertEqual([row.get("no_of_pax") for row in result["invoices"]], [3])
		self.assertEqual([row.get("currency") for row in result["invoices"]], ["USD"])


if __name__ == "__main__":
	unittest.main()
