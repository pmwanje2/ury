"""Site-less contract: URY print formats print record values as text, never as markup.

Frappe renders print formats in a Jinja SandboxedEnvironment with DebugUndefined and
autoescape off (frappe/utils/jinja.py, version-16), so a value is escaped only where
the template says ``| e``. A Data field such as a customer name, mobile number, order
number, guest label or item name is free text, and Frappe's save-time sanitiser keeps
allowlisted tags such as <b> and <img> (frappe/model/document.py ``_sanitize_content``),
so a raw value reaches the printed bill or slip as markup.

Each shipped format is rendered whole here, every text value carrying markup. Two kinds
of value are printed raw on purpose and are not marked:

- an item's ``description``: a Text Editor field, HTML by design (sanitised on save);
- numbers, dates and times: money, quantities and dates Frappe formats
  (``get_formatted``, ``fmt_money``, ``formatdate``), as its own standard print macro
  prints formatted Currency raw (frappe/templates/print_formats/standard_macros.html),
  and values the server sets (the bill copy's number, timestamp and posting date).

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
# An item description is Text Editor HTML and must still print as HTML.
DESCRIPTION = "<div><p>Hand-made <em>rice</em> noodles</p></div>"


def markup(field):
	return f"<b>[{field}]</b> & Co"


def plain(field):
	return f"[{field}] and Co"


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
		return str(getattr(self, fieldname))


def merged_bill(text):
	partner = Doc(
		customer=text("partner.customer"), currency=text("partner.currency"),
		payments=[Doc(mode_of_payment=text("partner.mode_of_payment"), amount=20)],
		paid_amount=20, change_amount=0, outstanding_amount=60,
	)
	return Doc(
		name=text("doc.name"), status=text("doc.status"), custom_guest_label=text("doc.custom_guest_label"),
		docstatus=1, is_return=0, room_number=text("doc.room_number"),
		customer=text("doc.customer"), customer_name=text("doc.customer_name"),
		mobile_number=text("doc.mobile_number"), order_type=text("doc.order_type"), waiter=WAITER,
		restaurant=text("doc.restaurant"), branch=text("doc.branch"), no_of_pax=text("doc.no_of_pax"),
		cashier=text("doc.cashier"), currency=text("doc.currency"),
		posting_date="2026-11-23", due_date="2026-11-30", update_billed_amount_in_delivery_note=0, invoice_created=1,
		items=[Doc(
			item_code=text("item.item_code"), item_name=text("item.item_name"), description=DESCRIPTION,
			qty=2, stock_uom=text("item.stock_uom"), uom=text("item.uom"),
			discount_amount=0, distributed_discount_amount=0, rate=50, amount=100,
		)],
		total_qty=2, net_total=100, grand_total=100,
		custom_merged_pos_invoice=text("doc.custom_merged_pos_invoice"), custom_merged_total=80,
		custom_merged_pos_invoice_details=[Doc(item_name=text("merged.item_name"), qty=1, rate=80, amount=80)],
		payments=[Doc(mode_of_payment=text("doc.mode_of_payment"), amount=40)],
		paid_amount=40, change_amount=0, outstanding_amount=60, ury_print_partner=partner,
	)


def bill_copy(text):
	return Doc(
		name=text("doc.name"), copy_number=2, copy_timestamp="2026-11-23 20:15:00", posting_date="2026-11-23",
		currency=text("doc.currency"), customer=text("doc.customer"),
		items=[Doc(item_name=text("item.item_name"), qty=2, amount=100)],
		taxes=[Doc(description=text("tax.description"), tax_amount=18)],
		grand_total=118, rounded_total=118, outstanding_amount=60,
		payments=[Doc(mode_of_payment=text("payment.mode_of_payment"), amount=58)],
	)


def waiter_slip(text):
	# build_combined_kot_doc: a URY KOT carrying the invoice's waiter and enriched item rows.
	return Doc(
		invoice=text("kot.invoice"), restaurant_table=text("kot.restaurant_table"),
		custom_guest_label=text("kot.custom_guest_label"), customer_name=text("kot.customer_name"),
		order_no=text("kot.order_no"), waiter=WAITER, date="2026-11-23", time="20:15:00",
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
		"doc.name", "doc.room_number", "doc.custom_guest_label", "doc.customer_name", "doc.mobile_number",
		"doc.order_type", "waiter.full_name", "doc.restaurant", "doc.branch", "doc.no_of_pax", "doc.cashier",
		"item.item_code", "item.item_name", "item.stock_uom", "item.uom", "doc.custom_merged_pos_invoice",
		"merged.item_name", "doc.mode_of_payment", "doc.customer", "doc.currency",
		"partner.mode_of_payment", "partner.customer", "partner.currency",
	)),
	"ury_bill_copy": (bill_copy, (
		"doc.name", "doc.currency", "item.item_name", "tax.description", "payment.mode_of_payment", "doc.customer",
	)),
	"ury_waiter_order_slip": (waiter_slip, (
		"kot.invoice", "kot.restaurant_table", "kot.custom_guest_label", "kot.customer_name", "kot.order_no",
		"waiter.full_name", "changed.item_name", "added.item",
	)),
}
TAG_STRIPPED = {"tax.description"}


def spec(name):
	return json.loads((HERE / name / f"{name}.json").read_text())


def render(name, doc, text):
	env = SandboxedEnvironment(undefined=DebugUndefined, autoescape=False)
	env.globals.update(
		_=lambda message: message,
		frappe=SimpleNamespace(
			get_fullname=lambda user=None: text("waiter.full_name") if user == WAITER else user,
			utils=SimpleNamespace(
				formatdate=str, fmt_money=lambda value, **kwargs: str(value), now=lambda: "2026-11-23 20:15:00",
			),
		),
	)
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
		self.tags.append((tag, attrs))

	def handle_endtag(self, tag):
		self.tags.append(("/" + tag, []))

	def handle_data(self, data):
		self.chunks.append(data)


class TestRecordValuesPrintAsText(unittest.TestCase):
	def test_markup_in_a_record_value_adds_no_tags_to_the_print(self):
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
					typed = f"[{field}] & Co" if field in TAG_STRIPPED else markup(field)
					self.assertIn(typed.casefold(), shown)

	def test_an_item_description_still_prints_as_html(self):
		# Guard: escaping the text fields must not escape the intentionally HTML one.
		html = render("merged_pos_invoice_format", merged_bill(markup), markup)
		self.assertIn(DESCRIPTION, html)
		self.assertIn(("em", []), Printed(html).tags)

	def test_a_tax_description_prints_tag_stripped_and_escaped(self):
		# Guard: `striptags` decodes entities, so "&lt;b&gt;" would come out as a real <b>
		# without the `| e` that follows it on the bill copy.
		doc = bill_copy(plain)
		doc.taxes[0].description = "<i>VAT</i> &lt;b&gt;18%&lt;/b&gt; & levy"
		printed = Printed(render("ury_bill_copy", doc, plain))
		self.assertEqual(printed.tags, Printed(render("ury_bill_copy", bill_copy(plain), plain)).tags)
		self.assertIn("VAT <b>18%</b> & levy", printed.text)

	def test_empty_optional_fields_print_nothing(self):
		# Guard: an escaped blank field prints nothing, not "None".
		blank = {"doc.customer_name", "doc.mobile_number", "doc.order_type", "doc.restaurant", "doc.branch",
			"doc.no_of_pax", "doc.cashier", "item.stock_uom", "item.uom"}
		doc = merged_bill(lambda field: None if field in blank else plain(field))
		doc.items[0].description = None
		self.assertNotIn("None", Printed(render("merged_pos_invoice_format", doc, plain)).text)


class TestEscapedTemplatesReachExistingSites(unittest.TestCase):
	def test_each_format_moves_its_modified_past_the_unescaped_version(self):
		# migrate reloads a standard format only when its file's modified is newer than the site's row.
		for name, unescaped in (
			("merged_pos_invoice_format", "2026-11-23 12:00:00.000000"),
			("ury_bill_copy", "2026-11-21 00:00:00.000000"),
			("ury_waiter_order_slip", "2026-11-23 00:00:00.000000"),
		):
			with self.subTest(print_format=name):
				self.assertGreater(spec(name)["modified"], unescaped)

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
		))


if __name__ == "__main__":
	unittest.main()
