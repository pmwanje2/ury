"""Frappe bench integration test: verifies Maximus guest-check default,
hospitality-scoped XSS blocking (without globally disabling retail POS formats),
URY Bill Copy client-payload XSS hardening, and wkhtmltopdf logo PDF rendering.

Designed to run portably on BOTH seeded benches AND freshly migrated unseeded sites.
"""

import json
import os
import unittest

import frappe
from frappe.desk.form.meta import get_meta
from frappe.utils.print_format_generator import PrintFormatGenerator
from frappe.www.printview import get_html_and_style

from ury.ury.print_format.defaults import (
	GUEST_CHECK_PRINT_FORMAT,
	configure_pos_invoice_print_formats,
)

XSS_PAYLOAD = 'Grace & Co <script>window.__xss=1;alert(1)</script> <img src=x onerror="window.__img_xss=1">'


class TestPosPrintFormatSecurity(unittest.TestCase):
	def _resolve_company_and_currency(self):
		companies = frappe.get_list("Company", fields=["name", "default_currency"], limit=1)
		if companies:
			return companies[0].name, companies[0].default_currency or "UGX"
		return "_Test Company", "UGX"

	def _make_in_memory_invoice(self, docstatus=0, *, hospitality=True):
		company, currency = self._resolve_company_and_currency()
		doc = frappe.new_doc("POS Invoice")
		doc.name = "POS-INV-SEC-TEST"
		doc.docstatus = docstatus
		doc.company = company
		doc.customer = "Walk-in Guest"
		doc.customer_name = XSS_PAYLOAD
		doc.posting_date = frappe.utils.today()
		doc.posting_time = "13:15:00"
		doc.currency = currency
		if hospitality:
			doc.restaurant = "Test Hospitality Outlet"
			doc.branch = "Test Branch"
		else:
			doc.restaurant = None
			doc.branch = None
			doc.pos_profile = None
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
		doc = self._make_in_memory_invoice(docstatus=0, hospitality=False)
		pf = frappe.get_doc("Print Format", "POS Invoice Classic")
		gen = PrintFormatGenerator(pf, doc, None)
		html = gen.get_html_preview()
		self.assertIn("<script>window.__xss=1;alert(1)</script>", html)
		self.assertIn('onerror="window.__img_xss=1"', html)

	def test_02_configuration_sets_guest_check_default_without_globally_disabling_retail_formats(self):
		"""P1-4: Configuration sets default_print_format without globally disabling standard formats."""
		if frappe.db.exists("Print Format", "POS Invoice Classic"):
			frappe.db.set_value("Print Format", "POS Invoice Classic", "disabled", 0, update_modified=False)
		configure_pos_invoice_print_formats()
		frappe.clear_cache(doctype="POS Invoice")

		# 1. Standard POS Invoice Classic stays enabled for non-hospitality retail profiles
		if frappe.db.exists("Print Format", "POS Invoice Classic"):
			self.assertEqual(
				frappe.db.get_value("Print Format", "POS Invoice Classic", "disabled"),
				0,
				"configure_pos_invoice_print_formats must not globally disable standard formats",
			)

		# 2. Desk metadata default_print_format is Merged POS Invoice Format (or safe custom)
		meta = get_meta("POS Invoice", cached=False)
		self.assertEqual(meta.default_print_format, GUEST_CHECK_PRINT_FORMAT)

		# 3. Merged POS Invoice Format is in Desk picker
		picker = [
			p.get("name") if isinstance(p, dict) else p
			for p in (meta.get("__print_formats") or [])
		]
		self.assertIn(GUEST_CHECK_PRINT_FORMAT, picker)

		# 4. configure_pos_invoice_print_formats must not re-run on every migrate and clobber admin overrides
		after_migrate_hooks = frappe.get_hooks("after_migrate") or []
		self.assertNotIn(
			"ury.ury.print_format.defaults.configure_pos_invoice_print_formats",
			after_migrate_hooks,
		)

	def test_03_hospitality_invoice_refuses_classic_while_default_escapes_xss(self):
		"""P1-4: Hospitality POS Invoice refuses POS Invoice Classic cleanly via guards & before_print."""
		from ury.ury.print_format.guards import get_html_and_style as guarded_get_html_and_style

		configure_pos_invoice_print_formats()
		hosp_doc = self._make_in_memory_invoice(docstatus=0, hospitality=True)

		# 1. Guarded whitelisted endpoint rejects POS Invoice Classic for hospitality invoice
		with self.assertRaises(frappe.DoesNotExistError):
			guarded_get_html_and_style(
				doc=json.dumps(hosp_doc.as_dict(), default=str),
				print_format="POS Invoice Classic",
				no_letterhead=1,
			)

		# 2. Direct printview path with form_dict.format rejects POS Invoice Classic via before_print
		frappe.form_dict.format = "POS Invoice Classic"
		try:
			with self.assertRaises(frappe.DoesNotExistError):
				get_html_and_style(
					doc=json.dumps(hosp_doc.as_dict(), default=str),
					print_format="POS Invoice Classic",
					no_letterhead=1,
				)
		finally:
			frappe.form_dict.pop("format", None)

		# 3. Default render (no format passed) uses Merged POS Invoice Format and escapes XSS
		frappe.form_dict.pop("format", None)
		frappe.form_dict.pop("print_format", None)
		out = guarded_get_html_and_style(doc=json.dumps(hosp_doc.as_dict(), default=str), print_format=None, no_letterhead=1)
		html = out.get("html") or ""
		self.assertIn('class="sr"', html)
		self.assertNotIn("<script>window.__xss=1;alert(1)</script>", html)
		self.assertIn("&lt;script&gt;window.__xss=1;alert(1)&lt;/script&gt;", html)

		# 4. Non-hospitality retail POS Invoice is NOT blocked from enabled POS Invoice Classic
		if frappe.db.exists("Print Format", "POS Invoice Classic") and not frappe.db.get_value("Print Format", "POS Invoice Classic", "disabled"):
			retail_doc = self._make_in_memory_invoice(docstatus=0, hospitality=False)
			retail_doc.customer_name = "Retail Customer"
			retail_doc.items[0].item_name = "Retail Item"
			retail_doc.items[0].comment = ""
			out_retail = guarded_get_html_and_style(
				doc=json.dumps(retail_doc.as_dict(), default=str),
				print_format="POS Invoice Classic",
				no_letterhead=1,
			)
			self.assertTrue(out_retail.get("html"))

	def test_04_bill_copy_client_payload_cannot_inject_xss_or_spoof_copy_number(self):
		"""P1-1: Crafted client-serialized doc payload to URY Bill Copy cannot inject XSS or spoof copy_number."""
		from ury.ury.print_format.guards import get_html_and_style as guarded_get_html_and_style

		crafted = self._make_in_memory_invoice(docstatus=1, hospitality=True).as_dict()
		crafted["copy_number"] = '<img src=x onerror="window.__copy_xss=1">'
		crafted["copy_timestamp"] = '<script>window.__ts_xss=1</script>'
		crafted["posting_date"] = '<img src=x onerror="window.__pd_xss=1">'
		crafted["posting_time"] = '<img src=x onerror="window.__pt_xss=1">'

		for fn in (get_html_and_style, guarded_get_html_and_style):
			with self.subTest(endpoint=fn.__module__), self.assertRaises((frappe.PermissionError, frappe.PrintFormatError)):
				fn(doc=json.dumps(crafted, default=str), print_format="URY Bill Copy", no_letterhead=1)

	def test_05_server_pdf_with_local_logo_does_not_fail_wkhtmltopdf_network_error(self):
		"""P1-5: Local logos inline as data URIs (never HTTP host URLs) and render cleanly via wkhtmltopdf."""
		from unittest.mock import patch

		from frappe.utils.pdf import get_pdf

		from ury.ury.print_format.utils import resolve_bill_logo_src

		pub_files = frappe.get_site_path("public", "files")
		os.makedirs(pub_files, exist_ok=True)
		logo_path = os.path.join(pub_files, "_test_maximus_logo.svg")
		with open(logo_path, "w") as f:
			f.write('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20"/></svg>')
		try:
			src = resolve_bill_logo_src("/files/_test_maximus_logo.svg")
			self.assertTrue(src.startswith("data:image/svg+xml;base64,"), src)
			self.assertEqual(resolve_bill_logo_src("/files/../../etc/passwd"), "")

			doc = self._make_in_memory_invoice(docstatus=0, hospitality=True)
			doc.customer_name = "PDF Check Guest"
			doc.items[0].item_name = "Grilled Tilapia"
			doc.items[0].comment = "No salt"

			real_get_value = frappe.db.get_value

			def _get_value_with_logo(doctype, *args, **kwargs):
				if doctype == "Company" and args and args[0] == doc.company:
					fields = args[1] if len(args) >= 2 else kwargs.get("fieldname")
					if fields == "company_logo":
						return "/files/_test_maximus_logo.svg"
					if isinstance(fields, (list, tuple)) and "company_logo" in fields:
						return frappe._dict(
							company_name=doc.company,
							tax_id=None,
							phone_no=None,
							company_logo="/files/_test_maximus_logo.svg",
						)
				return real_get_value(doctype, *args, **kwargs)

			from frappe.utils.jinja import get_jenv

			jenv_db = get_jenv().globals["frappe"].db
			with (
				patch.object(frappe.db, "get_value", side_effect=_get_value_with_logo),
				patch.dict(jenv_db, {"get_value": _get_value_with_logo}),
			):
				for fmt in (GUEST_CHECK_PRINT_FORMAT, "URY Bill Copy"):
					with self.subTest(print_format=fmt):
						if fmt == "URY Bill Copy":
							from ury.ury_pos.api import _render_copy_html

							# Exercise the server renderer used after reserving a numbered QZ copy.
							doc.docstatus = 1
							from frappe.www.printview import get_rendered_template

							full_html, _timestamp = _render_copy_html(doc, doc.name, copy_number=2)
							self.assertIn("data:image/svg+xml;base64,", full_html)
							html = get_rendered_template(doc, print_format=frappe.get_doc("Print Format", fmt), no_letterhead=1)
							self.assertIn("COPY #2", html)
						else:
							out = get_html_and_style(doc=json.dumps(doc.as_dict(), default=str), print_format=fmt, no_letterhead=0)
							html = out.get("html") or ""
						self.assertIn("data:image/svg+xml;base64,", html)
						self.assertNotIn("/files/_test_maximus_logo.svg", html)
						# Standalone HTML renders to valid PDF bytes via wkhtmltopdf with zero network fetches
						pdf_bytes = get_pdf(html)
						self.assertTrue(pdf_bytes.startswith(b"%PDF"), f"Expected valid PDF bytes for {fmt}")
						# Full printview wrapper also receives the inlined data URI, never an HTTP logo URL
						full_html = frappe.get_print(
							"POS Invoice",
							doc.name,
							fmt,
							doc=doc,
							as_pdf=False,
							no_letterhead=0,
						)
						self.assertIn("data:image/svg+xml;base64,", full_html)
						self.assertNotIn("/files/_test_maximus_logo.svg", full_html)
		finally:
			if os.path.exists(logo_path):
				os.remove(logo_path)
