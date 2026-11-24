"""Till cash management on the real v16 lifecycle (database-backed).

Oracle Simphony Cash Management moves cash in and out of a till during the
shift: a Pickup (safe drop) takes cash to the safe, a Loan brings float from
the safe, and a Paid-Out pays an expense (or a tip payout) from the drawer,
with a reason and, above the tender's HALO, a second person. At close, the
counted-against-expected variance posts to a Cash Over/Short account.

Here each movement is a native Journal Entry (``Cash Entry``) linked to the
POS Opening Entry; its till-drawer line moves expected cash at closing. The
Over/Short entry is a native Journal Entry the closing posts on submit. All
accounts, the threshold and the approver role are POS Profile settings.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.tests import till_money_fixtures as till_fx

DRAWER_CREDIT = {"Pickup": True, "Loan": False, "Paid Out": True}


def movement(till, kind, amount, counter=None, reason=None, submit=True, drawer=None, creator=None, extra=None):
	if counter is None:
		counter = till_fx.PAID_OUT_ACCOUNT if kind == "Paid Out" else till_fx.SAFE_ACCOUNT
	drawer = drawer or till_fx.CASH_ACCOUNT
	out = DRAWER_CREDIT[kind]
	frappe.set_user(creator or till.holder)
	try:
		payload = {
			"doctype": "Journal Entry",
			"voucher_type": "Cash Entry",
			"company": till_fx.COMPANY,
			"posting_date": frappe.utils.nowdate(),
			"custom_pos_opening_entry": till.opening.name,
			"custom_cash_movement_type": kind,
			"custom_cash_movement_reason": reason,
			"accounts": [
				{
					"account": drawer,
					"debit_in_account_currency": 0 if out else amount,
					"credit_in_account_currency": amount if out else 0,
					"cost_center": till_fx.COST_CENTER,
				},
				{
					"account": counter,
					"debit_in_account_currency": amount if out else 0,
					"credit_in_account_currency": 0 if out else amount,
					"cost_center": till_fx.COST_CENTER,
				},
			],
		}
		if extra:
			payload.update(extra)
		entry = frappe.get_doc(payload)
		entry.insert()
		if submit:
			entry.submit()
	finally:
		frappe.set_user("Administrator")
	return entry


def submit_as(entry, user):
	frappe.set_user(user)
	try:
		entry = frappe.get_doc("Journal Entry", entry.name)
		entry.submit()
	finally:
		frappe.set_user("Administrator")
	return entry


def over_short_entries(closing):
	return frappe.get_all(
		"Journal Entry",
		filters={"custom_pos_closing_entry": closing.name, "docstatus": 1},
		pluck="name",
	)


class TestTillCashMovements(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		till_fx.ensure_masters()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_pickup_loan_and_paid_out_move_expected_cash(self):
		till = till_fx.make_till("_Test Till Movements")
		till_fx.bill(till, 100, [(till_fx.CASH, 100)])
		movement(till, "Pickup", 300)
		movement(till, "Loan", 200)
		movement(till, "Paid Out", 50, reason="Ice for the bar")

		closing = till_fx.close_till(till, counted=950)

		row = closing.payment_reconciliation[0]
		self.assertEqual((row.expected_amount, row.difference), (950, 0))
		self.assertEqual(over_short_entries(closing), [])

	def test_paid_out_needs_a_reason(self):
		till = till_fx.make_till("_Test Till Paid Out Reason")
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-REASON\]"):
			movement(till, "Paid Out", 50, reason="  ")

	def test_paid_out_above_threshold_needs_a_second_person(self):
		till = till_fx.make_till("_Test Till Paid Out Approval")
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-SECOND-PERSON\]"):
			movement(till, "Paid Out", 600, reason="Gas refill")

		draft = movement(till, "Paid Out", 600, reason="Gas refill", submit=False)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-SECOND-PERSON\]"):
			submit_as(draft, till_fx.WAITER)
		approved = submit_as(draft, till_fx.MANAGER)
		self.assertEqual(approved.docstatus, 1)
		self.assertEqual(approved.custom_cash_movement_approver, till_fx.MANAGER)

		small = movement(till, "Paid Out", 500, reason="Lemons")
		self.assertEqual(small.docstatus, 1)

	def test_movement_counter_account_is_the_configured_one(self):
		till = till_fx.make_till("_Test Till Counter Account")
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-COUNTER\]"):
			movement(till, "Pickup", 100, counter=till_fx.PAID_OUT_ACCOUNT)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-COUNTER\]"):
			movement(till, "Paid Out", 100, counter=till_fx.CARD_ACCOUNT, reason="Bank")

		tip_payout = movement(till, "Paid Out", 40, counter=till_fx.TIPS_ACCOUNT, reason="Tip payout")
		self.assertEqual(tip_payout.docstatus, 1)

	def test_movement_must_move_the_till_drawer(self):
		till = till_fx.make_till("_Test Till Drawer Line")
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-DRAWER\]"):
			movement(till, "Pickup", 100, drawer=till_fx.CARD_ACCOUNT)

	def test_movement_needs_an_open_shift(self):
		till = till_fx.make_till("_Test Till Closed Shift")
		till_fx.close_till(till, counted=1000)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-SHIFT\]"):
			movement(till, "Pickup", 100)

	def test_closing_waits_for_a_pending_movement(self):
		till = till_fx.make_till("_Test Till Pending Movement")
		movement(till, "Paid Out", 600, reason="Gas refill", submit=False)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CLOSE-PENDING-MOVEMENT\]"):
			till_fx.close_till(till, counted=400)

	def test_movement_cannot_be_cancelled_after_its_shift_closed(self):
		till = till_fx.make_till("_Test Till Cancel After Close")
		pickup = movement(till, "Pickup", 100)
		till_fx.close_till(till, counted=900)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-SHIFT\]"):
			frappe.get_doc("Journal Entry", pickup.name).cancel()

	def test_manager_cannot_self_submit_own_paid_out_above_threshold(self):
		till = till_fx.make_till("_Test Till Manager SoD")
		draft = movement(till, "Paid Out", 600, reason="Generator fuel", submit=False, creator=till_fx.MANAGER)
		self.assertEqual(draft.owner, till_fx.MANAGER)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-SECOND-PERSON\]") as ctx:
			submit_as(draft, till_fx.MANAGER)
		message = str(ctx.exception).lower()
		for vendor in ("ury", "kamra", "huf"):
			self.assertNotIn(vendor, message)

		approved = submit_as(draft, till_fx.MANAGER_2)
		self.assertEqual(approved.docstatus, 1)
		self.assertEqual(approved.custom_cash_movement_approver, till_fx.MANAGER_2)

	def test_approver_field_cannot_be_spoofed_on_draft_or_below_threshold(self):
		till = till_fx.make_till("_Test Till Approver Spoof")
		small = movement(
			till,
			"Paid Out",
			300,
			reason="Lemons",
			submit=True,
			extra={"custom_cash_movement_approver": till_fx.MANAGER},
		)
		self.assertFalse(small.custom_cash_movement_approver)

		pickup = movement(
			till,
			"Pickup",
			100,
			submit=True,
			extra={"custom_cash_movement_approver": till_fx.MANAGER},
		)
		self.assertFalse(pickup.custom_cash_movement_approver)

		draft = movement(
			till,
			"Paid Out",
			600,
			reason="Gas refill",
			submit=False,
			extra={"custom_cash_movement_approver": till_fx.MANAGER},
		)
		self.assertFalse(draft.custom_cash_movement_approver)

	def test_pos_profile_account_fields_declare_link_filters(self):
		meta = frappe.get_meta("POS Profile")
		expected_root = {
			"custom_tips_payable_account": ("=", "Liability"),
			"custom_cash_over_short_account": ("in", ["Expense", "Income"]),
			"custom_cash_safe_account": ("=", "Asset"),
			"custom_paid_out_account": ("=", "Expense"),
		}
		for fieldname, root_clause in expected_root.items():
			with self.subTest(fieldname=fieldname):
				field = meta.get_field(fieldname)
				self.assertIsNotNone(field)
				self.assertTrue(field.link_filters, f"{fieldname} must define link_filters")
				clauses = json.loads(field.link_filters)
				self.assertIn(["Account", "company", "=", "eval:doc.company"], clauses)
				self.assertIn(["Account", "is_group", "=", 0], clauses)
				self.assertIn(["Account", "disabled", "=", 0], clauses)
				self.assertIn(["Account", "root_type", root_clause[0], root_clause[1]], clauses)


class TestCashOverShort(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		till_fx.ensure_masters()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_short_close_posts_over_short_journal(self):
		till = till_fx.make_till("_Test Till Short")
		till_fx.bill(till, 100, [(till_fx.CASH, 100)])
		closing = till_fx.close_till(till, counted=1080)

		self.assertEqual(closing.payment_reconciliation[0].difference, -20)
		entries = over_short_entries(closing)
		self.assertEqual(len(entries), 1)
		gl = till_fx.gl_by_account("Journal Entry", entries[0])
		self.assertEqual(gl, {till_fx.OVER_SHORT_ACCOUNT: (20, 0), till_fx.CASH_ACCOUNT: (0, 20)})

	def test_over_close_posts_over_short_journal(self):
		till = till_fx.make_till("_Test Till Over")
		till_fx.bill(till, 100, [(till_fx.CASH, 100)])
		closing = till_fx.close_till(till, counted=1115)

		entries = over_short_entries(closing)
		self.assertEqual(len(entries), 1)
		gl = till_fx.gl_by_account("Journal Entry", entries[0])
		self.assertEqual(gl, {till_fx.CASH_ACCOUNT: (15, 0), till_fx.OVER_SHORT_ACCOUNT: (0, 15)})

	def test_even_close_posts_nothing(self):
		till = till_fx.make_till("_Test Till Even")
		till_fx.bill(till, 100, [(till_fx.CASH, 100)])
		closing = till_fx.close_till(till, counted=1100)
		self.assertEqual(over_short_entries(closing), [])

	def test_cancelling_the_closing_cancels_its_over_short_journal(self):
		till = till_fx.make_till("_Test Till Cancel Closing")
		till_fx.bill(till, 100, [(till_fx.CASH, 100)])
		closing = till_fx.close_till(till, counted=1080)
		entries = over_short_entries(closing)
		self.assertEqual(len(entries), 1)

		frappe.get_doc("POS Closing Entry", closing.name).cancel()

		self.assertEqual(frappe.db.get_value("Journal Entry", entries[0], "docstatus"), 2)

	def test_cash_difference_needs_an_over_short_account(self):
		till = till_fx.make_till("_Test Till No Over Short", custom_cash_over_short_account=None)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CLOSE-OVER-SHORT-ACCOUNT\]"):
			till_fx.close_till(till, counted=990)
