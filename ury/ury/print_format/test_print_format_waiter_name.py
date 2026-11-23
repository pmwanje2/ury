"""Site-less contract: printed checks name the waiter, not their login, as text.

Simphony prints the employee's check name on guest checks. URY's printed bill
and waiter slip rendered ``doc.waiter``, the User id (an email). Both templates
are rendered here whole, the way Frappe renders print formats: a Jinja
SandboxedEnvironment with DebugUndefined and autoescape off
(frappe/utils/jinja.py, version-16), so a value is escaped only where the
template says ``| e``. ``frappe.get_fullname`` stands in for the print safe
global of that name (frappe/utils/safe_exec.py, version-15 and version-16):
the User's full name, or the id when there is no User, and the session user
when it is given no user at all.

CI installs the Jinja2 and MarkupSafe that Frappe v16 pins
(print-jinja-requirements.txt beside this file).
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import DebugUndefined
from jinja2.sandbox import SandboxedEnvironment

HERE = Path(__file__).resolve().parent
FORMATS = ("merged_pos_invoice_format", "ury_waiter_order_slip")
SESSION_USER = "cashier@grandhotel.example"


class Doc(SimpleNamespace):
	def get_formatted(self, fieldname, *args, **kwargs):
		return str(getattr(self, fieldname, ""))


def template(name):
	return json.loads((HERE / name / f"{name}.json").read_text())["html"]


def render(name, waiter, users):
	def get_fullname(user=None):
		user = user or SESSION_USER
		return users.get(user, user)

	env = SandboxedEnvironment(undefined=DebugUndefined, autoescape=False)
	env.globals.update(
		_=lambda text: text,
		frappe=SimpleNamespace(
			get_fullname=get_fullname,
			utils=SimpleNamespace(formatdate=str, fmt_money=lambda value, **kwargs: str(value)),
		),
	)
	doc = Doc(
		name="POS-INV-0001", invoice="POS-INV-0001", status="Draft", posting_date="2026-11-23",
		customer="Walk In", customer_name="Walk In", currency="UGX", waiter=waiter,
		items=[], kot_items=[], payments=[], custom_merged_pos_invoice=None, ury_print_partner=None,
		outstanding_amount=0, total_qty=0, net_total=0, grand_total=0,
	)
	return env.from_string(template(name)).render(doc=doc)


class TestWaiterNameOnPrints(unittest.TestCase):
	def test_each_print_names_the_waiter_from_their_user_record(self):
		for name in FORMATS:
			with self.subTest(print_format=name):
				printed = render(name, "samuel.otieno@grandhotel.example", {"samuel.otieno@grandhotel.example": "Samuel Otieno"})
				self.assertIn("Samuel Otieno", printed)
				self.assertNotIn("samuel.otieno@grandhotel.example", printed)

	def test_a_waiter_without_a_user_record_prints_their_id(self):
		for name in FORMATS:
			with self.subTest(print_format=name):
				self.assertIn("staff04@grandhotel.example", render(name, "staff04@grandhotel.example", {}))

	def test_a_check_without_a_waiter_never_prints_the_session_user(self):
		for name in FORMATS:
			for waiter in (None, ""):
				with self.subTest(print_format=name, waiter=waiter):
					self.assertNotIn(SESSION_USER, render(name, waiter, {SESSION_USER: SESSION_USER}))

	def test_a_waiter_name_prints_as_text_never_as_markup(self):
		# A User's full name is free text: markup in it must not reach the printed bill.
		users = {"x@grandhotel.example": "<b>x</b> & Co"}
		for name in FORMATS:
			with self.subTest(print_format=name):
				printed = render(name, "x@grandhotel.example", users)
				self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; Co", printed)
				self.assertNotIn("<b>x</b>", printed)

	def test_the_new_templates_reach_sites_that_already_have_them(self):
		# The merged bill is a standard format: migrate reloads it only when its modified moves
		# forward, also past the unescaped waiter-name version (2026-11-23 00:00).
		modified = json.loads((HERE / "merged_pos_invoice_format" / "merged_pos_invoice_format.json").read_text())["modified"]
		self.assertGreater(modified, "2026-11-23 00:00:00.000000")
		# The waiter slip is not a standard format: only a re-suffixed patch line reloads it.
		lines = [line.strip() for line in (HERE.parents[1] / "patches.txt").read_text().splitlines()]
		rerun = [line for line in lines if line.startswith("ury.patches.v2_0.fix_waiter_order_slip_print_format #")]
		self.assertEqual(len(rerun), 1)
		suffix = rerun[0].split("#", 1)[1].strip()
		self.assertGreaterEqual(suffix[:10], "2026-11-23")
		self.assertNotEqual(suffix, "2026-11-23 waiter full name", "the unescaped version's patch line does not run again")


if __name__ == "__main__":
	unittest.main()
