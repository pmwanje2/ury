"""Site-less contract: URY print formats print record values as text, never as markup.

Frappe renders print formats in a Jinja SandboxedEnvironment with DebugUndefined and
autoescape off (frappe/utils/jinja.py, version-16), so a value is escaped only where
the template says ``| e``. A Data field such as a customer name, mobile number, order
number, guest label, item name or modifier comment is free text, and Frappe's
save-time sanitiser keeps allowlisted tags such as <b> and <img>
(frappe/model/document.py ``_sanitize_content``), so a raw value reaches the printed
bill or slip as markup.

Each shipped format is rendered whole here, every text value carrying markup. Only
numbers, dates and times Frappe formats (``get_formatted``, ``fmt_money``,
``formatdate``, ``format_time``) and values the server sets (copy number, timestamp)
are left unmarked; raw ``item.description`` HTML is never dumped onto guest checks.

The record fixtures hold exactly the fields each template reads, so a template that
starts printing another field fails here until that field is added and marked.

CI installs the Jinja2 and MarkupSafe that Frappe v16 pins
(print-jinja-requirements.txt beside this file).
"""

import json
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from jinja2 import DebugUndefined
from jinja2.sandbox import SandboxedEnvironment

HERE = Path(__file__).resolve().parent
WAITER = "waiter@grandhotel.example"
CASHIER = "cashier@grandhotel.example"
DESCRIPTION = "<div><p>Hand-made <em>rice</em> noodles</p></div>"


def markup(field):
	return f'<b>[{field}]</b> "x" & Co'


def plain(field):
	return f'[{field}] x and Co'


class Doc:
	"""A record with exactly the given fields; reading any other field fails the test."""

	def __init__(self, **fields):
		self.__dict__.update(fields)

	def __getattr__(self, name):
		if name.startswith("_"):
			raise AttributeError(name)
		raise AssertionError(f"the template reads {name!r}, which this fixture does not set: add it")

	def get(self, fieldname, default=None):
		return getattr(self, fieldname)

	def get_formatted(self, fieldname, *args, **kwargs):
		val = getattr(self, fieldname)
		return "" if val is None else str(val)


def merged_bill(text):
	partner = Doc(
		name=text("partner.name"), docstatus=1, is_return=0, company="Demo Restaurant",
		room_number=None, kamra_ref="FOL-1",
		customer=text("partner.customer"), currency=text("partner.currency"),
		taxes=[
			Doc(description=text("partner.tax_description"), account_head="VAT", rate=18, tax_amount=12, included_in_print_rate=0),
		],
		payments=[Doc(mode_of_payment=text("partner.mode_of_payment"), amount=20, account=None)],
		paid_amount=20, change_amount=0, outstanding_amount=60,
		tax_id=text("partner.tax_id"), efris_e_invoice=None,
	)
	return Doc(
		name=text("doc.name"), status=text("doc.status"), custom_guest_label=text("doc.custom_guest_label"),
		docstatus=1, is_return=0, company="Demo Restaurant", company_address="ADDR-1", pos_profile="POS-1",
		room_number=text("doc.room_number"), kamra_ref="FOL-1",
		customer=text("doc.customer"), customer_name=text("doc.customer_name"),
		mobile_number=text("doc.mobile_number"), order_type=text("doc.order_type"), waiter=WAITER,
		restaurant=text("doc.restaurant"), branch=text("doc.branch"), no_of_pax=text("doc.no_of_pax"),
		restaurant_table=text("doc.restaurant_table"), custom_merged_tables=text("doc.custom_merged_tables"),
		custom_ury_order_number=text("doc.custom_ury_order_number"),
		cashier=CASHIER, currency=text("doc.currency"), tax_id=text("doc.tax_id"),
		posting_date="2026-11-24", posting_time="13:15:00",
		items=[Doc(
			item_code=text("item.item_code"), item_name=text("item.item_name"), description=DESCRIPTION,
			comment=text("item.comment"), qty=2, rate=50, amount=100,
		)],
		taxes=[
			Doc(description=text("service.description"), account_head="Service Charge - DR", rate=10, tax_amount=10, included_in_print_rate=0),
			Doc(description=text("tax.description"), account_head="Output VAT 18% - DR", rate=18, tax_amount=18, included_in_print_rate=0),
		],
		total_qty=2, total=100, net_total=100, discount_amount=5, additional_discount_percentage=5,
		rounding_adjustment=0, grand_total=123, rounded_total=123,
		custom_merged_pos_invoice=text("doc.custom_merged_pos_invoice"), custom_merged_total=80,
		custom_merged_pos_invoice_details=[Doc(item_name=text("merged.item_name"), comment=text("merged.comment"), qty=1, rate=80, amount=80)],
		payments=[
			Doc(mode_of_payment=text("doc.mode_of_payment"), amount=20, account=None),
			Doc(mode_of_payment="Room Charge", amount=20, account=None),
		],
		paid_amount=40, change_amount=0, outstanding_amount=60,
		efris_e_invoice="EINV-1", terms=None, ury_print_partner=partner,
	)


def bill_copy(text):
	partner = Doc(
		name=text("partner.name"), docstatus=1, is_return=0, currency=text("partner.currency"), customer=text("partner.customer"),
		items=[Doc(item_name=text("partner.item_name"), comment=text("partner.item_comment"), qty=1, amount=50)],
		taxes=[Doc(description=text("partner.tax_description"), account_head="VAT", rate=18, tax_amount=9, included_in_print_rate=0)],
		discount_amount=0, grand_total=59, rounded_total=59, outstanding_amount=0, change_amount=0,
		payments=[Doc(mode_of_payment=text("partner.mode_of_payment"), amount=59)],
		tax_id=text("partner.tax_id"), efris_e_invoice=None,
	)
	return Doc(
		name=text("doc.name"), docstatus=1, is_return=0, company="Demo Restaurant", company_address="ADDR-1", pos_profile="POS-1",
		restaurant=text("doc.restaurant"), branch=text("doc.branch"),
		copy_number=2, copy_timestamp="2026-11-24 20:15:00", posting_date="2026-11-24", posting_time="13:15:00",
		order_type=text("doc.order_type"), restaurant_table=text("doc.restaurant_table"),
		currency=text("doc.currency"), customer=text("doc.customer"), customer_name=text("doc.customer_name"),
		items=[Doc(item_name=text("item.item_name"), comment=text("item.comment"), qty=2, amount=100)],
		taxes=[Doc(description=text("tax.description"), account_head="VAT", rate=18, tax_amount=18, included_in_print_rate=0)],
		discount_amount=0, grand_total=118, rounded_total=118, outstanding_amount=60, change_amount=0,
		payments=[Doc(mode_of_payment=text("payment.mode_of_payment"), amount=58)],
		tax_id=text("doc.tax_id"), efris_e_invoice="EINV-1", ury_print_partner=partner,
	)


def waiter_slip(text):
	# build_combined_kot_doc: a URY KOT carrying the invoice's waiter and enriched item rows.
	return Doc(
		invoice=text("kot.invoice"), restaurant_table=text("kot.restaurant_table"),
		custom_guest_label=text("kot.custom_guest_label"), customer_name=text("kot.customer_name"),
		order_no=text("kot.order_no"), waiter=WAITER, date="2026-11-24", time="20:15:00",
		kot_items=[
			Doc(display_mode="old_new", cancelled_qty=1, old_qty=3, new_qty=2, quantity=3,
				item=text("changed.item"), item_name=text("changed.item_name"), comments=text("changed.comments")),
			# No item name: the slip falls back to the item code.
			Doc(display_mode="single_qty", cancelled_qty=0, old_qty=0, new_qty=1, quantity=1,
				item=text("added.item"), item_name=None, comments=text("added.comments")),
		],
	)


# Each format, its record, and the text values it prints (tax descriptions print tag-stripped).
FORMATS = {
	"merged_pos_invoice_format": (merged_bill, (
		"company.name", "address.line1", "address.line2", "address.city", "company.phone", "company.tax_id",
		"doc.name", "doc.room_number", "folio.guest_name", "doc.custom_guest_label", "doc.customer_name",
		"doc.mobile_number", "doc.order_type", "doc.custom_merged_tables", "doc.custom_ury_order_number",
		"waiter.full_name", "cashier.full_name", "doc.restaurant", "doc.branch", "doc.no_of_pax",
		"item.item_name", "item.comment", "service.description", "tax.description",
		"doc.custom_merged_pos_invoice", "merged.item_name", "merged.comment",
		"doc.mode_of_payment", "doc.customer", "doc.currency", "doc.tax_id",
		"partner.mode_of_payment", "partner.customer", "partner.currency",
		"efris.fdn", "efris.verification_code", "efris.device_no", "efris.issued",
		"efris.original_fdn", "efris.tax_category", "partner.application_ref",
	)),
	"ury_bill_copy": (bill_copy, (
		"company.name", "address.line1", "address.city", "company.phone", "company.tax_id",
		"doc.restaurant", "doc.branch", "doc.name", "doc.customer_name", "doc.currency",
		"item.item_name", "item.comment", "tax.description", "payment.mode_of_payment", "doc.customer", "doc.tax_id",
		"partner.name", "partner.item_name", "partner.item_comment", "partner.tax_description", "partner.mode_of_payment",
		"efris.fdn", "efris.verification_code", "efris.device_no", "efris.issued",
		"efris.original_fdn", "efris.tax_category", "partner.application_ref",
	)),
	"ury_waiter_order_slip": (waiter_slip, (
		"kot.invoice", "kot.restaurant_table", "kot.custom_guest_label", "kot.customer_name", "kot.order_no",
		"waiter.full_name", "changed.item_name", "added.item",
	)),
}
TAG_STRIPPED = {"service.description", "tax.description", "partner.tax_description"}


def spec(name):
	return json.loads((HERE / name / f"{name}.json").read_text())


def render(name, doc, text, *, with_efris=True):
	def get_value(doctype, filters_or_name, fieldname=None, as_dict=False, order_by=None):
		if doctype == "Company":
			if fieldname == "custom_guest_ledger_clearing":
				return None
			if as_dict:
				return {
					"company_name": text("company.name"),
					"tax_id": text("company.tax_id"),
					"phone_no": text("company.phone"),
					"company_logo": text("company.logo"),
				}
		if doctype == "URY Restaurant" and as_dict:
			return {
				"name": text("doc.restaurant"),
				"address": "ADDR-1",
				"tax_id": None,
				"image": None,
			}
		if doctype == "Address" and as_dict:
			return {
				"address_line1": text("address.line1"),
				"address_line2": text("address.line2"),
				"city": text("address.city"),
				"state": "",
				"country": "",
				"phone": None,
			}
		if doctype == "Folio":
			return SimpleNamespace(guest_name=text("folio.guest_name"), reservation=None)
		if doctype == "URY Bill Reprint Log":
			return 2
		return None

	def get_doc(doctype, docname):
		assert doctype == "E Invoice", doctype
		return SimpleNamespace(
			taxes=[
				SimpleNamespace(
					tax_category_code=f"01:{text('efris.tax_category')}",
					net_amount=85,
					tax_amount=15,
				)
			]
		)

	def get_fullname(user=None):
		if user == WAITER:
			return text("waiter.full_name")
		if user == CASHIER:
			return text("cashier.full_name")
		return user

	env = SandboxedEnvironment(undefined=DebugUndefined, autoescape=False)
	env.globals.update(
		_=lambda message: message,
		frappe=SimpleNamespace(
			get_fullname=get_fullname,
			get_doc=get_doc,
			get_meta=lambda dt: SimpleNamespace(has_field=lambda f: False),
			db=SimpleNamespace(get_value=get_value),
			utils=SimpleNamespace(
				formatdate=str,
				format_date=lambda d, fmt=None: str(d or ""),
				format_time=lambda t, fmt=None: str(t or "")[:5],
				get_url=lambda p="": str(p or ""),
				fmt_money=lambda value, **kwargs: str(value),
				now=lambda: "2026-11-24 20:15:00",
			),
		),
	)
	if with_efris:
		def efris_receipt_data(target):
			if target is doc:
				return {
					"state": "fiscalised",
					"fdn": text("efris.fdn"),
					"verification_code": text("efris.verification_code"),
					"device_no": text("efris.device_no"),
					"issued": text("efris.issued"),
					"is_return": 1,
					"original_fdn": text("efris.original_fdn"),
					"qr": text("efris.qr"),
					"sandbox": 0,
				}
			return {
				"state": "credit_note_pending",
				"application_ref": text("partner.application_ref"),
				"sandbox": 0,
			}
		env.globals["efris_receipt_data"] = efris_receipt_data
	return env.from_string(spec(name)["html"]).render(doc=doc)


class Printed(HTMLParser):
	"""The tags a printer lays out and the text it shows, character references decoded."""

	def __init__(self, html):
		super().__init__(convert_charrefs=True)
		self.tags, self.chunks = [], []
		self.feed(html)
		self.close()
		self.text = "".join(self.chunks)

	def handle_starttag(self, tag, attrs):
		# Normalize attribute values so only tag structure & attribute names are compared
		# (an unescaped quote in an attribute value injects a bogus attribute name!).
		self.tags.append((tag, sorted(k for k, _v in attrs)))

	def handle_endtag(self, tag):
		self.tags.append(("/" + tag, []))

	def handle_data(self, data):
		self.chunks.append(data)


class TestRecordValuesPrintAsText(unittest.TestCase):
	def test_markup_in_a_record_value_adds_no_tags_or_attributes_to_the_print(self):
		for name, (build, _printed) in FORMATS.items():
			with self.subTest(print_format=name):
				with_markup = Printed(render(name, build(markup), markup)).tags
				without = Printed(render(name, build(plain), plain)).tags
				self.assertEqual(with_markup, without)

	def test_each_text_value_prints_as_typed(self):
		for name, (build, printed) in FORMATS.items():
			shown = Printed(render(name, build(markup), markup)).text.casefold()
			for field in printed:
				with self.subTest(print_format=name, field=field):
					typed = f'[{field}] "x" & Co' if field in TAG_STRIPPED else markup(field)
					self.assertIn(typed.casefold(), shown)

	def test_item_modifier_comment_prints_escaped_and_description_html_is_not_dumped(self):
		html = render("merged_pos_invoice_format", merged_bill(markup), markup)
		self.assertNotIn(DESCRIPTION, html)
		self.assertNotIn(("em", []), Printed(html).tags)
		self.assertIn(markup("item.comment").casefold(), Printed(html).text.casefold())

	def test_a_tax_description_prints_tag_stripped_and_escaped(self):
		# Guard: `striptags` decodes entities, so "&lt;b&gt;" would come out as a real <b>
		# without the `| e` that follows it on tax/service descriptions.
		for fmt, build in (("ury_bill_copy", bill_copy), ("merged_pos_invoice_format", merged_bill)):
			with self.subTest(print_format=fmt):
				doc = build(plain)
				doc.taxes[-1].description = "<i>VAT</i> &lt;b&gt;18%&lt;/b&gt; & levy"
				printed = Printed(render(fmt, doc, plain))
				self.assertEqual(printed.tags, Printed(render(fmt, build(plain), plain)).tags)
				self.assertIn("VAT <b>18%</b> & levy", printed.text)

	def test_empty_optional_fields_print_nothing_across_all_formats(self):
		# Guard: optional blank/None fields must print nothing, never "None" (#141 P2).
		blank_merged = {
			"doc.customer_name", "doc.mobile_number", "doc.order_type", "doc.restaurant", "doc.branch",
			"doc.no_of_pax", "doc.restaurant_table", "doc.custom_merged_tables", "doc.custom_ury_order_number",
			"doc.custom_guest_label", "doc.room_number", "doc.tax_id", "item.comment", "merged.comment",
			"company.tax_id", "company.phone", "company.logo", "address.line1", "address.line2", "address.city",
		}
		m_doc = merged_bill(lambda field: None if field in blank_merged else plain(field))
		m_doc.waiter = None
		m_doc.cashier = None
		m_doc.posting_time = None
		m_doc.kamra_ref = None
		m_doc.ury_print_partner = None
		m_doc.custom_merged_pos_invoice = None
		self.assertNotIn("None", Printed(render("merged_pos_invoice_format", m_doc, lambda f: None if f in blank_merged else plain(f), with_efris=False)).text)

		blank_copy = {
			"doc.restaurant", "doc.branch", "doc.order_type", "doc.restaurant_table", "doc.customer_name",
			"doc.tax_id", "item.comment", "company.tax_id", "company.phone", "company.logo", "address.line1", "address.line2", "address.city",
		}
		c_doc = bill_copy(lambda field: None if field in blank_copy else plain(field))
		c_doc.posting_date = None
		c_doc.posting_time = None
		c_doc.ury_print_partner = None
		self.assertNotIn("None", Printed(render("ury_bill_copy", c_doc, lambda f: None if f in blank_copy else plain(f), with_efris=False)).text)

		w_doc = waiter_slip(lambda field: None if field in ("kot.invoice", "kot.customer_name", "kot.restaurant_table", "kot.custom_guest_label", "kot.order_no") else plain(field))
		w_doc.waiter = None
		w_doc.date = None
		w_doc.time = None
		w_text = Printed(render("ury_waiter_order_slip", w_doc, plain, with_efris=False)).text
		self.assertNotIn("None", w_text)


class TestEscapedTemplatesReachExistingSites(unittest.TestCase):
	def test_each_format_moves_its_modified_past_the_prior_version(self):
		# migrate reloads a standard format only when its file's modified is newer than the site's row.
		for name, previous in (
			("merged_pos_invoice_format", "2026-11-23 23:00:00.000000"),
			("ury_bill_copy", "2026-11-23 23:00:00.000000"),
			("ury_waiter_order_slip", "2026-11-23 21:00:00.000000"),
		):
			with self.subTest(print_format=name):
				self.assertGreater(spec(name)["modified"], previous)

	def test_the_waiter_slip_patch_line_has_not_run_on_any_site(self):
		# The slip is not a standard format: sites that hold it get the new template from a
		# re-suffixed patch line, which no site has run yet.
		self.assertEqual(spec("ury_waiter_order_slip")["standard"], "No")
		lines = [line.strip() for line in (HERE.parents[1] / "patches.txt").read_text().splitlines()]
		rerun = [line for line in lines if line.startswith("ury.patches.v2_0.fix_waiter_order_slip_print_format #")]
		self.assertEqual(len(rerun), 1)
		suffix = rerun[0].split("#", 1)[1].strip()
		self.assertNotIn(suffix, (
			"2026-11-15 guest label", "2026-11-21 v16 guest label",
			"2026-11-23 waiter full name", "2026-11-23 escaped waiter name",
			"2026-11-23 escaped record values",
		))


if __name__ == "__main__":
	unittest.main()
