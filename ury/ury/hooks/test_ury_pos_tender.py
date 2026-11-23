"""Non-cash over-tender and charged tips on the real v16 POS Invoice (database-backed).

Oracle Simphony settles a check tender by tender: only a cash tender gives
change; a card (or room-charge) tender that is over-tendered is refused, or,
when the Tender/Media record is set to post the excess as a charged tip, the
excess becomes a charged tip. Charged and cash tips are owed to the server:
they post to a Tips Payable liability, never to sales revenue, never into the
VAT base and never out of the drawer as change.

The rule lives on the native Mode of Payment (``custom_overtender_rule``,
default ``Refuse``); the Tips Payable account on the POS Profile, next to the
native ``account_for_change_amount``. A tip is one native Sales Taxes and
Charges row (``Actual`` on the Tips Payable account), so consolidation and the
GL post it like any other charge.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.tests import till_money_fixtures as till_fx


def _tip_rows(invoice):
	return [row for row in invoice.taxes if row.account_head == till_fx.TIPS_ACCOUNT]


class TestNonCashOvertender(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		till_fx.ensure_masters()
		cls.till = till_fx.make_till("_Test Till Overtender")

	def tearDown(self):
		frappe.set_user("Administrator")
		till_fx.set_overtender_rule(till_fx.CARD, "Refuse")
		till_fx.set_overtender_rule(till_fx.WIRE, "Refuse")

	def test_overtender_rule_defaults_to_refuse(self):
		field = frappe.get_meta("Mode of Payment").get_field("custom_overtender_rule")
		self.assertIsNotNone(field, "Mode of Payment needs a per-tender over-tender rule")
		self.assertEqual(field.default, "Refuse")
		self.assertEqual(field.options.split("\n"), ["Refuse", "Post to Charged Tip"])

	def test_card_overtender_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-TENDER-OVERTENDER\]"):
			till_fx.bill(self.till, 100, [(till_fx.CARD, 120)])

	def test_cash_overtender_still_gives_change(self):
		invoice = till_fx.bill(self.till, 100, [(till_fx.CASH, 120)])
		self.assertEqual(invoice.change_amount, 20)
		self.assertEqual(_tip_rows(invoice), [])

	def test_change_is_given_only_from_cash_received(self):
		invoice = till_fx.bill(self.till, 120, [(till_fx.CASH, 20), (till_fx.CARD, 110)])
		self.assertEqual(invoice.change_amount, 10)

		# 135 tendered on a 120 bill with only 5 in cash: 10 of the excess is card.
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-TENDER-OVERTENDER\]"):
			till_fx.bill(self.till, 120, [(till_fx.CASH, 5), (till_fx.CARD, 130)])

	def test_card_overtender_posts_to_charged_tip_when_the_tender_allows(self):
		till_fx.set_overtender_rule(till_fx.CARD, "Post to Charged Tip")
		invoice = till_fx.bill(self.till, 100, [(till_fx.CARD, 120)])

		self.assertEqual(invoice.change_amount, 0)
		self.assertEqual(invoice.net_total, 100)
		self.assertEqual(invoice.grand_total, 120)
		self.assertEqual(invoice.custom_charged_tip, 20)
		self.assertEqual(invoice.custom_tip_server, till_fx.WAITER)
		rows = _tip_rows(invoice)
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].charge_type, rows[0].tax_amount), ("Actual", 20))

	def test_mixed_non_cash_overtender_needs_every_non_cash_tender_to_allow_tips(self):
		till_fx.set_overtender_rule(till_fx.CARD, "Post to Charged Tip")
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-TENDER-OVERTENDER\]"):
			till_fx.bill(self.till, 100, [(till_fx.CARD, 70), (till_fx.WIRE, 50)])

		till_fx.set_overtender_rule(till_fx.WIRE, "Post to Charged Tip")
		invoice = till_fx.bill(self.till, 100, [(till_fx.CARD, 70), (till_fx.WIRE, 50)])
		self.assertEqual((invoice.custom_charged_tip, invoice.change_amount), (20, 0))


class TestChargedTip(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		till_fx.ensure_masters()
		cls.till = till_fx.make_till("_Test Till Tips")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_cash_tip_is_a_tips_payable_line_not_change(self):
		invoice = till_fx.bill(self.till, 100, [(till_fx.CASH, 120)], tip=20)

		self.assertEqual(invoice.change_amount, 0)
		self.assertEqual((invoice.net_total, invoice.grand_total), (100, 120))
		rows = _tip_rows(invoice)
		self.assertEqual([row.tax_amount for row in rows], [20])
		self.assertEqual(invoice.custom_tip_server, till_fx.WAITER)

	def test_tip_stays_outside_the_vat_base(self):
		invoice = till_fx.bill(self.till, 100, [(till_fx.CASH, 138)], tip=20, vat_rate=18)

		vat = [row for row in invoice.taxes if row.account_head == till_fx.VAT_ACCOUNT]
		self.assertEqual([row.tax_amount for row in vat], [18])
		self.assertEqual([row.tax_amount for row in _tip_rows(invoice)], [20])
		self.assertEqual((invoice.grand_total, invoice.change_amount), (138, 0))

	def test_tip_row_is_rebuilt_not_duplicated_on_resave(self):
		invoice = till_fx.bill(self.till, 100, [(till_fx.CASH, 120)], tip=20, submit=False)
		invoice.save(ignore_permissions=True)
		invoice.save(ignore_permissions=True)

		self.assertEqual([row.tax_amount for row in _tip_rows(invoice)], [20])
		self.assertEqual(invoice.grand_total, 120)

	def test_negative_tip_is_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-TIP-NEGATIVE\]"):
			till_fx.bill(self.till, 100, [(till_fx.CASH, 100)], tip=-5)

	def test_tip_needs_a_tips_payable_liability_account(self):
		bare = till_fx.make_till("_Test Till No Tips Account", custom_tips_payable_account=None)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-TIP-ACCOUNT\]"):
			till_fx.bill(bare, 100, [(till_fx.CASH, 120)], tip=20)

		revenue = till_fx.make_till("_Test Till Revenue Tips", custom_tips_payable_account=till_fx.INCOME_ACCOUNT)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-TIP-ACCOUNT\]"):
			till_fx.bill(revenue, 100, [(till_fx.CASH, 120)], tip=20)

	def test_tip_reaches_the_gl_as_tips_payable_on_consolidation(self):
		till = till_fx.make_till("_Test Till Tip GL")
		invoice = till_fx.bill(till, 100, [(till_fx.CASH, 120)], tip=20)
		closing = till_fx.close_till(till, counted=1120)

		self.assertEqual(closing.payment_reconciliation[0].expected_amount, 1120)
		consolidated = frappe.db.get_value("POS Invoice", invoice.name, "consolidated_invoice")
		self.assertTrue(consolidated, "the closing must consolidate the tipped invoice")
		gl = till_fx.gl_by_account("Sales Invoice", consolidated)
		self.assertEqual(gl.get(till_fx.TIPS_ACCOUNT), (0, 20))
		self.assertEqual(gl.get(till_fx.INCOME_ACCOUNT), (0, 100))
		self.assertEqual(gl.get(till_fx.CASH_ACCOUNT), (120, 0))
