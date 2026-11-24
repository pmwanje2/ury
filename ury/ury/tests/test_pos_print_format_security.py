"""Frappe bench integration test: proves ERPNext POS Invoice Classic XSS and verifies
Maximus configuration disables all unsafe standard POS Invoice formats, enforces
Merged POS Invoice Format as Desk/POS default, and closes Frappe v16's beta disabled bypass.
"""

import unittest

import frappe
from frappe.desk.form.meta import get_meta
from frappe.utils.print_format_generator import PrintFormatGenerator
from frappe.www.printview import get_html_and_style

from ury.ury.print_format.defaults import (
	GUEST_CHECK_PRINT_FORMAT,
	UNSAFE_POS_INVOICE_PRINT_FORMATS,
	configure_pos_invoice_print_formats,
)


XSS_PAYLOAD = 'Grace & Co <script>window.__xss=1;alert(1)</script> <img src=x onerror="window.__img_xss=1">'


class TestPosPrintFormatSecurity(unittest.TestCase):
	def _make_in_memory_invoice(self, docstatus=0):
		company = frappe.get_list("Company", pluck="name")[0]
		doc = frappe.new_doc("POS Invoice")
		doc.name = "POS-INV-SEC-TEST"
		doc.docstatus = docstatus
		doc.company = company
		doc.customer = "Walk-in Guest"
		doc.customer_name = XSS_PAYLOAD
		doc.posting_date = frappe.utils.today()
		doc.posting_time = "13:15:00"
		doc.currency = frappe.db.get_value("Company", company, "default_currency") or "UGX"
		doc.append(
			"items",
			{
				"item_code": "TEST-ITEM",
				"item_name": 'Tilapia <script>window.__item_xss=1</script>',
				"qty": 1,
				"rate": 10000,
				"amount": 10000,
				"comment": '<img src=x onerror="window.__mod_xss=1"> no salt',
			},
		)
		doc.total = 10000
		doc.net_total = 10000
		doc.grand_total = 10000
		doc.rounded_total = 10000
		doc.paid_amount = 10000
		doc.append("payments", {"mode_of_payment": "Cash", "amount": 10000})
		return doc

	def test_01_proves_erpnext_pos_invoice_classic_xss(self):
		"""Proves ERPNext's standard POS Invoice Classic emits raw unescaped <script>/<img>."""
		if not frappe.db.exists("Print Format", "POS Invoice Classic"):
			self.skipTest("POS Invoice Classic not installed")
		doc = self._make_in_memory_invoice(docstatus=0)
		pf = frappe.get_doc("Print Format", "POS Invoice Classic")
		# Render with disabled=0 in memory to reproduce the upstream Frappe/ERPNext
		# beta Print Format Builder bug (Data.html omits `| e`).
		pf.disabled = 0
		gen = PrintFormatGenerator(pf, doc, None)
		html = gen.get_html_preview()
		self.assertIn("<script>window.__xss=1;alert(1)</script>", html)
		self.assertIn('onerror="window.__img_xss=1"', html)

	def test_02_configuration_disables_unsafe_formats_and_sets_guest_check_default(self):
		"""Configuration disables every unsafe standard POS Invoice format and sets default."""
		configure_pos_invoice_print_formats()
		frappe.clear_cache(doctype="POS Invoice")

		# 1. All unsafe standard POS Invoice formats are disabled
		for name in UNSAFE_POS_INVOICE_PRINT_FORMATS:
			if frappe.db.exists("Print Format", name):
				self.assertEqual(
					frappe.db.get_value("Print Format", name, "disabled"),
					1,
					f"Unsafe format {name!r} must be disabled",
				)

		# 2. Desk metadata default_print_format is Merged POS Invoice Format (or safe custom)
		meta = get_meta("POS Invoice", cached=False)
		self.assertEqual(meta.default_print_format, GUEST_CHECK_PRINT_FORMAT)

		# 3. Desk format picker (__print_formats) excludes every unsafe format
		picker = [
			p.get("name") if isinstance(p, dict) else p
			for p in (meta.get("__print_formats") or [])
		]
		self.assertIn(GUEST_CHECK_PRINT_FORMAT, picker)
		for name in UNSAFE_POS_INVOICE_PRINT_FORMATS:
			self.assertNotIn(name, picker, f"Unsafe format {name!r} leaked into Desk picker")

	def test_03_direct_printview_refuses_disabled_pos_invoice_classic(self):
		"""Closes Frappe v16's Print Format Builder Beta bypass of print_format.disabled."""
		configure_pos_invoice_print_formats()
		existing = frappe.get_all("POS Invoice", filters={"docstatus": ["<", 2]}, pluck="name", limit=1)
		if not existing:
			self.skipTest("No POS Invoice in bench")
		inv_name = existing[0]

		# Direct render of disabled POS Invoice Classic MUST fail closed
		frappe.form_dict.format = "POS Invoice Classic"
		try:
			with self.assertRaises(frappe.DoesNotExistError):
				get_html_and_style(doc="POS Invoice", name=inv_name, print_format="POS Invoice Classic", no_letterhead=1)
		finally:
			frappe.form_dict.pop("format", None)

		# Default render (no format passed) uses Merged POS Invoice Format and escapes XSS
		inv = frappe.get_doc("POS Invoice", inv_name)
		inv.customer_name = XSS_PAYLOAD
		frappe.form_dict.pop("format", None)
		frappe.form_dict.pop("print_format", None)
		out = get_html_and_style(doc=inv.as_dict(), print_format=None, no_letterhead=1)
		html = out.get("html") or ""
		self.assertIn('class="sr"', html)
		self.assertNotIn("<script>window.__xss=1;alert(1)</script>", html)
		self.assertIn("&lt;script&gt;window.__xss=1;alert(1)&lt;/script&gt;", html)

	def test_04_submitted_pos_invoice_prints_without_v16_max_copy_number_error(self):
		"""Printing a submitted POS Invoice on Frappe v16 must not fail on max(copy_number)."""
		submitted = frappe.get_all("POS Invoice", filters={"docstatus": 1}, pluck="name", limit=1)
		if not submitted:
			self.skipTest("No submitted POS Invoice in bench")
		inv_name = submitted[0]
		frappe.form_dict.pop("copy_number", None)
		frappe.form_dict.pop("format", None)
		out_bill = get_html_and_style(doc="POS Invoice", name=inv_name, print_format=GUEST_CHECK_PRINT_FORMAT, no_letterhead=1)
		self.assertIn('class="sr"', out_bill.get("html") or "")
		out_copy = get_html_and_style(doc="POS Invoice", name=inv_name, print_format="URY Bill Copy", no_letterhead=1)
		self.assertIn("COPY #", out_copy.get("html") or "")
