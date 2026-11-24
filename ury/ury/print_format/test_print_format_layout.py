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
		self.assertIn("https://pos.example/files/grand-terrace-logo.svg", html)
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

	def test_configure_disables_unsafe_formats_sets_default_and_migrates_unsafe_profiles(self):
		mod, fake_frappe = self._import_defaults()
		existing = set(mod.UNSAFE_POS_INVOICE_PRINT_FORMATS) | {"Merged POS Invoice Format"}
		fake_frappe.db.exists.side_effect = lambda dt, name: dt == "Print Format" and name in existing
		fake_frappe.db.get_value.side_effect = lambda dt, flt, field=None: (
			"POS Invoice Classic" if dt == "Property Setter" else None
		)
		fake_frappe.get_all.return_value = [
			SimpleNamespace(name="POS-Blank", print_format=None),
			SimpleNamespace(name="POS-Classic", print_format="POS Invoice Classic"),
			SimpleNamespace(name="POS-Custom", print_format="Restaurant Receipt 80mm"),
		]

		mod.configure_pos_invoice_print_formats()

		disabled_calls = [
			c.args for c in fake_frappe.db.set_value.call_args_list
			if c.args[0] == "Print Format" and c.args[2] == "disabled" and c.args[3] == 1
		]
		disabled_names = {c[1] for c in disabled_calls}
		for unsafe in (
			"POS Invoice Classic",
			"POS Invoice Bordered",
			"POS Invoice Modern",
			"POS Invoice Modern with Images",
			"POS Invoice",
			"Return POS Invoice",
		):
			self.assertIn(unsafe, disabled_names)

		# Default Property Setter created for POS Invoice -> Merged POS Invoice Format
		fake_frappe.make_property_setter.assert_called()
		ps_arg = fake_frappe.make_property_setter.call_args.args[0]
		self.assertEqual(ps_arg["doctype"], "POS Invoice")
		self.assertEqual(ps_arg["property"], "default_print_format")
		self.assertEqual(ps_arg["value"], "Merged POS Invoice Format")

		# Blank & unsafe POS Profiles migrated; safe custom profile untouched
		profile_updates = {
			c.args[1]: c.args[3]
			for c in fake_frappe.db.set_value.call_args_list
			if c.args[0] == "POS Profile" and c.args[2] == "print_format"
		}
		self.assertEqual(profile_updates.get("POS-Blank"), "Merged POS Invoice Format")
		self.assertEqual(profile_updates.get("POS-Classic"), "Merged POS Invoice Format")
		self.assertNotIn("POS-Custom", profile_updates)

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

		self.assertIn("ury.ury.print_format.defaults.configure_pos_invoice_print_formats", hooks_src)
		self.assertIn("configure_pos_invoice_print_formats", install_src)
		self.assertIn("ury.patches.v3_45.configure_pos_invoice_guest_check_print_format", patches_txt)
		self.assertTrue(
			(APP_ROOT / "patches/v3_45/configure_pos_invoice_guest_check_print_format.py").exists(),
			"Missing v3_45 migration patch",
		)
		# Frappe v16 SQL safety in CustomPOSInvoice.before_print
		self.assertNotIn('"max(copy_number)"', pos_inv_src)
		self.assertIn("DoesNotExistError", pos_inv_src)
		# POS Profile validate guards print_format
		self.assertIn("validate_print_format", pos_prof_hook_src)
		# No hardcoded fallback to unsafe ERPNext "POS Invoice" format
		self.assertNotIn('print_format = "POS Invoice"', ury_print_src)
		self.assertNotIn('resolved_format = "POS Invoice"', service_src)
		self.assertIn("Merged POS Invoice Format", seed_prof_src)
		self.assertIn("Merged POS Invoice Format", setup_src)


if __name__ == "__main__":
	unittest.main()
