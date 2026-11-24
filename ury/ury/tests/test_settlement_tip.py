"""The settlement boundary carries the cashier's tip onto the bill (isolated ERP runtime).

``make_invoice`` is the POS settle call. A tip entered at settlement (a cash
tip, or a charged tip on a card or room-charge tender) is stored on the bill
with the server it is owed to, and its Tips Payable line is in place before
the bill totals are computed, so the tenders are allocated against the bill
plus the tip. The line itself is built by the POS Invoice tender rules
(``ury/ury/hooks/test_ury_pos_tender.py`` covers it on the real lifecycle).
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from ury.ury.doctype.ury_order import ury_order as order


class TestSettlementTip(unittest.TestCase):
	def setUp(self):
		self.enterContext(patch.object(order, "_", lambda msg: msg))
		self.f = self.enterContext(patch.object(order, "frappe"))
		self.f.ValidationError = ValueError
		self.f.throw.side_effect = lambda msg, exc=ValueError, **kw: (_ for _ in ()).throw(exc(msg))
		self.f.utils.cint.side_effect = lambda value: int(value or 0)
		self.f.utils.flt.side_effect = frappe.utils.flt
		self.f.session.user = "cashier@example.com"
		self.f.db.get_value.return_value = 0
		self.f.get_value.return_value = "Dine In"
		self.f.get_meta.return_value.has_field.return_value = False
		self.f.get_cached_doc.return_value = frappe._dict(name="Till", company="Hotel", currency="UGX")
		self.events = []
		self.invoice = self._bill()
		for name, value in {
			"_validate_client_ids": None,
			"_lock_settlement_customer": 0,
			"_client_order_invoice": None,
			"_check_held_before_settlement": None,
			"_refuse_client_order_rebind": None,
			"_set_settlement_customer": None,
			"_enforce_on_account": False,
			"get_order_invoice": self.invoice,
		}.items():
			self.enterContext(patch.object(order, name, return_value=value))
		self.sync = self.enterContext(
			patch.object(
				order,
				"sync_charged_tip",
				create=True,
				side_effect=lambda doc, *args, **kwargs: self.events.append(("tip", doc.custom_charged_tip)),
			)
		)

	def _bill(self):
		bill = MagicMock()
		bill.name = "B1"
		bill.waiter = "waiter@example.com"
		bill.custom_merged_pos_invoice = None
		bill.custom_charged_tip = 0
		bill.custom_tip_server = None
		bill.docstatus = 0
		bill.pos_profile = "Till"
		bill.restaurant_table = None
		bill.payments = []
		bill.set.side_effect = lambda key, value: setattr(bill, key, value)
		bill.append.side_effect = lambda key, row: getattr(bill, key).append(frappe._dict(row))
		bill.calculate_taxes_and_totals.side_effect = lambda: self.events.append("totals")
		bill.save.side_effect = lambda **kw: self.events.append("save")
		return bill

	def settle(self, **kwargs):
		return order.make_invoice(
			"Walk-in",
			[dict(mode_of_payment="Cash", amount=120)],
			"cashier@example.com",
			"Till",
			"captain@example.com",
			invoice="B1",
			submit_invoice=0,
			**kwargs,
		)

	def test_tip_is_on_the_bill_before_its_totals(self):
		self.settle(tip=20)

		self.assertEqual(self.invoice.custom_charged_tip, 20)
		self.assertEqual(self.invoice.custom_tip_server, "waiter@example.com")
		self.assertEqual(self.events[:2], [("tip", 20), "totals"])

	def test_tip_server_can_be_named_at_settlement(self):
		self.settle(tip="15", tip_server="runner@example.com")

		self.assertEqual(self.invoice.custom_charged_tip, 15)
		self.assertEqual(self.invoice.custom_tip_server, "runner@example.com")

	def test_settling_without_a_tip_clears_an_earlier_tip(self):
		self.invoice.custom_charged_tip = 15
		self.settle()

		self.assertEqual(self.invoice.custom_charged_tip, 0)
		self.assertEqual(self.events[:2], [("tip", 0), "totals"])

	def test_negative_tip_is_refused_before_any_write(self):
		with self.assertRaisesRegex(ValueError, r"\[POS-TIP-NEGATIVE\]"):
			self.settle(tip=-5)
		self.assertNotIn("save", self.events)

	def test_merged_invoice_without_explicit_tip_keeps_overtender_on_primary_bill(self):
		self.invoice.grand_total = 100
		self.invoice.rounded_total = 100
		self.invoice.custom_merged_pos_invoice = "B2"

		target = self._bill()
		target.name = "B2"
		target.grand_total = 50
		target.rounded_total = 50
		self.f.get_doc.return_value = target

		order.make_invoice(
			"Walk-in",
			[dict(mode_of_payment="Credit Card", amount=197)],
			"cashier@example.com",
			"Till",
			"captain@example.com",
			invoice="B1",
			submit_invoice=0,
		)

		self.assertEqual([(r.mode_of_payment, r.amount) for r in target.payments], [("Credit Card", 50.0)])
		self.assertEqual([(r.mode_of_payment, r.amount) for r in self.invoice.payments], [("Credit Card", 147.0)])
