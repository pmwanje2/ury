"""Site-less contract: printed checks name the waiter, not their login.

Simphony prints the employee's check name on guest checks. URY's printed bill
and waiter slip rendered ``doc.waiter``, the User id (an email). Each Jinja
expression that prints the waiter is evaluated here against a stand-in for
``frappe.get_fullname``, which the print Jinja environment exposes through
Frappe's safe globals (frappe/utils/safe_exec.py, version-15 and version-16)
and which returns the User's full name, or the id when there is no User.
"""

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
FORMATS = ("merged_pos_invoice_format", "ury_waiter_order_slip")
USERS = {"samuel.otieno@grandhotel.example": "Samuel Otieno"}
SESSION_USER = "cashier@grandhotel.example"


def get_fullname(user=None):
	"""Mirrors frappe.utils.get_fullname: no user means the session user."""
	user = user or SESSION_USER
	return USERS.get(user, user)


def template(name):
	return json.loads((HERE / name / f"{name}.json").read_text())["html"]


def waiter_expressions(html):
	"""Every {{ ... }} that prints the waiter, and whether an {% if doc.waiter %} guards it."""
	guarded = [match.span() for match in re.finditer(r"\{%-?\s*if\s+doc\.waiter\s*-?%\}.*?\{%-?\s*endif\s*-?%\}", html, re.S)]
	found = []
	for match in re.finditer(r"\{\{(.*?)\}\}", html, re.S):
		if "waiter" in match.group(1):
			inside = any(start <= match.start() and match.end() <= end for start, end in guarded)
			found.append((match.group(1).strip(), inside))
	return found


def render(expression, waiter):
	frappe = SimpleNamespace(get_fullname=get_fullname)
	return eval(expression, {"__builtins__": {}}, {"doc": SimpleNamespace(waiter=waiter), "frappe": frappe})


class TestWaiterNameOnPrints(unittest.TestCase):
	def test_each_print_names_the_waiter_from_their_user_record(self):
		for name in FORMATS:
			expressions = waiter_expressions(template(name))
			self.assertTrue(expressions, f"{name} no longer prints the waiter")
			for expression, _guarded in expressions:
				with self.subTest(print_format=name, expression=expression):
					self.assertEqual(render(expression, "samuel.otieno@grandhotel.example"), "Samuel Otieno")

	def test_a_waiter_without_a_user_record_prints_their_id(self):
		for name in FORMATS:
			for expression, _guarded in waiter_expressions(template(name)):
				with self.subTest(print_format=name, expression=expression):
					self.assertEqual(render(expression, "staff04@grandhotel.example"), "staff04@grandhotel.example")

	def test_a_check_without_a_waiter_never_prints_the_session_user(self):
		for name in FORMATS:
			for expression, guarded in waiter_expressions(template(name)):
				if guarded:
					continue
				with self.subTest(print_format=name, expression=expression):
					self.assertEqual(render(expression, None), "")

	def test_the_new_templates_reach_sites_that_already_have_them(self):
		# The merged bill is a standard format: migrate reloads it only when its modified moves forward.
		self.assertGreater(json.loads((HERE / "merged_pos_invoice_format" / "merged_pos_invoice_format.json").read_text())["modified"],
			"2026-11-21 00:00:00.000000")
		# The waiter slip is not a standard format: only a re-suffixed patch line reloads it.
		lines = [line.strip() for line in (HERE.parents[1] / "patches.txt").read_text().splitlines()]
		rerun = [line for line in lines if line.startswith("ury.patches.v2_0.fix_waiter_order_slip_print_format #")]
		self.assertEqual(len(rerun), 1)
		self.assertGreater(rerun[0].split("#", 1)[1].strip()[:10], "2026-11-21")


if __name__ == "__main__":
	unittest.main()
