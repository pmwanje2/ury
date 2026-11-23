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
		payments=[Doc(mode_of_payment="Cash", amount=1000)], taxes=[], custom_merged_pos_invoice=None,
		ury_print_partner=None, outstanding_amount=0, total_qty=1, net_total=847.46, grand_total=1000,
		rounded_total=1000, paid_amount=1000, change_amount=0, tax_id=None, efris_e_invoice=None,
		copy_number=1, copy_timestamp="2026-11-23 12:34",
	)
	values.update(fields)
	return Doc(**values)


def fiscalised(**extra):
	return {"state": "fiscalised", "fdn": FDN, "verification_code": CODE, "qr": QR, "sandbox": 0, "is_return": 0, **extra}


def render(name, doc, efris=None, e_invoices=None):
	"""Render one shipped format; efris maps invoice name -> efris_receipt_data() result (None: app absent)."""
	calls = []

	def get_doc(doctype, docname):
		assert doctype == "E Invoice", doctype
		return (e_invoices or {})[docname]

	env = SandboxedEnvironment(undefined=DebugUndefined, autoescape=False)
	env.globals.update(
		_=lambda text: text,
		frappe=SimpleNamespace(
			get_fullname=lambda user=None: user or "cashier@example.com",
			get_doc=get_doc,
			utils=SimpleNamespace(formatdate=str, now=lambda: "2026-11-23 12:00",
			                      fmt_money=lambda value, **kwargs: f"UGX {value}"),
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
		flat = text(render(MERGED, bill(room_number="101"))[0])
		self.assertIn("ROOM CHARGE", flat)
		self.assertIn("Room : 101", flat)
		self.assertIn("Guest signature", flat)
		self.assertNotIn("Guest signature", text(render(MERGED, bill())[0]))

	def test_fiscal_and_room_values_are_escaped(self):
		doc = bill(room_number=XSS, tax_id=XSS)
		html = render(MERGED, doc, {doc.name: fiscalised(fdn=XSS, verification_code=XSS)})[0]
		self.assertNotIn(XSS, html)
		self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

	def test_the_standard_format_is_re_synced_on_migrate(self):
		# Frappe re-imports a standard Print Format from its JSON only when `modified` advances
		# (past the escaped version this branch stacks on, fix/print-format-escape-user-values).
		self.assertGreater(spec(MERGED)["modified"], "2026-11-23 21:00:00.000000")


class BillCopyFiscalReceipt(unittest.TestCase):
	def test_a_copy_of_a_fiscalised_bill_repeats_its_fdn_code_and_qr(self):
		doc = bill(efris_e_invoice="EINV-1")
		html, _calls = render(COPY, doc, {doc.name: fiscalised()}, {"EINV-1": SimpleNamespace(taxes=TAXES)})
		flat = text(html)
		for needle in ("COPY", FDN, CODE, "A: Standard (18%)"):
			self.assertIn(needle, flat)
		self.assertIn(f'src="{QR}"', html)

	def test_a_copy_of_a_pending_bill_stays_pending(self):
		doc = bill()
		flat = text(render(COPY, doc, {doc.name: {"state": "pending", "sandbox": 0}})[0])
		self.assertIn("FISCAL RECEIPT PENDING", flat)
		self.assertNotIn(FDN, flat)

	def test_a_non_fiscal_tenant_copy_prints_no_efris_lines(self):
		flat = text(render(COPY, bill())[0])
		self.assertIn("COPY", flat)
		for needle in ("EFRIS", "FDN", "FISCAL"):
			self.assertNotIn(needle, flat)

	def test_the_standard_format_is_re_synced_on_migrate(self):
		self.assertGreater(spec(COPY)["modified"], "2026-11-23 21:00:00.000000")


if __name__ == "__main__":
	unittest.main()
