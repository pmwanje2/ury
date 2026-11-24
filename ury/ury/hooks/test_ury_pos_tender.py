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

	def test_grand_total_discount_never_discounts_the_tip(self):
		invoice = till_fx.bill(
			self.till,
			100,
			[(till_fx.CASH, 110)],
			tip=20,
			additional_discount_percentage=10,
			apply_discount_on="Grand Total",
		)

		self.assertEqual(invoice.additional_discount_percentage, 10)
		self.assertEqual(invoice.discount_amount, 10)
		self.assertEqual(invoice.net_total, 90)
		self.assertEqual(invoice.grand_total, 110)
		self.assertEqual(invoice.change_amount, 0)
		self.assertEqual([row.tax_amount for row in _tip_rows(invoice)], [20])

	def test_card_overtender_tip_with_grand_total_discount(self):
		till_fx.set_overtender_rule(till_fx.CARD, "Post to Charged Tip")
		try:
			invoice = till_fx.bill(
				self.till,
				100,
				[(till_fx.CARD, 110)],
				additional_discount_percentage=10,
				apply_discount_on="Grand Total",
			)
		finally:
			till_fx.set_overtender_rule(till_fx.CARD, "Refuse")

		self.assertEqual(invoice.discount_amount, 10)
		self.assertEqual(invoice.net_total, 90)
		self.assertEqual(invoice.custom_charged_tip, 20)
		self.assertEqual(invoice.grand_total, 110)
		self.assertEqual(invoice.change_amount, 0)
		self.assertEqual([row.tax_amount for row in _tip_rows(invoice)], [20])

	def test_discount_never_discounts_tip_across_bases_tax_modes_rounding_and_preview(self):
		from frappe.tests.utils import change_settings
		from frappe.utils import round_based_on_smallest_currency_fraction

		with change_settings("Global Defaults", {"disable_rounded_total": 1}):
			# Net Total basis (no tax)
			net_inv = till_fx.bill(
				self.till,
				100,
				[(till_fx.CASH, 110)],
				tip=20,
				additional_discount_percentage=10,
				apply_discount_on="Net Total",
				disable_rounded_total=1,
			)
			self.assertEqual((net_inv.discount_amount, net_inv.net_total, net_inv.grand_total, net_inv.change_amount), (10, 90, 110, 0))

			# Exclusive and inclusive VAT (18%) across both apply_discount_on bases
			for basis, expected_discount in (("Grand Total", 11.8), ("Net Total", 10.0)):
				for vat_inclusive, rate in ((False, 100), (True, 118)):
					with self.subTest(basis=basis, vat_inclusive=vat_inclusive):
						inv = till_fx.bill(
							self.till,
							rate,
							[(till_fx.CASH, 126.2)],
							tip=20,
							vat_rate=18,
							vat_inclusive=vat_inclusive,
							additional_discount_percentage=10,
							apply_discount_on=basis,
							disable_rounded_total=1,
						)
						vat = [row for row in inv.taxes if row.account_head == till_fx.VAT_ACCOUNT]
						self.assertAlmostEqual(inv.discount_amount, expected_discount, places=2)
						self.assertAlmostEqual(inv.net_total, 90.0, places=2)
						self.assertAlmostEqual(vat[0].tax_amount_after_discount_amount, 16.2, places=2)
						self.assertEqual([row.tax_amount for row in _tip_rows(inv)], [20])
						self.assertAlmostEqual(inv.grand_total, 126.2, places=2)
						self.assertAlmostEqual(inv.change_amount, 0.0, places=2)

			# Flat discount_amount works cleanly and cannot eat into the tip
			flat_inv = till_fx.bill(
				self.till,
				100,
				[(till_fx.CASH, 110)],
				tip=20,
				discount_amount=10,
				apply_discount_on="Grand Total",
				disable_rounded_total=1,
			)
			self.assertEqual((flat_inv.discount_amount, flat_inv.net_total, flat_inv.grand_total, flat_inv.change_amount), (10, 90, 110, 0))
			with self.assertRaises(frappe.ValidationError):
				till_fx.bill(
					self.till,
					100,
					[(till_fx.CASH, 10)],
					tip=20,
					discount_amount=110,
					apply_discount_on="Grand Total",
					disable_rounded_total=1,
				)

			# Unsaved preview (same calculate_taxes_and_totals seam used by preview_settlement_totals)
			draft = till_fx.bill(self.till, 100, [(till_fx.CASH, 120)], tip=20, submit=False, disable_rounded_total=1)
			preview = frappe.get_doc("POS Invoice", draft.name)
			preview.apply_discount_on = "Grand Total"
			preview.additional_discount_percentage = 10
			preview.discount_amount = 0
			preview.calculate_taxes_and_totals()
			self.assertEqual(preview.discount_amount, 10)
			self.assertEqual(preview.net_total, 90)
			self.assertEqual(preview.grand_total, 110)

		# Rounding enabled (disable_rounded_total=0) with Grand Total discount + VAT + tip
		with change_settings("Global Defaults", {"disable_rounded_total": 0}):
			expected_rounded = round_based_on_smallest_currency_fraction(126.2, self.till.profile.currency, 2)
			rounded_inv = till_fx.bill(
				self.till,
				100,
				[(till_fx.CASH, expected_rounded)],
				tip=20,
				vat_rate=18,
				additional_discount_percentage=10,
				apply_discount_on="Grand Total",
				disable_rounded_total=0,
			)
			self.assertAlmostEqual(rounded_inv.discount_amount, 11.8, places=2)
			self.assertAlmostEqual(rounded_inv.grand_total, 126.2, places=2)
			self.assertAlmostEqual(rounded_inv.rounded_total, expected_rounded, places=2)
			self.assertAlmostEqual(rounded_inv.change_amount, 0.0, places=2)

	def test_sales_return_keeps_tip_reversal_visible_and_preserves_discount(self):
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

		original = till_fx.bill(
			self.till,
			100,
			[(till_fx.CASH, 110)],
			tip=20,
			additional_discount_percentage=10,
			apply_discount_on="Grand Total",
			disable_rounded_total=1,
		)
		self.assertEqual((original.grand_total, original.custom_charged_tip, original.custom_tip_server), (110, 20, till_fx.WAITER))

		frappe.set_user(self.till.holder)
		try:
			ret = make_sales_return(original.name)
			ret.insert(ignore_permissions=True)
			ret.submit()
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(ret.custom_charged_tip, -20)
		self.assertEqual(ret.custom_tip_server, till_fx.WAITER)
		self.assertEqual(ret.discount_amount, -10)
		self.assertEqual(ret.grand_total, -110)
		self.assertEqual([row.tax_amount for row in _tip_rows(ret)], [-20])

	def test_partial_return_zeroing_tip_removes_copied_tips_payable_row_without_resurrection(self):
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

		frappe.set_user(self.till.holder)
		try:
			original = frappe.new_doc("POS Invoice")
			original.pos_profile = self.till.name
			original.company = till_fx.COMPANY
			original.customer = till_fx.CUSTOMER
			original.is_pos = 1
			original.update_stock = 0
			original.waiter = till_fx.WAITER
			original.disable_rounded_total = 1
			original.append(
				"items",
				{
					"item_code": till_fx.ITEM,
					"qty": 2,
					"rate": 50,
					"income_account": till_fx.INCOME_ACCOUNT,
					"cost_center": till_fx.COST_CENTER,
				},
			)
			original.custom_charged_tip = 20
			original.append("payments", {"mode_of_payment": till_fx.CASH, "amount": 120})
			original.insert(ignore_permissions=True)
			original.submit()

			# Partial return of 1 of 2 items where caller sets custom_charged_tip = 0
			# without manually deleting the copied Tips Payable tax row.
			ret = make_sales_return(original.name)
			self.assertEqual(ret.custom_charged_tip, -20)
			self.assertEqual(ret.custom_tip_server, till_fx.WAITER)
			ret.items[0].qty = -1
			ret.custom_charged_tip = 0
			ret.payments[0].amount = -50
			ret.insert(ignore_permissions=True)
			ret.submit()
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(ret.custom_charged_tip, 0)
		self.assertFalse(ret.custom_tip_server)
		self.assertEqual(_tip_rows(ret), [])
		self.assertEqual(ret.grand_total, -50)

	def test_saved_draft_return_can_zero_tip_without_resurrection(self):
		from erpnext.accounts.doctype.pos_invoice.pos_invoice import make_sales_return

		original = till_fx.bill(self.till, 100, [(till_fx.CASH, 120)], tip=20, disable_rounded_total=1)
		frappe.set_user(self.till.holder)
		try:
			ret = make_sales_return(original.name)
			ret.insert(ignore_permissions=True)
			self.assertEqual(ret.custom_charged_tip, -20)
			ret.custom_charged_tip = 0
			ret.payments[0].amount = -100
			ret.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(ret.custom_charged_tip, 0)
		self.assertFalse(ret.custom_tip_server)
		self.assertEqual(_tip_rows(ret), [])
		self.assertEqual(ret.grand_total, -100)

	def test_amended_cancelled_pos_invoice_succeeds_without_order_number_crash(self):
		original = till_fx.bill(self.till, 100, [(till_fx.CASH, 100)], disable_rounded_total=1)
		frappe.set_user("Administrator")
		frappe.get_doc("POS Invoice", original.name).cancel()

		amended = frappe.copy_doc(frappe.get_doc("POS Invoice", original.name))
		amended.docstatus = 0
		amended.amended_from = original.name
		amended.insert(ignore_permissions=True)
		amended.submit()

		self.assertTrue(amended.name.endswith("-1"), f"Expected amended name ending in -1, got {amended.name}")
		self.assertEqual(amended.docstatus, 1)

	def test_merged_pos_invoice_without_explicit_tip_keeps_card_overtender_per_tender_rule(self):
		from ury.ury.doctype.ury_order import ury_order as order

		m1 = till_fx.bill(self.till, 100, [(till_fx.CASH, 100)], waiter=self.till.holder, submit=False, disable_rounded_total=1)
		m2 = till_fx.bill(self.till, 50, [(till_fx.CASH, 50)], waiter=self.till.holder, submit=False, disable_rounded_total=1)
		frappe.db.set_value("POS Invoice", m1.name, {"custom_merged_pos_invoice": m2.name, "branch": till_fx.BRANCH})
		frappe.db.set_value("POS Invoice", m2.name, {"custom_merged_pos_invoice": m1.name, "branch": till_fx.BRANCH})
		frappe.get_doc("User", self.till.holder).add_roles("URY Cashier")
		prof = frappe.get_doc("POS Profile", self.till.name)
		if not any(r.role == "URY Cashier" for r in (prof.role_allowed_for_billing or [])):
			prof.append("role_allowed_for_billing", {"role": "URY Cashier"})
			prof.save(ignore_permissions=True)

		till_fx.set_overtender_rule(till_fx.CARD, "Refuse")
		frappe.set_user(self.till.holder)
		try:
			with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-TENDER-OVERTENDER\]"):
				order.make_invoice(
					till_fx.CUSTOMER,
					[{"mode_of_payment": till_fx.CARD, "amount": 197}],
					self.till.holder,
					self.till.name,
					till_fx.WAITER,
					invoice=m1.name,
					submit_invoice=1,
				)

			till_fx.set_overtender_rule(till_fx.CARD, "Post to Charged Tip")
			order.make_invoice(
				till_fx.CUSTOMER,
				[{"mode_of_payment": till_fx.CARD, "amount": 197}],
				self.till.holder,
				self.till.name,
				till_fx.WAITER,
				invoice=m1.name,
				submit_invoice=1,
			)
		finally:
			frappe.set_user("Administrator")
			till_fx.set_overtender_rule(till_fx.CARD, "Refuse")

		m1_doc = frappe.get_doc("POS Invoice", m1.name)
		m2_doc = frappe.get_doc("POS Invoice", m2.name)
		self.assertEqual(m1_doc.custom_charged_tip, 47)
		self.assertEqual(m1_doc.grand_total, 147)
		self.assertEqual(m1_doc.change_amount, 0)
		self.assertEqual(m2_doc.grand_total, 50)
