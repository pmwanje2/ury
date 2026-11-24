"""Site-less contract: URY's printed bills separate the guest check from the fiscal receipt (G16, G21).

Oracle Simphony prints a non-fiscal guest check (pro forma) during service and the
customer / fiscal receipt only when the check is tendered. URA EFRIS fiscalises a POS
Invoice on submit; uganda_compliance (frappe-siteops:rel-v16-b5d7b085,
apps/uganda_compliance) exposes the result to print formats through its Jinja hook
``efris_receipt_data(doc)`` (hooks.py ``jinja.methods``; efris/print_utils.py), whose
``state`` is ``not_efris``, ``fiscalised`` (``fdn``, ``verification_code`` = E Invoice
``antifake_code``, ``qr`` data URI), ``credit_note_pending`` or ``pending``. The EFRIS
tax-category summary is the E Invoice ``taxes`` table (E Invoice Tax Items:
``tax_category_code`` such as ``01:A: Standard (18%)``, ``net_amount``,
``tax_amount``), linked from POS Invoice ``efris_e_invoice``. The buyer TIN is the
native POS Invoice ``tax_id``.

A tenant without uganda_compliance has no ``efris_receipt_data`` global; its bills
print no EFRIS lines. A draft bill is the pro-forma guest check and never calls the
helper. A room-charge tender (``room_number``) prints Simphony's room charge voucher:
the room and a guest signature line.

Rendered whole, the way Frappe renders print formats: a Jinja SandboxedEnvironment with
DebugUndefined and autoescape off (frappe/utils/jinja.py, version-16), with the Jinja2
and MarkupSafe Frappe v16 pins (print-jinja-requirements.txt beside this file).
"""

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import DebugUndefined
from jinja2.sandbox import SandboxedEnvironment

HERE = Path(__file__).resolve().parent
MERGED = "merged_pos_invoice_format"
COPY = "ury_bill_copy"
FDN = "324013579864"
CODE = "47181263590461978813"
QR = "data:image/png;base64,iVBORw0KGgo="
TAXES = [SimpleNamespace(tax_category_code="01:A: Standard (18%)", net_amount=847.46, tax_amount=152.54,
                         gross_amount=1000.0)]
XSS = "<img src=x onerror=alert(1)>"


class Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def get_formatted(self, fieldname, *args, **kwargs):
		return str(getattr(self, fieldname, ""))


def spec(name):
	return json.loads((HERE / name / f"{name}.json").read_text())


def bill(name="POS-INV-0001", docstatus=1, **fields):
	values = dict(
		name=name, docstatus=docstatus, status="Paid" if docstatus else "Draft", is_return=0, posting_date="2026-11-23",
		customer="Walk In", customer_name="Walk In", currency="UGX", waiter=None, company="Demo Restaurant",
		items=[Doc(item_code="RICE", item_name="Rice", description="", qty=1, stock_uom="Nos", uom="Nos",
		           discount_amount=0, distributed_discount_amount=0, rate=1000, amount=1000)],
		payments=[Doc(mode_of_payment="Cash", amount=1000, account="Cash - DR")], taxes=[], custom_merged_pos_invoice=None,
		ury_print_partner=None, outstanding_amount=0, total_qty=1, net_total=847.46, grand_total=1000,
		rounded_total=1000, paid_amount=1000, change_amount=0, tax_id=None, efris_e_invoice=None,
		kamra_ref=None, room_number=None,
		copy_number=1, copy_timestamp="2026-11-23 12:34",
	)
	values.update(fields)
	return Doc(**values)


def fiscalised(**extra):
	return {"state": "fiscalised", "fdn": FDN, "verification_code": CODE, "qr": QR, "sandbox": 0, "is_return": 0, **extra}


def render(name, doc, efris=None, e_invoices=None, *, clearing="Guest Ledger Clearing - DR",
           guest_name="Jane Guest", copy_log_max=None, fmt_money=None):
	"""Render one shipped format; efris maps invoice name -> efris_receipt_data() result (None: app absent)."""
	calls = []

	def get_doc(doctype, docname):
		assert doctype == "E Invoice", doctype
		return (e_invoices or {})[docname]

	def get_value(doctype, filters_or_name, fieldname=None, as_dict=False, order_by=None):
		if doctype == "URY Bill Reprint Log":
			assert fieldname == "copy_number" and order_by == "copy_number desc", (
				f"Frappe v16 forbids SQL function strings in get_value; got fieldname={fieldname!r}, order_by={order_by!r}"
			)
			return copy_log_max
		if doctype == "Company" and fieldname == "custom_guest_ledger_clearing":
			return clearing
		if doctype == "Folio":
			return Doc(guest_name=guest_name, reservation="RES-1")
		if doctype == "Reservation" and fieldname == "room":
			return "ROOM-0204"
		if doctype == "Room" and fieldname == "room_number":
			return "204"
		return None

	env = SandboxedEnvironment(undefined=DebugUndefined, autoescape=False)
	env.globals.update(
		_=lambda text: text,
		frappe=SimpleNamespace(
			get_fullname=lambda user=None: user or "cashier@example.com",
			get_doc=get_doc,
			get_meta=lambda doctype: SimpleNamespace(
				has_field=lambda field: doctype == "Company" and field == "custom_guest_ledger_clearing" and clearing is not None
			),
			db=SimpleNamespace(get_value=get_value),
			utils=SimpleNamespace(formatdate=str, now=lambda: "2026-11-23 12:00",
			                      fmt_money=fmt_money or (lambda value, **kwargs: f"UGX {value}")),
		),
	)
	if efris is not None:
		def efris_receipt_data(target):
			calls.append(target.name)
			return efris[target.name]
		env.globals["efris_receipt_data"] = efris_receipt_data
	html = env.from_string(spec(name)["html"]).render(doc=doc)
	return html, calls


def text(html):
	return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


class MergedFormatFiscalReceipt(unittest.TestCase):
	def test_a_fiscalised_bill_prints_fdn_code_qr_buyer_tin_and_tax_categories(self):
		doc = bill(tax_id="1000123456", efris_e_invoice="EINV-1")
		html, calls = render(MERGED, doc, {doc.name: fiscalised()}, {"EINV-1": SimpleNamespace(taxes=TAXES)})
		flat = text(html)
		self.assertEqual(calls, [doc.name])
		self.assertIn("FISCAL RECEIPT", flat)
		self.assertNotIn("PENDING", flat)
		for needle in ("URA EFRIS", "FDN", FDN, "Verification code", CODE, "Buyer TIN", "1000123456",
		               "A: Standard (18%)", "UGX 152.54"):
			self.assertIn(needle, flat)
		self.assertIn(f'src="{QR}"', html)

	def test_a_pending_bill_says_so_and_never_claims_to_be_the_fiscal_receipt(self):
		doc = bill()
		flat = text(render(MERGED, doc, {doc.name: {"state": "pending", "sandbox": 0}})[0])
		self.assertIn("FISCAL RECEIPT PENDING", flat)
		self.assertIn("Not a tax invoice until URA issues the FDN", flat)
		self.assertNotIn("FDN :", flat)
		self.assertEqual(flat.count("FISCAL RECEIPT"), flat.count("FISCAL RECEIPT PENDING"))

	def test_a_non_fiscal_tenant_prints_no_efris_lines(self):
		flat = text(render(MERGED, bill())[0])
		self.assertIn("RECEIPT", flat)
		for needle in ("EFRIS", "FDN", "FISCAL", "Verification code"):
			self.assertNotIn(needle, flat)

	def test_an_efris_tenant_bill_outside_efris_is_marked_not_fiscal(self):
		doc = bill()
		flat = text(render(MERGED, doc, {doc.name: {"state": "not_efris", "sandbox": 0}})[0])
		self.assertIn("Not a fiscal receipt", flat)
		self.assertNotIn("FDN", flat)

	def test_a_draft_bill_is_the_pro_forma_guest_check_and_never_asks_for_fiscal_data(self):
		doc = bill(docstatus=0)
		html, calls = render(MERGED, doc, {doc.name: fiscalised()})
		flat = text(html)
		self.assertEqual(calls, [])
		self.assertIn("GUEST CHECK", flat)
		self.assertIn("PRO FORMA - NOT A RECEIPT", flat)
		self.assertNotIn("FISCAL", flat)
		self.assertNotIn(FDN, flat)

	def test_a_sandbox_fdn_is_marked_not_valid(self):
		doc = bill()
		flat = text(render(MERGED, doc, {doc.name: fiscalised(sandbox=1)})[0])
		self.assertIn("EFRIS SANDBOX - NOT A VALID TAX INVOICE", flat)

	def test_each_half_of_a_merged_bill_prints_its_own_fdn(self):
		partner = bill(name="POS-INV-0002")
		doc = bill(custom_merged_pos_invoice="POS-INV-0002", custom_merged_pos_invoice_details=[], custom_merged_total=0,
		           ury_print_partner=partner)
		efris = {doc.name: fiscalised(), partner.name: fiscalised(fdn="324013579999", verification_code="999")}
		html, calls = render(MERGED, doc, efris)
		self.assertEqual(calls, [doc.name, partner.name])
		self.assertIn(FDN, html)
		self.assertIn("324013579999", html)

	def test_a_room_charge_prints_the_room_and_a_guest_signature_line(self):
		rc = bill(
			room_number="101",
			payments=[Doc(mode_of_payment="Room Charge", amount=1000, account="Guest Ledger Clearing - DR")],
		)
		flat = text(render(MERGED, rc)[0])
		self.assertIn("ROOM CHARGE", flat)
		self.assertIn("Room : 101", flat)
		self.assertIn("Guest signature", flat)
		# Cash payment must not print a ROOM CHARGE voucher even if room_number or kamra_ref is present.
		cash_flat = text(render(MERGED, bill(room_number="101", kamra_ref="FOL-1"))[0])
		self.assertNotIn("ROOM CHARGE", cash_flat)
		self.assertNotIn("Guest signature", cash_flat)

	def test_merged_top_banner_is_pending_when_partner_is_pending_efris(self):
		partner = bill(name="POS-INV-0002")
		doc = bill(custom_merged_pos_invoice="POS-INV-0002", custom_merged_pos_invoice_details=[], custom_merged_total=0,
		           ury_print_partner=partner)
		efris = {doc.name: fiscalised(), partner.name: {"state": "pending", "sandbox": 0}}
		html, calls = render(MERGED, doc, efris)
		self.assertEqual(calls, [doc.name, partner.name])
		heading = text(re.search(r"<h4[^>]*>(.*?)</h4>", html, re.DOTALL).group(1))
		self.assertEqual(heading, "FISCAL RECEIPT PENDING")

	def test_merged_room_charge_resolves_partner_room_and_folio(self):
		partner = bill(
			name="POS-INV-0002",
			room_number="204",
			payments=[Doc(mode_of_payment="Guest Folio", amount=1000, account="Guest Ledger Clearing - DR")],
		)
		doc = bill(
			custom_merged_pos_invoice="POS-INV-0002", custom_merged_pos_invoice_details=[], custom_merged_total=1000,
			ury_print_partner=partner,
		)
		flat = text(render(MERGED, doc)[0])
		self.assertIn("ROOM CHARGE", flat)
		self.assertIn("Room : 204", flat)
		self.assertIn("Guest signature", flat)

		# Folio-only room service resolves room from Reservation -> Room and prints guest name.
		folio_doc = bill(
			kamra_ref="FOL-1",
			room_number=None,
			payments=[Doc(mode_of_payment="Guest Folio", amount=1000, account="Guest Ledger Clearing - DR")],
		)
		folio_flat = text(render(MERGED, folio_doc)[0])
		self.assertIn("ROOM CHARGE", folio_flat)
		self.assertIn("Room : 204", folio_flat)
		self.assertIn("Jane Guest", folio_flat)

	def test_pending_credit_note_uses_credit_note_pending_top_banner(self):
		for state in ("credit_note_pending", "pending"):
			with self.subTest(state=state):
				doc = bill(is_return=1)
				html = render(MERGED, doc, {doc.name: {"state": state, "application_ref": "CN-1", "sandbox": 0}})[0]
				heading = text(re.search(r"<h4[^>]*>(.*?)</h4>", html, re.DOTALL).group(1))
				self.assertEqual(heading, "CREDIT NOTE (PENDING URA APPROVAL)")
		approved_html = render(MERGED, bill(is_return=1), {bill().name: fiscalised(is_return=1, original_fdn="111")})[0]
		self.assertEqual(text(re.search(r"<h4[^>]*>(.*?)</h4>", approved_html, re.DOTALL).group(1)), "REFUND")

	def test_fiscal_and_room_values_are_escaped(self):
		doc = bill(
			room_number=XSS,
			tax_id=XSS,
			payments=[Doc(mode_of_payment="Room Charge", amount=1000, account="Guest Ledger Clearing - DR")],
		)
		html = render(MERGED, doc, {doc.name: fiscalised(fdn=XSS, verification_code=XSS)})[0]
		self.assertNotIn(XSS, html)
		self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

	def test_the_standard_format_is_re_synced_on_migrate(self):
		# Frappe re-imports a standard Print Format from its JSON only when `modified` advances.
		self.assertGreater(spec(MERGED)["modified"], "2026-11-23 22:00:00.000000")


class BillCopyFiscalReceipt(unittest.TestCase):
	def test_a_copy_of_a_fiscalised_bill_repeats_its_fdn_code_and_qr(self):
		doc = bill(efris_e_invoice="EINV-1")
		html, _calls = render(COPY, doc, {doc.name: fiscalised()}, {"EINV-1": SimpleNamespace(taxes=TAXES)})
		flat = text(html)
		for needle in ("COPY #1", FDN, CODE, "A: Standard (18%)"):
			self.assertIn(needle, flat)
		self.assertIn(f'src="{QR}"', html)

	def test_a_copy_without_in_memory_number_falls_back_to_log_or_hash_one(self):
		flat_default = text(render(COPY, bill(copy_number=None))[0])
		self.assertIn("COPY #1", flat_default)
		flat_logged = text(render(COPY, bill(copy_number=None), copy_log_max=5)[0])
		self.assertIn("COPY #5", flat_logged)

	def test_a_copy_of_a_merged_bill_includes_partner_items_payments_and_fdn(self):
		partner = bill(
			name="POS-INV-0002",
			items=[Doc(item_code="TEA", item_name="Masala Tea", qty=2, amount=4000)],
			payments=[Doc(mode_of_payment="Mobile Money", amount=4000, account="MoMo - DR")],
			grand_total=4000, rounded_total=4000,
		)
		doc = bill(custom_merged_pos_invoice="POS-INV-0002", ury_print_partner=partner)
		efris = {doc.name: fiscalised(), partner.name: fiscalised(fdn="324013579999", verification_code="VC-PARTNER")}
		html, calls = render(COPY, doc, efris)
		flat = text(html)
		self.assertEqual(calls, [doc.name, partner.name])
		for needle in ("POS-INV-0001", "Rice", FDN, "POS-INV-0002", "Masala Tea", "Mobile Money", "324013579999", "VC-PARTNER"):
			self.assertIn(needle, flat, needle)

	def test_a_copy_of_a_pending_bill_stays_pending(self):
		doc = bill()
		flat = text(render(COPY, doc, {doc.name: {"state": "pending", "sandbox": 0}})[0])
		self.assertIn("FISCAL RECEIPT PENDING", flat)
		self.assertNotIn(FDN, flat)

	def test_a_copy_of_a_pending_return_shows_credit_note_pending(self):
		for state in ("credit_note_pending", "pending"):
			with self.subTest(state=state):
				doc = bill(is_return=1)
				flat = text(render(COPY, doc, {doc.name: {"state": state, "application_ref": None, "sandbox": 0}})[0])
				self.assertIn("CREDIT NOTE PENDING URA APPROVAL", flat)
				self.assertNotIn("FISCAL RECEIPT PENDING", flat)

	def test_copy_fiscal_values_are_escaped(self):
		doc = bill(tax_id=XSS, is_return=1, efris_e_invoice="EINV-1")
		einv = SimpleNamespace(taxes=[SimpleNamespace(tax_category_code=f"01:A: {XSS}", net_amount=847.46, tax_amount=152.54)])
		html = render(
			COPY,
			doc,
			{doc.name: fiscalised(fdn=XSS, verification_code=XSS, device_no=XSS, issued=XSS, is_return=1, original_fdn=XSS)},
			{"EINV-1": einv},
		)[0]
		self.assertNotIn(XSS, html)
		self.assertGreaterEqual(html.count("&lt;img src=x onerror=alert(1)&gt;"), 7)

	def test_a_non_fiscal_tenant_copy_prints_no_efris_lines(self):
		flat = text(render(COPY, bill())[0])
		self.assertIn("COPY", flat)
		for needle in ("EFRIS", "FDN", "FISCAL"):
			self.assertNotIn(needle, flat)

	def test_the_standard_format_is_re_synced_on_migrate(self):
		self.assertGreater(spec(COPY)["modified"], "2026-11-23 23:00:00.000000")
		self.assertNotIn("max(copy_number)", spec(COPY)["html"])

	def test_bill_copy_formats_ugx_amounts_with_currency_formatter_not_raw_floats(self):
		"""R4 P2: URY Bill Copy must use currency formatting (fmt_money), never raw Python floats on UGX."""
		def ugx_fmt(value, currency=None, **kwargs):
			num = f"{round(float(value or 0)):,}"
			return f"{currency} {num}" if currency else num

		partner = bill(
			name="POS-INV-0002",
			currency="UGX",
			items=[Doc(item_code="WATER", item_name="Mineral Water", qty=2.0, amount=10000.0)],
			taxes=[Doc(description="Exempt", tax_amount=0.0)],
			payments=[Doc(mode_of_payment="Cash", amount=10000.0, account="Cash - DR")],
			grand_total=10000.0,
			rounded_total=10000.0,
			outstanding_amount=0.0,
		)
		doc = bill(
			name="POS-INV-0001",
			currency="UGX",
			customer="Corporate Buyer",
			items=[
				Doc(item_code="ROLEX", item_name="Signature Rolex", qty=2.0, amount=30000.0),
				Doc(item_code="SODA", item_name="Soda", qty=1.0, amount=5000.0),
			],
			taxes=[Doc(description="VAT 18%", tax_amount=5338.98)],
			payments=[Doc(mode_of_payment="Cash", amount=23000.0, account="Cash - DR")],
			grand_total=35000.0,
			rounded_total=35000.0,
			outstanding_amount=12000.0,
			custom_merged_pos_invoice="POS-INV-0002",
			ury_print_partner=partner,
		)
		flat = text(render(COPY, doc, fmt_money=ugx_fmt)[0])
		for expected in ("UGX 30,000", "UGX 5,000", "UGX 5,339", "UGX 35,000", "UGX 23,000",
		                 "UGX 12,000", "UGX 10,000", "UGX 45,000", "UGX 0"):
			self.assertIn(expected, flat, f"Expected formatted currency {expected!r} in: {flat}")
		for raw_float in ("30000.0", "5000.0", "5338.98", "35000.0", "23000.0", "12000.0", "10000.0", "45000.0", "0.0"):
			self.assertNotIn(raw_float, flat, f"Raw float {raw_float!r} leaked into URY Bill Copy: {flat}")
		self.assertGreater(spec(COPY)["modified"], "2026-11-24 00:30:00.000000")


if __name__ == "__main__":
	unittest.main()
