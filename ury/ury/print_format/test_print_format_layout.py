"""Site-less contract: Maximus guest-check / receipt layout, tax-state titles,
configuration-driven headers/footers, and unsafe standard print format hardening.

Design baseline:
- Reuses the siteops "Restaurant Receipt 80mm" `.sr` guest-check structure (no third fork).
- Aligns with Oracle Simphony guest check headers/trailers/print-class output and
  SAP Customer Checkout / S/4HANA Billing Document Output Control.
- Enforces the sales-tax-determination@1 rule: "TAX INVOICE" appears on non-EFRIS
  settled sales ONLY when VAT was determined (never for drafts, No VAT, or
  Service-Charge-only checks).
- Prohibits internal ERPNext fields and vendor brand leaks (URY/HUF/Kamra/Tridz).
- Configures `Merged POS Invoice Format` as default for POS Invoice and makes
  XSS-vulnerable ERPNext standard POS Invoice formats unreachable without editing ERPNext.
"""

import importlib
import json
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from jinja2 import DebugUndefined
from jinja2.sandbox import SandboxedEnvironment

HERE = Path(__file__).resolve().parent
APP_ROOT = HERE.parents[1]
REPO_ROOT = HERE.parents[2]
MERGED = "merged_pos_invoice_format"
COPY = "ury_bill_copy"
WAITER_SLIP = "ury_waiter_order_slip"


class Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def get_formatted(self, fieldname, *args, **kwargs):
		val = getattr(self, fieldname, "")
		if val is None:
			return ""
		return str(val)


def spec(name):
	return json.loads((HERE / name / f"{name}.json").read_text())


def flat_text(html):
	without_style = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
	return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", without_style)).strip()


def make_bill(**overrides):
	base = dict(
		name="POS-INV-0101",
		docstatus=1,
		status="Paid",
		is_return=0,
		company="Grand Terrace Ltd",
		company_address="ADDR-MAIN",
		pos_profile="Main Dining POS",
		restaurant="Grand Terrace",
		branch="Main Dining",
		posting_date="2026-11-24",
		posting_time="13:15:00",
		order_type="Dine In",
		restaurant_table="T-04",
		custom_merged_tables="",
		no_of_pax="2",
		custom_ury_order_number="101",
		custom_guest_label="Window Booth",
		customer="Walk-in Guest",
		customer_name="Walk-in Guest",
		mobile_number="+256 700 123456",
		waiter="samuel.otieno@grandterrace.test",
		cashier="alice.cashier@grandterrace.test",
		currency="UGX",
		tax_id=None,
		items=[
			Doc(
				item_code="GRILLED-TILAPIA",
				item_name="Grilled Lake Tilapia",
				description="<p>INTERNAL HTML DESCRIPTION SHOULD NEVER SHOW</p>",
				comment="No salt, extra lemon",
				qty=2,
				stock_uom="Nos",
				uom="Nos",
				discount_amount=0,
				distributed_discount_amount=0,
				rate=25000,
				amount=50000,
			),
			Doc(
				item_code="MINERAL-WATER",
				item_name="Sparkling Mineral Water",
				description="<p>Water HTML desc</p>",
				comment="",
				qty=1,
				stock_uom="Nos",
				uom="Nos",
				discount_amount=0,
				distributed_discount_amount=0,
				rate=5000,
				amount=5000,
			),
		],
		taxes=[],
		total_qty=3,
		total=55000,
		net_total=55000,
		discount_amount=0,
		additional_discount_percentage=0,
		total_taxes_and_charges=0,
		rounding_adjustment=0,
		grand_total=55000,
		rounded_total=55000,
		payments=[Doc(mode_of_payment="Cash", amount=60000, account="Cash - GT")],
		paid_amount=60000,
		change_amount=5000,
		outstanding_amount=0,
		update_billed_amount_in_delivery_note=1,
		due_date="2026-11-24",
		invoice_created=1,
		terms=None,
		custom_merged_pos_invoice=None,
		custom_merged_total=0,
		custom_merged_pos_invoice_details=[],
		ury_print_partner=None,
		efris_e_invoice=None,
		kamra_ref=None,
		room_number=None,
		copy_number=1,
		copy_timestamp="2026-11-24 13:20:00",
	)
	base.update(overrides)
	return Doc(**base)


def render_format(
	name,
	doc,
	*,
	company_doc=None,
	address_doc=None,
	restaurant_doc=None,
	letter_head=None,
	footer=None,
	no_letterhead=False,
	efris=None,
	e_invoices=None,
	format_bill_money=None,
	resolve_bill_logo_src=None,
):
	users = {
		"samuel.otieno@grandterrace.test": "Samuel Otieno",
		"alice.cashier@grandterrace.test": "Alice Nakato",
	}
	co = company_doc if company_doc is not None else {
		"company_name": "Grand Terrace Ltd",
		"tax_id": "1000456789",
		"phone_no": "+256 414 555 010",
		"company_logo": "/files/grand-terrace-logo.svg",
	}
	addr = address_doc if address_doc is not None else {
		"address_line1": "12 Kampala Road",
		"address_line2": "Terrace Level",
		"city": "Kampala",
		"state": "",
		"country": "Uganda",
		"phone": "+256 414 555 010",
	}
	rest = restaurant_doc if restaurant_doc is not None else {
		"name": "Grand Terrace",
		"address": "ADDR-MAIN",
		"tax_id": "1000456789",
		"image": "/files/grand-terrace-logo.svg",
	}

	def get_value(doctype, filters_or_name, fieldname=None, as_dict=False, order_by=None):
		if doctype == "Company":
			if fieldname == "custom_guest_ledger_clearing":
				return "Guest Ledger Clearing - GT"
			if as_dict:
				return dict(co) if co else None
			if isinstance(fieldname, str):
				return (co or {}).get(fieldname)
		if doctype == "Address":
			return dict(addr) if (as_dict and addr) else None
		if doctype == "URY Restaurant":
			return dict(rest) if (as_dict and rest) else None
		if doctype == "POS Profile" and fieldname == "company_address":
			return "ADDR-MAIN"
		if doctype == "Dynamic Link":
			return "ADDR-MAIN"
		if doctype == "URY Bill Reprint Log":
			return 1
		return None

	def get_doc(doctype, docname):
		assert doctype == "E Invoice", f"Unexpected frappe.get_doc({doctype!r})"
		return (e_invoices or {})[docname]

	env = SandboxedEnvironment(undefined=DebugUndefined, autoescape=False)
	env.globals.update(
		_=lambda s: s,
		frappe=SimpleNamespace(
			get_fullname=lambda user=None: users.get(user, user) if user else "",
			get_doc=get_doc,
			get_meta=lambda dt: SimpleNamespace(
				has_field=lambda f: dt == "Company" and f == "custom_guest_ledger_clearing"
			),
			db=SimpleNamespace(get_value=get_value),
			utils=SimpleNamespace(
				formatdate=str,
				format_date=lambda d, fmt=None: str(d or ""),
				format_time=lambda t, fmt=None: str(t or "")[:5],
				get_url=lambda p="": f"https://pos.example{p}" if str(p).startswith("/") else str(p),
				fmt_money=lambda value, **kw: f"{kw.get('currency') or 'UGX'} {value}",
				now=lambda: "2026-11-24 13:20:00",
			),
		),
	)
	if efris is not None:
		env.globals["efris_receipt_data"] = lambda target: efris[target.name]
	if format_bill_money is not None:
		env.globals["format_bill_money"] = format_bill_money
	if resolve_bill_logo_src is not None:
		env.globals["resolve_bill_logo_src"] = resolve_bill_logo_src
	ctx = {"doc": doc, "no_letterhead": no_letterhead}
	if letter_head is not None:
		ctx["letter_head"] = letter_head
	if footer is not None:
		ctx["footer"] = footer
	return env.from_string(spec(name)["html"]).render(**ctx)


class TestGuestCheckLayoutStructure(unittest.TestCase):
	def test_reuses_siteops_sr_guest_check_structure_and_zero_margins(self):
		data = spec(MERGED)
		html = data["html"]
		for key in ("margin_top", "margin_bottom", "margin_left", "margin_right"):
			self.assertEqual(data[key], 0, f"{key} must be 0 for clean 80mm + A4 layout")
		self.assertIn('class="sr"', html)
		self.assertIn("table-layout: fixed", html)
		self.assertIn("overflow-wrap: anywhere", html)
		self.assertIn("@page", html)
		self.assertNotIn("page-border", html)
		self.assertNotIn("col-xs-6", html)

	def test_renders_configured_business_header_logo_address_tin_and_footer(self):
		html = render_format(
			MERGED,
			make_bill(),
			letter_head='<div class="custom-lh">Fine Dining &amp; Grill — Est. 2018</div>',
			footer="Thank you for dining at Grand Terrace<br>Wi-Fi: TerraceGuest",
			no_letterhead=False,
		)
		text = flat_text(html)
		self.assertIn("/files/grand-terrace-logo.svg", html)
		# P1-5: Must not wrap local logo paths with frappe.utils.get_url() (breaks wkhtmltopdf PDFs).
		self.assertNotIn("https://pos.example/files/grand-terrace-logo.svg", html)
		for expected in (
			"Grand Terrace Ltd",
			"Grand Terrace",
			"12 Kampala Road",
			"Kampala",
			"+256 414 555 010",
			"TIN: 1000456789",
			"Fine Dining &amp; Grill",
			"Thank you for dining at Grand Terrace",
			"Wi-Fi: TerraceGuest",
		):
			self.assertIn(expected, html if "&amp;" in expected else text)

	def test_skips_erpnext_a4_default_letterhead_boilerplate_and_respects_no_letterhead(self):
		erpnext_lh = '<table class="letterhead-container"><tr><td><div class="invoice-title">POS Invoice</div></td></tr></table>'
		html = render_format(MERGED, make_bill(), letter_head=erpnext_lh, footer="Custom Footer", no_letterhead=False)
		self.assertNotIn("letterhead-container", html)
		self.assertNotIn("POS Invoice", flat_text(html))
		self.assertIn("Custom Footer", flat_text(html))

		suppressed = render_format(
			MERGED,
			make_bill(),
			letter_head="<div>Header Line</div>",
			footer="Custom Footer",
			no_letterhead=True,
		)
		self.assertNotIn("Header Line", suppressed)
		self.assertNotIn("Custom Footer", suppressed)
		self.assertIn("Thank you", flat_text(suppressed))

	def test_never_shows_internal_erpnext_fields_or_vendor_names(self):
		html = render_format(MERGED, make_bill())
		text = flat_text(html)
		for forbidden in (
			"POS Invoice",
			"Update Billed Amount in Delivery Note",
			"Payment Due Date",
			"Stock UOM",
			"Distributed Discount Amount",
			"Invoice Created",
			"INTERNAL HTML DESCRIPTION SHOULD NEVER SHOW",
			"alice.cashier@grandterrace.test",
			"samuel.otieno@grandterrace.test",
			"URY",
			"HUF",
			"Kamra",
			"Tridz",
		):
			self.assertNotIn(forbidden, text, f"Forbidden internal/vendor string leaked: {forbidden}")

	def test_document_title_follows_tax_and_fiscal_state(self):
		vat_row = Doc(
			description="Output VAT",
			account_head="Output VAT 18% - GT",
			rate=18,
			tax_amount=9900,
			included_in_print_rate=0,
		)
		svc_row = Doc(
			description="Service Charge",
			account_head="Service Charge - GT",
			rate=10,
			tax_amount=5500,
			included_in_print_rate=0,
		)
		zero_vat = Doc(
			description="VAT Exempt",
			account_head="Output VAT 0% - GT",
			rate=0,
			tax_amount=0,
			included_in_print_rate=0,
		)

		def heading(html):
			m = re.search(r"<h4[^>]*>(.*?)</h4>", html, re.DOTALL)
			self.assertIsNotNone(m, "Document title must render in <h4>")
			return flat_text(m.group(1))

		# 1. Draft with VAT is STILL GUEST CHECK (pro forma), never TAX INVOICE
		draft_html = render_format(MERGED, make_bill(docstatus=0, taxes=[vat_row]))
		self.assertEqual(heading(draft_html), "GUEST CHECK")
		self.assertIn("PRO FORMA - NOT A RECEIPT", flat_text(draft_html))
		self.assertNotIn("TAX INVOICE", flat_text(draft_html))

		# 2. Settled non-EFRIS with VAT determined -> TAX INVOICE
		vat_html = render_format(MERGED, make_bill(docstatus=1, taxes=[svc_row, vat_row]))
		self.assertEqual(heading(vat_html), "TAX INVOICE")

		# 3. Settled non-EFRIS with No VAT (empty taxes) -> RECEIPT
		no_vat_html = render_format(MERGED, make_bill(docstatus=1, taxes=[]))
		self.assertEqual(heading(no_vat_html), "RECEIPT")
		self.assertNotIn("TAX INVOICE", flat_text(no_vat_html))

		# 4. Settled non-EFRIS with 0% / zero-tax row only -> RECEIPT
		zero_html = render_format(MERGED, make_bill(docstatus=1, taxes=[zero_vat]))
		self.assertEqual(heading(zero_html), "RECEIPT")
		self.assertNotIn("TAX INVOICE", flat_text(zero_html))

		# 5. Settled non-EFRIS with Service Charge only (no VAT) -> RECEIPT
		svc_only_html = render_format(MERGED, make_bill(docstatus=1, taxes=[svc_row]))
		self.assertEqual(heading(svc_only_html), "RECEIPT")
		self.assertNotIn("TAX INVOICE", flat_text(svc_only_html))

	def test_tax_invoice_title_excludes_non_vat_levies_zero_vat_and_unfiscalised_efris(self):
		"""P1-2: TAX INVOICE only for non-zero VAT/GST on non-EFRIS sites; never for levies, 0.00 VAT, or not_efris."""
		def heading(html):
			m = re.search(r"<h4[^>]*>(.*?)</h4>", html, re.DOTALL)
			self.assertIsNotNone(m)
			return flat_text(m.group(1))

		levy_row = Doc(
			description="Tourism Levy (2%)",
			account_head="Tourism Levy - GT",
			rate=2,
			tax_amount=500,
			included_in_print_rate=0,
		)
		zero_charged_vat = Doc(
			description="Output VAT",
			account_head="Output VAT 18% - GT",
			rate=18,
			tax_amount=0,
			included_in_print_rate=0,
		)
		vat_row = Doc(
			description="Output VAT",
			account_head="Output VAT 18% - GT",
			rate=18,
			tax_amount=9900,
			included_in_print_rate=0,
		)
		gst_row = Doc(
			description="GST 5%",
			account_head="Output GST 5% - GT",
			rate=5,
			tax_amount=2500,
			included_in_print_rate=0,
		)

		# 1. Non-VAT Tourism Levy only -> RECEIPT (never TAX INVOICE)
		levy_html = render_format(MERGED, make_bill(docstatus=1, taxes=[levy_row]))
		self.assertEqual(heading(levy_html), "RECEIPT")
		self.assertNotIn("TAX INVOICE", flat_text(levy_html))

		# 2. 100%-discounted check with 18% VAT row whose tax_amount == 0 -> RECEIPT
		zero_vat_html = render_format(
			MERGED,
			make_bill(docstatus=1, grand_total=0, rounded_total=0, discount_amount=55000, taxes=[zero_charged_vat]),
		)
		self.assertEqual(heading(zero_vat_html), "RECEIPT")
		self.assertNotIn("TAX INVOICE", flat_text(zero_vat_html))

		# 3. EFRIS-enabled site with efris_invoice == 0 (state: not_efris) + VAT -> RECEIPT (must not contradict "Not a fiscal receipt")
		not_efris_doc = make_bill(name="GT-00007", docstatus=1, taxes=[vat_row])
		not_efris_html = render_format(
			MERGED,
			not_efris_doc,
			efris={"GT-00007": {"state": "not_efris", "sandbox": 0}},
		)
		self.assertEqual(heading(not_efris_html), "RECEIPT")
		self.assertIn("Not a fiscal receipt", flat_text(not_efris_html))
		self.assertNotIn("TAX INVOICE", flat_text(not_efris_html))

		# 4. Positive GST on non-EFRIS site -> TAX INVOICE
		gst_html = render_format(MERGED, make_bill(docstatus=1, taxes=[gst_row]))
		self.assertEqual(heading(gst_html), "TAX INVOICE")

		# 5. Merged bill where only partner has non-VAT Tourism Levy -> RECEIPT
		partner_levy = make_bill(name="GT-00008", docstatus=1, taxes=[levy_row])
		merged_levy = make_bill(
			name="GT-00009", docstatus=1, taxes=[], custom_merged_pos_invoice="GT-00008", ury_print_partner=partner_levy
		)
		self.assertEqual(heading(render_format(MERGED, merged_levy)), "RECEIPT")

	def test_merged_usd_check_preserves_cents_partner_currency_formatted_balance_and_attributed_footers(self):
		"""P1-3: Merged USD bills keep cents, partner currency, formatted outstanding, and attributed tenders/EFRIS."""
		def multi_money(value, currency=None):
			num = float(value or 0)
			if currency == "USD":
				return f"$ {num:,.2f}"
			return f"Sh {round(num):,}"

		class BadPartnerRow(Doc):
			"""Simulates URY Merged POS Invoice Detail falling back to company currency UGX when passed primary doc."""
			def get_formatted(self, fieldname, parent=None, *args, **kwargs):
				return f"Sh {getattr(self, fieldname)}"

		partner = make_bill(
			name="HB-00001",
			currency="USD",
			grand_total=8.40,
			rounded_total=8.40,
			payments=[Doc(mode_of_payment="Card", amount=8.40, account="Card - HB")],
			paid_amount=8.40,
			change_amount=0,
			outstanding_amount=4.25,
			customer="Corp Account",
		)
		primary = make_bill(
			name="HB-00002",
			currency="USD",
			items=[
				Doc(item_code="STEAK", item_name="Ribeye", description="", comment="", qty=1, rate=12.75, amount=12.75)
			],
			total=12.75,
			net_total=12.75,
			grand_total=12.75,
			rounded_total=12.75,
			payments=[Doc(mode_of_payment="Cash", amount=12.75, account="Cash - HB")],
			paid_amount=12.75,
			change_amount=0,
			outstanding_amount=0,
			custom_merged_pos_invoice="HB-00001",
			custom_merged_total=8.40,
			custom_merged_pos_invoice_details=[
				BadPartnerRow(item_name="Truffle Parmesan Fries", comment="", qty=2, rate=4.20, amount=8.40)
			],
			ury_print_partner=partner,
		)
		html = render_format(
			MERGED,
			primary,
			efris={
				"HB-00002": {"state": "not_efris", "sandbox": 0},
				"HB-00001": {"state": "not_efris", "sandbox": 0},
			},
			format_bill_money=multi_money,
		)
		text = flat_text(html)
		# 1. Partner item must format in USD ($ 8.40 / @ $ 4.20), never company default "Sh 8.40"
		self.assertIn("$ 8.40", text)
		self.assertIn("@ $ 4.20", text)
		self.assertNotIn("Sh 8.40", text)
		self.assertNotIn("Sh 4.20", text)
		# 2. Combined total must preserve exact cents ($ 21.15), never truncate to $ 21.00
		self.assertIn("$ 21.15", text)
		self.assertNotIn("$ 21.00", text)
		self.assertNotIn("$ 8.00", text)
		# 3. Outstanding balance formatted via _money, never raw float "4.25 USD"
		self.assertIn("Charged to account Corp Account: $ 4.25", text)
		self.assertNotIn("4.25 USD", text)
		# 4. Tenders attributed per check on merged bills
		self.assertIn("HB-00002", text)
		self.assertIn("HB-00001", text)
		self.assertRegex(text, r"HB-00001.*Card")
		# 5. "Not a fiscal receipt" printed ONCE (not duplicated) when both halves are non-fiscal
		self.assertEqual(text.count("Not a fiscal receipt"), 1, f"Duplicate non-fiscal notice in: {text}")

		# Cross-currency merged bill must not sum incompatible currencies into a bogus COMBINED TOTAL
		ugx_partner = make_bill(
			name="HB-UGX",
			currency="UGX",
			grand_total=30000,
			rounded_total=30000,
			payments=[Doc(mode_of_payment="Cash", amount=30000, account="Cash - HB")],
			paid_amount=30000,
			change_amount=0,
			outstanding_amount=0,
		)
		mixed = make_bill(
			name="HB-USD",
			currency="USD",
			grand_total=12.75,
			rounded_total=12.75,
			custom_merged_pos_invoice="HB-UGX",
			custom_merged_total=30000,
			custom_merged_pos_invoice_details=[
				BadPartnerRow(item_name="Local Juice", comment="", qty=1, rate=30000, amount=30000)
			],
			ury_print_partner=ugx_partner,
		)
		mixed_text = flat_text(render_format(MERGED, mixed, format_bill_money=multi_money))
		self.assertIn("Sh 30,000", mixed_text)
		self.assertNotIn("COMBINED TOTAL", mixed_text)

	def test_check_metadata_modifiers_service_charge_taxes_and_tenders(self):
		doc = make_bill(
			discount_amount=5500,
			additional_discount_percentage=10,
			net_total=49500,
			taxes=[
				Doc(description="Service Charge", account_head="Service Charge - GT", rate=10, tax_amount=4950, included_in_print_rate=0),
				Doc(description="Output VAT", account_head="Output VAT 18% - GT", rate=18, tax_amount=8910, included_in_print_rate=0),
			],
			total_taxes_and_charges=13860,
			grand_total=63360,
			rounded_total=63400,
			rounding_adjustment=40,
			payments=[Doc(mode_of_payment="Cash", amount=70000, account="Cash - GT")],
			paid_amount=70000,
			change_amount=6600,
		)
		html = render_format(MERGED, doc)
		text = flat_text(html)
		for expected in (
			"POS-INV-0101",
			"#101",
			"2026-11-24",
			"13:15",
			"Table T-04",
			"Covers",
			"Window Booth",
			"Samuel Otieno",
			"Alice Nakato",
			"Grilled Lake Tilapia",
			"No salt, extra lemon",
			"@ 25000",
			"Subtotal",
			"Discount",
			"10%",
			"Service Charge",
			"Output VAT",
			"18%",
			"Rounding",
			"TOTAL",
			"Cash",
			"Change",
		):
			self.assertIn(expected, text, f"Missing guest-check element: {expected}")
		# Modifiers are indented under the item line
		self.assertIn('class="mod"', html)
		self.assertNotIn("Tip", text)

	def test_arabic_and_long_names_render_with_rtl_auto_direction_and_wrapping(self):
		long_arabic_item = "طبق السمك المشوي الملكي مع الأرز بالزعفران — " + ("X" * 90)
		doc = make_bill(
			customer_name="السيدة فاطمة المنصور والعائلة الكرام",
			custom_guest_label="طاولة كبار الشخصيات",
			items=[
				Doc(
					item_code="LONG-1",
					item_name=long_arabic_item,
					description="",
					comment="بدون ملح، ليمون إضافي",
					qty=1,
					rate=42000,
					amount=42000,
				)
			],
		)
		html = render_format(MERGED, doc)
		self.assertIn('dir="auto"', html)
		self.assertIn("السيدة فاطمة المنصور والعائلة الكرام", html)
		self.assertIn(long_arabic_item, html)
		self.assertIn("بدون ملح، ليمون إضافي", html)


class TestBillCopyAndWaiterSlipAlignment(unittest.TestCase):
	def test_bill_copy_reuses_sr_layout_and_v16_safe_copy_log_query(self):
		data = spec(COPY)
		html = data["html"]
		for key in ("margin_top", "margin_bottom", "margin_left", "margin_right"):
			self.assertEqual(data[key], 0, f"URY Bill Copy {key} must be 0")
		self.assertIn('class="sr"', html)
		self.assertIn("overflow-wrap: anywhere", html)
		# Frappe v16 forbids raw SQL function strings like "max(copy_number)" in get_value
		self.assertNotIn("max(copy_number)", html)

		rendered = render_format(COPY, make_bill(copy_number=2))
		text = flat_text(rendered)
		self.assertIn("COPY #2", text)
		self.assertIn("Grand Terrace Ltd", text)
		self.assertIn("TIN: 1000456789", text)
		self.assertIn("No salt, extra lemon", text)
		for forbidden in ("URY", "HUF", "Kamra", "Tridz"):
			self.assertNotIn(forbidden, text)

	def test_bill_copy_matches_guest_check_totals_discount_change_rate_and_clean_timestamps(self):
		"""P2-1 & P1-5: URY Bill Copy renders Subtotal, Discount, Service Charge, @ rate, Change, Letter Head/Footer, and clean timestamps."""
		def ugx_money(value, currency=None):
			return f"Sh {round(float(value or 0)):,}"

		doc = make_bill(
			copy_number=1,
			copy_timestamp="2026-11-24 11:25:46.919649",
			posting_date="2026-11-24",
			posting_time="11:20:00.123456",
			total=55000.0,
			net_total=49500.0,
			discount_amount=7040.0,
			additional_discount_percentage=10,
			taxes=[
				Doc(description="Service Charge", account_head="Service Charge - GT", rate=10, tax_amount=5500.0, included_in_print_rate=0),
				Doc(description="Output VAT", account_head="Output VAT 18% - GT", rate=18, tax_amount=9900.0, included_in_print_rate=0),
			],
			grand_total=63360.0,
			rounded_total=63360.0,
			payments=[Doc(mode_of_payment="Cash", amount=70000.0, account="Cash - GT")],
			paid_amount=70000.0,
			change_amount=6640.0,
			outstanding_amount=0.0,
		)
		html = render_format(
			COPY,
			doc,
			letter_head='<div class="custom-lh">Fine Dining Header</div>',
			footer="Thank you for dining at Grand Terrace<br>Wi-Fi: TerraceGuest",
			no_letterhead=False,
			format_bill_money=ugx_money,
		)
		text = flat_text(html)
		for expected in (
			"COPY #1",
			"Fine Dining Header",
			"2026-11-24 11:25:46",
			"11:20",
			"@ Sh 25,000",
			"Subtotal",
			"Sh 55,000",
			"Discount (10%)",
			"-Sh 7,040",
			"Service Charge",
			"Sh 5,500",
			"Output VAT (18%)",
			"Sh 9,900",
			"Sh 63,360",
			"Cash: Sh 70,000",
			"Change: Sh 6,640",
			"Thank you for dining at Grand Terrace",
			"Wi-Fi: TerraceGuest",
		):
			self.assertIn(expected, text, f"Missing {expected!r} in URY Bill Copy: {text}")
		for forbidden in (".919649", ".123456", "55000.0", "63360.0", "Outstanding"):
			self.assertNotIn(forbidden, text, f"Unexpected {forbidden!r} in URY Bill Copy: {text}")

		# Respects no_letterhead=True
		suppressed = render_format(
			COPY,
			doc,
			letter_head='<div class="custom-lh">Fine Dining Header</div>',
			footer="Custom Footer",
			no_letterhead=True,
			format_bill_money=ugx_money,
		)
		self.assertNotIn("Fine Dining Header", suppressed)
		self.assertNotIn("Custom Footer", suppressed)

	def test_room_charge_voucher_room_fallback_and_nowrap_label(self):
		"""P2-2: Room Charge voucher shows Room fallback when custom room_number is absent and never wraps Charged to room."""
		def ugx_money(value, currency=None):
			return f"Sh {round(float(value or 0)):,}"

		for fmt in (MERGED, COPY):
			with self.subTest(fmt=fmt):
				suite_doc = make_bill(
					room_number=None,
					kamra_ref=None,
					custom_guest_label="Suite 304 — Guest",
					payments=[Doc(mode_of_payment="Room Charge", amount=55000, account="Guest Ledger Clearing - GT")],
					grand_total=55000,
					rounded_total=55000,
				)
				html = render_format(fmt, suite_doc, format_bill_money=ugx_money)
				text = flat_text(html)
				self.assertIn("ROOM CHARGE", text)
				self.assertIn("Room : Suite 304", text)
				self.assertIn("Charged to room : Sh 55,000", text)
				self.assertIn("rc-l", html)
				self.assertIn("white-space: nowrap", html)

				# Without any room hint, still prints a Room : fill-in line rather than omitting Room
				no_room_doc = make_bill(
					room_number=None,
					kamra_ref=None,
					custom_guest_label="Ada Guest",
					payments=[Doc(mode_of_payment="Room Charge", amount=55000, account="Guest Ledger Clearing - GT")],
					grand_total=55000,
					rounded_total=55000,
				)
				no_room_text = flat_text(render_format(fmt, no_room_doc, format_bill_money=ugx_money))
				self.assertIn("Room :", no_room_text)
				self.assertIn("__________", no_room_text)

	def test_waiter_slip_never_prints_none_for_empty_optional_fields(self):
		data = spec(WAITER_SLIP)
		html = data["html"]
		self.assertIn("box-sizing: border-box", html)
		self.assertIn("overflow-wrap", html)
		env = SandboxedEnvironment(undefined=DebugUndefined, autoescape=False)
		env.globals.update(
			_=lambda s: s,
			frappe=SimpleNamespace(get_fullname=lambda user=None: user or ""),
		)
		kot = Doc(
			invoice=None,
			restaurant_table=None,
			custom_guest_label=None,
			customer_name=None,
			order_no=None,
			waiter=None,
			date=None,
			time=None,
			kot_items=[
				Doc(display_mode="single_qty", cancelled_qty=0, item="TEA", item_name="Masala Tea", quantity=2),
			],
		)
		rendered = env.from_string(html).render(doc=kot)
		text = flat_text(rendered)
		self.assertNotIn("None", text)
		self.assertNotIn("Invoice:", text)
		self.assertNotIn("Customer:", text)
		self.assertNotIn("Date:", text)
		self.assertNotIn("Time:", text)
		self.assertIn("Masala Tea", text)


class TestDefaultPrintFormatAndHardening(unittest.TestCase):
	def _import_defaults(self):
		fake_frappe = types.ModuleType("frappe")
		fake_frappe._ = lambda s: s
		fake_frappe.db = MagicMock()
		fake_frappe.get_all = MagicMock(return_value=[])
		fake_frappe.make_property_setter = MagicMock()
		fake_frappe.clear_cache = MagicMock()
		fake_frappe.get_meta = MagicMock()
		fake_frappe.ValidationError = type("ValidationError", (Exception,), {})
		fake_frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
		fake_frappe.utils = SimpleNamespace(cint=lambda v: int(v or 0))
		with patch.dict(sys.modules, {"frappe": fake_frappe, "frappe.utils": fake_frappe.utils}):
			sys.modules.pop("ury.ury.print_format.defaults", None)
			mod = importlib.import_module("ury.ury.print_format.defaults")
			return mod, fake_frappe

	def test_defaults_module_lists_all_unsafe_erpnext_pos_formats_and_resolves_safely(self):
		mod, _ = self._import_defaults()
		self.assertEqual(mod.GUEST_CHECK_PRINT_FORMAT, "Merged POS Invoice Format")
		for unsafe in (
			"POS Invoice Classic",
			"POS Invoice Bordered",
			"POS Invoice Modern",
			"POS Invoice Modern with Images",
			"POS Invoice",
			"Return POS Invoice",
		):
			self.assertIn(unsafe, mod.UNSAFE_POS_INVOICE_PRINT_FORMATS)
			self.assertTrue(mod.is_unsafe_pos_invoice_format(unsafe))
			self.assertEqual(mod.resolve_safe_pos_print_format(unsafe), "Merged POS Invoice Format")

		self.assertEqual(mod.resolve_safe_pos_print_format(None), "Merged POS Invoice Format")
		self.assertEqual(mod.resolve_safe_pos_print_format(""), "Merged POS Invoice Format")
		self.assertEqual(mod.resolve_safe_pos_print_format("Restaurant Receipt 80mm"), "Restaurant Receipt 80mm")

	def test_configure_preserves_retail_profiles_and_does_not_globally_disable_standard_formats(self):
		"""P1-4: Do not globally disable ERPNext/EFRIS Print Formats or coerce non-hospitality retail POS Profiles."""
		mod, fake_frappe = self._import_defaults()
		existing = set(mod.UNSAFE_POS_INVOICE_PRINT_FORMATS) | {"Merged POS Invoice Format"}
		fake_frappe.db.exists.side_effect = lambda dt, name: dt == "Print Format" and name in existing
		fake_frappe.db.get_value.side_effect = lambda dt, flt, field=None: (
			"POS Invoice Classic" if dt == "Property Setter" else None
		)
		fake_frappe.get_all.return_value = [
			SimpleNamespace(name="POS-Hosp-Blank", print_format=None, restaurant="Grand Terrace", branch="Main Dining"),
			SimpleNamespace(name="POS-Hosp-Classic", print_format="POS Invoice Classic", restaurant="Grand Terrace", branch="Main Dining"),
			SimpleNamespace(name="POS-Hosp-Custom", print_format="Restaurant Receipt 80mm", restaurant="Grand Terrace", branch="Main Dining"),
			SimpleNamespace(name="Retail Till POS", print_format="POS Invoice Classic", restaurant=None, branch=None),
			SimpleNamespace(name="Retail Blank POS", print_format=None, restaurant="", branch=""),
		]

		mod.configure_pos_invoice_print_formats()

		# 1. Must NEVER globally set Print Format.disabled = 1 on standard formats
		disabled_calls = [
			c.args for c in fake_frappe.db.set_value.call_args_list
			if c.args[0] == "Print Format" and c.args[2] == "disabled" and c.args[3] == 1
		]
		self.assertEqual(disabled_calls, [], f"Must not globally disable standard Print Formats: {disabled_calls}")

		# 2. Default Property Setter still sets POS Invoice -> Merged POS Invoice Format
		fake_frappe.make_property_setter.assert_called()
		ps_arg = fake_frappe.make_property_setter.call_args.args[0]
		self.assertEqual(ps_arg["doctype"], "POS Invoice")
		self.assertEqual(ps_arg["property"], "default_print_format")
		self.assertEqual(ps_arg["value"], "Merged POS Invoice Format")

		# 3. Hospitality blank/unsafe profiles migrated; hospitality custom AND retail profiles untouched
		profile_updates = {
			c.args[1]: c.args[3]
			for c in fake_frappe.db.set_value.call_args_list
			if c.args[0] == "POS Profile" and c.args[2] == "print_format"
		}
		self.assertEqual(profile_updates.get("POS-Hosp-Blank"), "Merged POS Invoice Format")
		self.assertEqual(profile_updates.get("POS-Hosp-Classic"), "Merged POS Invoice Format")
		self.assertNotIn("POS-Hosp-Custom", profile_updates)
		self.assertNotIn("Retail Till POS", profile_updates)
		self.assertNotIn("Retail Blank POS", profile_updates)

	def test_validate_pos_invoice_print_format_scopes_unsafe_block_to_hospitality_and_enforces_disabled_globally(self):
		"""P1-4: Hospitality POS Invoices reject unsafe formats; retail profiles stay usable unless disabled=1."""
		mod, fake_frappe = self._import_defaults()
		fake_frappe.throw = MagicMock(side_effect=lambda msg, exc=None: (_ for _ in ()).throw((exc or Exception)(msg)))
		fake_frappe.local = SimpleNamespace(db=fake_frappe.db)

		def fake_get_value(dt, name, field=None, **kw):
			if dt == "Print Format":
				return 1 if name == "Disabled Custom Format" else 0
			if dt == "POS Profile":
				if name == "Main Dining POS":
					return {"restaurant": "Grand Terrace", "branch": "Main Dining"}
				return {"restaurant": None, "branch": None}
			return None

		fake_frappe.db.get_value.side_effect = fake_get_value

		hosp_inv = Doc(doctype="POS Invoice", restaurant="Grand Terrace", branch="Main Dining", pos_profile="Main Dining POS")
		retail_inv = Doc(doctype="POS Invoice", restaurant=None, branch=None, pos_profile="Retail Till POS")

		# Hospitality invoice refuses POS Invoice Classic & POS EFRIS Invoice
		with self.assertRaises(fake_frappe.DoesNotExistError):
			mod.validate_pos_invoice_print_format(hosp_inv, "POS Invoice Classic")
		with self.assertRaises(fake_frappe.DoesNotExistError):
			mod.validate_pos_invoice_print_format(hosp_inv, "POS EFRIS Invoice")

		# Retail invoice is allowed to use enabled POS Invoice Classic
		mod.validate_pos_invoice_print_format(retail_inv, "POS Invoice Classic")

		# Any explicitly disabled format is refused for BOTH hospitality and retail
		for inv in (hosp_inv, retail_inv):
			with self.assertRaises(fake_frappe.DoesNotExistError):
				mod.validate_pos_invoice_print_format(inv, "Disabled Custom Format")

	def test_pos_profile_validate_allows_retail_profile_to_keep_or_revert_classic_format(self):
		"""P1-4: Retail POS Profile (no restaurant/branch) can save/revert POS Invoice Classic; hospitality cannot."""
		mod, fake_frappe = self._import_defaults()
		fake_frappe.throw = MagicMock(side_effect=lambda msg, exc=None: (_ for _ in ()).throw((exc or Exception)(msg)))
		fake_frappe.msgprint = MagicMock()
		fake_frappe.local = SimpleNamespace(db=fake_frappe.db)
		fake_frappe.db.get_value.return_value = 0
		fake_frappe.db.sql.return_value = [(1,)]

		with patch.dict(sys.modules, {"frappe": fake_frappe, "frappe.utils": fake_frappe.utils}):
			sys.modules.pop("ury.ury.hooks.ury_pos_profile", None)
			hook_mod = importlib.import_module("ury.ury.hooks.ury_pos_profile")

			retail_prof = Doc(name="Retail Till POS", restaurant=None, branch=None, print_format="POS Invoice Classic")
			hook_mod.validate_print_format(retail_prof)
			self.assertEqual(retail_prof.print_format, "POS Invoice Classic")

			hosp_prof = Doc(name="Main Dining POS", restaurant="Grand Terrace", branch="Main Dining", print_format="POS Invoice Classic")
			with self.assertRaises(fake_frappe.ValidationError):
				hook_mod.validate_print_format(hosp_prof)

	def test_resolve_bill_logo_src_inlines_local_images_without_network_urls_and_blocks_traversal(self):
		"""P1-5: Local logos inline as data: URIs so wkhtmltopdf never hits ConnectionRefusedError; traversal rejected."""
		import tempfile

		fake_frappe = types.ModuleType("frappe")
		fake_frappe.db = MagicMock()
		fake_frappe.utils = SimpleNamespace(cint=lambda v: int(v or 0), fmt_money=lambda v, **kw: str(v))
		with tempfile.TemporaryDirectory() as tmp:
			site_dir = Path(tmp) / "sites" / "pos.localhost"
			pub_files = site_dir / "public" / "files"
			pub_files.mkdir(parents=True)
			logo_file = pub_files / "grand-terrace.png"
			logo_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
			secret_file = site_dir / "site_config.json"
			secret_file.write_text('{"db_password": "secret"}')

			fake_frappe.get_site_path = lambda *parts: str(site_dir.joinpath(*parts))
			with patch.dict(sys.modules, {"frappe": fake_frappe, "frappe.utils": fake_frappe.utils}):
				sys.modules.pop("ury.ury.print_format.utils", None)
				utils_mod = importlib.import_module("ury.ury.print_format.utils")

				inlined = utils_mod.resolve_bill_logo_src("/files/grand-terrace.png")
				self.assertTrue(inlined.startswith("data:image/png;base64,"), inlined)
				# Missing file returns relative path (never host_name URL)
				self.assertEqual(utils_mod.resolve_bill_logo_src("/files/missing.png"), "/files/missing.png")
				# Path traversal / non-image / javascript scheme blocked
				self.assertEqual(utils_mod.resolve_bill_logo_src("/files/../../site_config.json"), "")
				self.assertEqual(utils_mod.resolve_bill_logo_src("javascript:alert(1)"), "")

		# Neither template may call frappe.utils.get_url on logo
		self.assertNotIn("frappe.utils.get_url", spec(MERGED)["html"])
		self.assertNotIn("frappe.utils.get_url", spec(COPY)["html"])

	def test_before_print_rejects_client_copy_number_and_timestamp_spoofing_and_has_no_frame_walk(self):
		"""P1-1 & P1-4: CustomPOSInvoice.before_print never trusts client copy_number/copy_timestamp and avoids inspect.currentframe."""
		pos_inv_src = (APP_ROOT / "ury/class_overrides/pos_invoice.py").read_text()
		self.assertNotIn("inspect.currentframe", pos_inv_src)
		self.assertNotIn("f_locals", pos_inv_src)

		fake_frappe = types.ModuleType("frappe")
		fake_frappe._ = lambda s: s
		fake_frappe.form_dict = {}
		fake_frappe.db = MagicMock()
		fake_frappe.db.exists.return_value = True
		fake_frappe.db.get_value.side_effect = lambda dt, flt, field=None, **kw: (
			3 if dt == "URY Bill Reprint Log" else 0
		)
		fake_frappe.local = SimpleNamespace(db=fake_frappe.db)
		fake_frappe.ValidationError = type("ValidationError", (Exception,), {})
		fake_frappe.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
		fake_frappe.throw = MagicMock(side_effect=lambda msg, exc=None: (_ for _ in ()).throw((exc or Exception)(msg)))
		fake_frappe.utils = SimpleNamespace(cint=lambda v: int(v) if str(v).lstrip("-").isdigit() else 0, flt=lambda v: float(v or 0))

		saved = Doc(name="INV-99", docstatus=1, custom_merged_pos_invoice=None, restaurant="Grand Terrace", branch="Main", pos_profile="Main POS")
		fake_frappe.get_doc = MagicMock(return_value=saved)

		class _BasePOSInvoice:
			def before_print(self, settings=None):
				pass

		fake_erpnext = types.ModuleType("erpnext")
		fake_erpnext_acc = types.ModuleType("erpnext.accounts")
		fake_erpnext_dt = types.ModuleType("erpnext.accounts.doctype")
		fake_erpnext_pi = types.ModuleType("erpnext.accounts.doctype.pos_invoice")
		fake_erpnext_pi_mod = types.ModuleType("erpnext.accounts.doctype.pos_invoice.pos_invoice")
		fake_erpnext_pi_mod.POSInvoice = _BasePOSInvoice
		fake_erpnext_pi_mod.get_stock_availability = MagicMock()
		fake_order_mod = types.ModuleType("ury.ury.doctype.ury_order.ury_order")
		fake_order_mod.check_split_target_access = MagicMock()

		with patch.dict(sys.modules, {
			"frappe": fake_frappe,
			"frappe.utils": fake_frappe.utils,
			"erpnext": fake_erpnext,
			"erpnext.accounts": fake_erpnext_acc,
			"erpnext.accounts.doctype": fake_erpnext_dt,
			"erpnext.accounts.doctype.pos_invoice": fake_erpnext_pi,
			"erpnext.accounts.doctype.pos_invoice.pos_invoice": fake_erpnext_pi_mod,
			"ury.ury.doctype.ury_order.ury_order": fake_order_mod,
		}):
			sys.modules.pop("ury.ury.print_format.defaults", None)
			sys.modules.pop("ury.ury.class_overrides.pos_invoice", None)
			pi_mod = importlib.import_module("ury.ury.class_overrides.pos_invoice")

			# 1. Client payload without _COPY_RENDER_MARKER cannot spoof copy_number or copy_timestamp
			client_doc = object.__new__(pi_mod.CustomPOSInvoice)
			client_doc.__dict__.update(
				name="INV-99", docstatus=1, restaurant="Grand Terrace", branch="Main", pos_profile="Main POS",
				copy_number='<img src=x onerror="alert(1)">',
				copy_timestamp='<script>alert(1)</script>',
				outstanding_amount=0, custom_merged_pos_invoice=None,
			)
			client_doc.get = lambda k, d=None: client_doc.__dict__.get(k, d)
			pi_mod.CustomPOSInvoice.before_print(client_doc)
			self.assertIsNone(client_doc.copy_number)
			self.assertIsNone(client_doc.copy_timestamp)

			# 2. Internal copy render with _COPY_RENDER_MARKER preserves integer copy_number & server copy_timestamp
			server_doc = object.__new__(pi_mod.CustomPOSInvoice)
			server_doc.__dict__.update(
				name="INV-99", docstatus=1, restaurant="Grand Terrace", branch="Main", pos_profile="Main POS",
				_ury_copy_render=pi_mod._COPY_RENDER_MARKER,
				copy_number=2,
				copy_timestamp="2026-11-24 10:00:00",
				outstanding_amount=0, custom_merged_pos_invoice=None,
			)
			server_doc.get = lambda k, d=None: server_doc.__dict__.get(k, d)
			pi_mod.CustomPOSInvoice.before_print(server_doc)
			self.assertEqual(server_doc.copy_number, 2)
			self.assertEqual(server_doc.copy_timestamp, "2026-11-24 10:00:00")

	def test_copy_print_helpers_and_network_endpoints_preserve_letterhead_and_reject_unsafe_formats(self):
		"""P1-5: Bill copy URL/HTML forward POS Profile letter_head; network/socket print endpoints reject unsafe formats."""
		api_src = (APP_ROOT / "ury_pos/api.py").read_text()
		ury_print_src = (APP_ROOT / "ury/api/ury_print.py").read_text()
		service_src = (APP_ROOT / "ury/printing/service.py").read_text()
		pos_print_js = (APP_ROOT / "public/js/pos_print.js").read_text()
		ury_order_js = (APP_ROOT / "ury/doctype/ury_order/ury_order.js").read_text()
		legacy_vue = (REPO_ROOT / "urypos/src/stores/invoiceData.js").read_text()

		# Copy print URL & HTML helpers must forward configured letter_head instead of hardcoding no_letterhead=1
		self.assertIn("_resolve_invoice_letter_head", api_src)
		self.assertNotIn("no_letterhead=1&trigger_print=1&copy_number={copy_number}", api_src)

		# Network & socket print endpoints validate explicitly passed print_format on POS Invoice
		self.assertIn("validate_pos_invoice_print_format", ury_print_src)
		self.assertIn("validate_pos_invoice_print_format", service_src)

		# Legacy JS/Desk/Vue print clients resolve unsafe formats and forward letter_head
		for label, src in (
			("pos_print.js", pos_print_js),
			("ury_order.js", ury_order_js),
			("urypos/invoiceData.js", legacy_vue),
		):
			with self.subTest(client=label):
				self.assertIn("Merged POS Invoice Format", src, f"{label} must fall back to safe guest-check format")
				self.assertIn("POS Invoice Classic", src, f"{label} must reject unsafe POS Invoice formats")
				self.assertIn("letter_head", src, f"{label} must forward configured letter_head")

	def test_configure_preserves_existing_safe_custom_default_property_setter(self):
		mod, fake_frappe = self._import_defaults()
		fake_frappe.db.exists.return_value = True
		fake_frappe.db.get_value.side_effect = lambda dt, flt, field=None: (
			"Restaurant Receipt 80mm" if dt == "Property Setter"
			else (0 if dt == "Print Format" and flt == "Restaurant Receipt 80mm" else 1)
		)
		fake_frappe.get_all.return_value = []

		mod.configure_pos_invoice_print_formats()
		fake_frappe.make_property_setter.assert_not_called()

	def test_hooks_install_patches_and_fallbacks_are_wired(self):
		hooks_src = (APP_ROOT / "hooks.py").read_text()
		install_src = (APP_ROOT / "install.py").read_text()
		patches_txt = (APP_ROOT / "patches.txt").read_text()
		pos_inv_src = (APP_ROOT / "ury/class_overrides/pos_invoice.py").read_text()
		pos_prof_hook_src = (APP_ROOT / "ury/hooks/ury_pos_profile.py").read_text()
		ury_print_src = (APP_ROOT / "ury/api/ury_print.py").read_text()
		service_src = (APP_ROOT / "ury/printing/service.py").read_text()
		seed_prof_src = (APP_ROOT / "ury/dev_seed/profiles.py").read_text()
		setup_src = (APP_ROOT / "ury/api/minimal/business_setup.py").read_text()

		# P1-4: Do NOT re-run configure_pos_invoice_print_formats on every after_migrate (keeps Desk changes reversible)
		self.assertNotIn("ury.ury.print_format.defaults.configure_pos_invoice_print_formats", hooks_src)
		self.assertIn("ury.ury.print_format.utils.resolve_bill_logo_src", hooks_src)
		self.assertIn("frappe.www.printview.get_html_and_style", hooks_src)
		self.assertIn("frappe.utils.print_format.download_pdf", hooks_src)
		self.assertIn("configure_pos_invoice_print_formats", install_src)
		self.assertIn("ury.patches.v3_45.configure_pos_invoice_guest_check_print_format", patches_txt)
		self.assertTrue(
			(APP_ROOT / "patches/v3_45/configure_pos_invoice_guest_check_print_format.py").exists(),
			"Missing v3_45 migration patch",
		)
		# Frappe v16 SQL safety in CustomPOSInvoice.before_print
		self.assertNotIn('"max(copy_number)"', pos_inv_src)
		self.assertIn("validate_pos_invoice_print_format", pos_inv_src)
		# POS Profile validate guards print_format
		self.assertIn("validate_print_format", pos_prof_hook_src)
		# No hardcoded fallback to unsafe ERPNext "POS Invoice" format
		self.assertNotIn('print_format = "POS Invoice"', ury_print_src)
		self.assertNotIn('resolved_format = "POS Invoice"', service_src)
		self.assertIn("Merged POS Invoice Format", seed_prof_src)
		self.assertIn("Merged POS Invoice Format", setup_src)


if __name__ == "__main__":
	unittest.main()
