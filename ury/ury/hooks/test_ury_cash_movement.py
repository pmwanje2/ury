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
from frappe.utils import cint

from ury.tests import till_money_fixtures as till_fx

DRAWER_CREDIT = {"Pickup": True, "Loan": False, "Paid Out": True}
USD_CASH_ACCOUNT = "_Test Cash USD - _TC"
USD_DEBTORS_ACCOUNT = "_Test Debtors USD - _TC"
USD_MODE = "_Test Cash USD"
USD_CUSTOMER = "_Test Till Money USD Customer"


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


def _ensure_usd_till_masters():
	if not frappe.db.exists("Account", USD_CASH_ACCOUNT):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_Test Cash USD",
				"parent_account": "Cash In Hand - _TC",
				"company": till_fx.COMPANY,
				"is_group": 0,
				"root_type": "Asset",
				"account_type": "Cash",
				"account_currency": "USD",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Account", USD_DEBTORS_ACCOUNT):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_Test Debtors USD",
				"parent_account": "Accounts Receivable - _TC",
				"company": till_fx.COMPANY,
				"is_group": 0,
				"root_type": "Asset",
				"account_type": "Receivable",
				"account_currency": "USD",
			}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("Customer", USD_CUSTOMER):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": USD_CUSTOMER,
				"customer_group": "Individual",
				"territory": "All Territories",
				"default_currency": "USD",
				"accounts": [{"company": till_fx.COMPANY, "account": USD_DEBTORS_ACCOUNT}],
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)

	if not frappe.db.exists("Mode of Payment", USD_MODE):
		frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": USD_MODE,
				"type": "Cash",
				"accounts": [{"company": till_fx.COMPANY, "default_account": USD_CASH_ACCOUNT}],
			}
		).insert(ignore_permissions=True)

	today = frappe.utils.nowdate()
	existing_fx = frappe.db.get_value(
		"Currency Exchange",
		{"date": today, "from_currency": "USD", "to_currency": "INR"},
		"name",
	)
	if existing_fx:
		frappe.db.set_value("Currency Exchange", existing_fx, "exchange_rate", 80.0)
	else:
		frappe.get_doc(
			{
				"doctype": "Currency Exchange",
				"date": today,
				"from_currency": "USD",
				"to_currency": "INR",
				"exchange_rate": 80.0,
				"for_buying": 1,
				"for_selling": 1,
			}
		).insert(ignore_permissions=True)


def _open_usd_till(profile_name, holder, opening_amount=100):
	today = frappe.utils.nowdate()
	till_fx._make_role_only_user(holder, "URY Cashier")
	till_fx.assign_branch_user(till_fx.BRANCH, holder)
	if frappe.db.exists("POS Profile", profile_name):
		frappe.delete_doc("POS Profile", profile_name, ignore_permissions=True, force=1)
	profile = frappe.get_doc(
		{
			"doctype": "POS Profile",
			"name": profile_name,
			"company": till_fx.COMPANY,
			"currency": "USD",
			"warehouse": "_Test Warehouse - _TC",
			"cost_center": till_fx.COST_CENTER,
			"income_account": till_fx.INCOME_ACCOUNT,
			"expense_account": "Cost of Goods Sold - _TC",
			"write_off_account": "_Test Write Off - _TC",
			"write_off_cost_center": till_fx.COST_CENTER,
			"account_for_change_amount": USD_CASH_ACCOUNT,
			"selling_price_list": "Standard Selling",
			"branch": till_fx.BRANCH,
			"restaurant": till_fx.RESTAURANT,
			"custom_enable_cash_movements": 1,
			"custom_tips_payable_account": till_fx.TIPS_ACCOUNT,
			"custom_cash_safe_account": till_fx.SAFE_ACCOUNT,
			"custom_paid_out_account": till_fx.PAID_OUT_ACCOUNT,
			"custom_cash_over_short_account": till_fx.OVER_SHORT_ACCOUNT,
			"custom_paid_out_approval_threshold": "20",
			"custom_cash_movement_approver_role": till_fx.APPROVER_ROLE,
		}
	)
	profile.append("payments", {"mode_of_payment": USD_MODE, "default": 1})
	for email in (holder, till_fx.PURE_MANAGER, till_fx.WAITER):
		profile.append("applicable_for_users", {"user": email})
	profile.insert(ignore_permissions=True)

	opening = frappe.new_doc("POS Opening Entry")
	opening.pos_profile = profile_name
	opening.user = holder
	opening.company = till_fx.COMPANY
	opening.period_start_date = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-5)
	opening.posting_date = today
	opening.branch = till_fx.BRANCH
	opening.restaurant = till_fx.RESTAURANT
	opening.set("balance_details", [{"mode_of_payment": USD_MODE, "opening_amount": opening_amount}])
	opening.insert(ignore_permissions=True)
	opening.submit()
	return profile, opening


def _make_usd_invoice(profile_name, customer, rate=50, debit_to=None):
	inv = frappe.new_doc("POS Invoice")
	inv.pos_profile = profile_name
	inv.company = till_fx.COMPANY
	inv.customer = customer
	inv.currency = "USD"
	inv.conversion_rate = 80.0
	if debit_to:
		inv.debit_to = debit_to
	inv.is_pos = 1
	inv.update_stock = 0
	inv.waiter = till_fx.WAITER
	inv.disable_rounded_total = 1
	inv.append(
		"items",
		{
			"item_code": till_fx.ITEM,
			"qty": 1,
			"rate": rate,
			"income_account": till_fx.INCOME_ACCOUNT,
			"cost_center": till_fx.COST_CENTER,
		},
	)
	inv.append("payments", {"mode_of_payment": USD_MODE, "amount": rate})
	inv.insert(ignore_permissions=True)
	inv.submit()
	return inv


def _close_usd_till(profile_name, opening, holder, opening_amount, closing_amount):
	today = frappe.utils.nowdate()
	closing = frappe.new_doc("POS Closing Entry")
	closing.pos_opening_entry = opening.name
	closing.pos_profile = profile_name
	closing.company = till_fx.COMPANY
	closing.user = holder
	closing.period_start_date = opening.period_start_date
	closing.period_end_date = frappe.utils.now_datetime()
	closing.posting_date = today
	closing.posting_time = frappe.utils.nowtime()
	closing.append(
		"payment_reconciliation",
		{"mode_of_payment": USD_MODE, "opening_amount": opening_amount, "closing_amount": closing_amount},
	)
	closing.insert(ignore_permissions=True)
	closing.submit()
	closing.reload()
	return closing


class TestTillCashMovements(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		till_fx.ensure_masters()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

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

	def test_tip_payout_carries_tip_server_and_non_tip_movements_clear_it(self):
		field = frappe.get_meta("Journal Entry").get_field("custom_tip_server")
		self.assertIsNotNone(field, "Journal Entry needs custom_tip_server for Tip Payout attribution")
		self.assertEqual((field.fieldtype, field.options), ("Link", "User"))

		till = till_fx.make_till("_Test Till Tip Server")
		tip_payout = movement(
			till,
			"Paid Out",
			40,
			counter=till_fx.TIPS_ACCOUNT,
			reason="Server cash tips paid",
			extra={"custom_tip_server": till_fx.WAITER},
		)
		self.assertEqual(tip_payout.custom_tip_server, till_fx.WAITER)

		expense_po = movement(
			till,
			"Paid Out",
			40,
			counter=till_fx.PAID_OUT_ACCOUNT,
			reason="Ice",
			extra={"custom_tip_server": till_fx.WAITER},
		)
		self.assertFalse(expense_po.custom_tip_server)

		pickup = movement(
			till,
			"Pickup",
			50,
			extra={"custom_tip_server": till_fx.WAITER},
		)
		self.assertFalse(pickup.custom_tip_server)

	def test_paid_out_threshold_zero_requires_approval_while_blank_has_no_limit(self):
		strict_till = till_fx.make_till(
			"_Test Till Halo Zero",
			custom_paid_out_approval_threshold="0",
		)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-SECOND-PERSON\]"):
			movement(strict_till, "Paid Out", 1, reason="Small paid-out at HALO 0")

		draft = movement(strict_till, "Paid Out", 1, reason="Small paid-out at HALO 0", submit=False)
		approved = submit_as(draft, till_fx.MANAGER)
		self.assertEqual(approved.docstatus, 1)
		self.assertEqual(approved.custom_cash_movement_approver, till_fx.MANAGER)

		no_limit_till = till_fx.make_till(
			"_Test Till Halo Blank",
			custom_paid_out_approval_threshold="",
		)
		large = movement(no_limit_till, "Paid Out", 50000, reason="Bulk supplier cash")
		self.assertEqual(large.docstatus, 1)
		self.assertFalse(large.custom_cash_movement_approver)

	def test_foreign_currency_till_cash_movements_and_over_short_post_multi_currency_gl(self):
		from ury.ury.hooks.ury_cash_movement import till_cash_movements
		from ury.ury_pos import cash_movements

		# Standard clean Frappe/ERPNext v16 site defaults to flag == 0; foreign-currency tills
		# close cleanly when the Customer carries a matching-currency Receivable Party Account.
		self.assertEqual(
			cint(
				frappe.db.get_single_value(
					"Accounts Settings", "allow_multi_currency_invoices_against_single_party_account"
				)
			),
			0,
		)
		_ensure_usd_till_masters()

		profile_name = "_Test Till USD Foreign"
		holder = "till-money-usd-cashier@example.com"
		_profile, opening = _open_usd_till(profile_name, holder, opening_amount=100)
		inv = _make_usd_invoice(profile_name, USD_CUSTOMER, rate=50)

		frappe.set_user(till_fx.PURE_MANAGER)
		try:
			pickup = cash_movements.record_till_cash_movement(
				pos_profile=profile_name, movement_type="Pickup", amount=20, pos_opening_entry=opening.name
			)
			loan = cash_movements.record_till_cash_movement(
				pos_profile=profile_name, movement_type="Loan", amount=30, pos_opening_entry=opening.name
			)
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(holder)
		try:
			# $10 <= $20 threshold (even though 800 INR > 20): must self-submit!
			small_po = cash_movements.record_till_cash_movement(
				pos_profile=profile_name,
				movement_type="Paid Out",
				amount=10,
				reason="Ice",
				paid_out_target="expense",
				pos_opening_entry=opening.name,
			)
			# $25 > $20 threshold: must wait for second-person approval
			large_po = cash_movements.record_till_cash_movement(
				pos_profile=profile_name,
				movement_type="Paid Out",
				amount=25,
				reason="Server tip payout",
				paid_out_target="tips",
				tip_server=till_fx.WAITER,
				pos_opening_entry=opening.name,
			)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual((pickup["docstatus"], pickup["amount"], pickup["drawer_change"]), (1, 20.0, -20.0))
		self.assertEqual((loan["docstatus"], loan["amount"], loan["drawer_change"]), (1, 30.0, 30.0))
		self.assertEqual((small_po["docstatus"], small_po["amount"]), (1, 10.0))
		self.assertEqual((large_po["docstatus"], large_po["amount"], large_po["tip_server"]), (0, 25.0, till_fx.WAITER))

		frappe.set_user(till_fx.PURE_MANAGER)
		try:
			approved = cash_movements.approve_till_cash_movement(large_po["name"])
			self.assertEqual(approved["docstatus"], 1)
			state = cash_movements.get_till_cash_state(profile_name)
			totals = cash_movements.get_shift_cash_movement_totals(opening.name)
		finally:
			frappe.set_user("Administrator")

		# Net drawer change in profile currency (USD): -20 + 30 - 10 - 25 = -25 USD
		self.assertEqual(till_cash_movements(opening.name), {USD_CASH_ACCOUNT: -25.0})
		self.assertEqual(state["net_drawer_movement"], -25.0)
		self.assertEqual(totals["by_mode"], {USD_MODE: -25.0})

		# Verify multi_currency & balanced INR GL on Pickup ($20 * 80 = 1600 INR)
		pickup_je = frappe.get_doc("Journal Entry", pickup["name"])
		self.assertEqual(pickup_je.multi_currency, 1)
		self.assertEqual(
			till_fx.gl_by_account("Journal Entry", pickup["name"]),
			{till_fx.SAFE_ACCOUNT: (1600.0, 0), USD_CASH_ACCOUNT: (0, 1600.0)},
		)

		# Close USD till counting $120 vs expected $125 ($100 open + $50 sale - $25 movements) -> -$5 short
		closing = _close_usd_till(profile_name, opening, holder, opening_amount=100, closing_amount=120)

		row = closing.payment_reconciliation[0]
		self.assertEqual((row.expected_amount, row.closing_amount, row.difference), (125.0, 120.0, -5.0))
		consolidated = frappe.db.get_value("POS Invoice", inv.name, "consolidated_invoice")
		self.assertTrue(consolidated, "USD POS Invoice must consolidate on shift close")
		self.assertEqual(frappe.db.get_value("Sales Invoice", consolidated, "debit_to"), USD_DEBTORS_ACCOUNT)
		entries = over_short_entries(closing)
		self.assertEqual(len(entries), 1)
		os_je = frappe.get_doc("Journal Entry", entries[0])
		self.assertEqual(os_je.multi_currency, 1)
		# $5 * 80 = 400 INR balanced in company GL
		self.assertEqual(
			till_fx.gl_by_account("Journal Entry", entries[0]),
			{till_fx.OVER_SHORT_ACCOUNT: (400.0, 0), USD_CASH_ACCOUNT: (0, 400.0)},
		)

	def test_foreign_currency_till_preserves_invoice_usd_receivable_and_refuses_misconfigured_flag_zero(self):
		self.assertEqual(
			cint(
				frappe.db.get_single_value(
					"Accounts Settings", "allow_multi_currency_invoices_against_single_party_account"
				)
			),
			0,
		)
		_ensure_usd_till_masters()

		# Case 1: explicit USD debit_to on POS Invoice (even for INR-default Customer) is preserved and consolidates cleanly.
		profile_ok = "_Test Till USD Explicit DebitTo"
		holder_ok = "till-money-usd-explicit@example.com"
		_p1, opening_ok = _open_usd_till(profile_ok, holder_ok, opening_amount=100)
		inv_ok = _make_usd_invoice(profile_ok, till_fx.CUSTOMER, rate=40, debit_to=USD_DEBTORS_ACCOUNT)
		closing_ok = _close_usd_till(profile_ok, opening_ok, holder_ok, opening_amount=100, closing_amount=140)
		self.assertEqual(closing_ok.docstatus, 1)
		consolidated_ok = frappe.db.get_value("POS Invoice", inv_ok.name, "consolidated_invoice")
		self.assertTrue(consolidated_ok)
		self.assertEqual(frappe.db.get_value("Sales Invoice", consolidated_ok, "debit_to"), USD_DEBTORS_ACCOUNT)

		# Case 2: neither USD Party Account/debit_to nor Accounts Settings flag enabled -> actionable coded refusal.
		inr_only_customer = "_Test Till Money INR Only Customer"
		if not frappe.db.exists("Customer", inr_only_customer):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": inr_only_customer,
					"customer_group": "Individual",
					"territory": "All Territories",
				}
			).insert(ignore_permissions=True, ignore_mandatory=True)
		profile_bad = "_Test Till USD Misconfigured"
		holder_bad = "till-money-usd-misconf@example.com"
		_p2, opening_bad = _open_usd_till(profile_bad, holder_bad, opening_amount=100)
		_make_usd_invoice(profile_bad, inr_only_customer, rate=30)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CLOSE-FOREIGN-RECEIVABLE\]") as ctx:
			_close_usd_till(profile_bad, opening_bad, holder_bad, opening_amount=100, closing_amount=130)
		self.assertIn("Accounts Settings", str(ctx.exception))

	def test_foreign_currency_till_closes_with_single_party_account_flag_enabled(self):
		original = cint(
			frappe.db.get_single_value(
				"Accounts Settings", "allow_multi_currency_invoices_against_single_party_account"
			)
		)

		def _restore_flag():
			frappe.db.set_single_value(
				"Accounts Settings", "allow_multi_currency_invoices_against_single_party_account", original
			)
			frappe.db.commit()

		self.addCleanup(_restore_flag)
		frappe.db.set_single_value(
			"Accounts Settings", "allow_multi_currency_invoices_against_single_party_account", 1
		)
		_ensure_usd_till_masters()

		profile_name = "_Test Till USD Flag Enabled"
		holder = "till-money-usd-flag1@example.com"
		_profile, opening = _open_usd_till(profile_name, holder, opening_amount=100)
		inv = _make_usd_invoice(profile_name, till_fx.CUSTOMER, rate=50)
		closing = _close_usd_till(profile_name, opening, holder, opening_amount=100, closing_amount=145)
		self.assertEqual(closing.docstatus, 1)
		consolidated = frappe.db.get_value("POS Invoice", inv.name, "consolidated_invoice")
		self.assertTrue(consolidated)
		entries = over_short_entries(closing)
		self.assertEqual(len(entries), 1)
		self.assertEqual(
			till_fx.gl_by_account("Journal Entry", entries[0]),
			{till_fx.OVER_SHORT_ACCOUNT: (400.0, 0), USD_CASH_ACCOUNT: (0, 400.0)},
		)


class TestCashOverShort(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		till_fx.ensure_masters()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

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

	def test_over_short_reason_required_only_above_configured_tolerance(self):
		self.assertIsNotNone(frappe.get_meta("POS Profile").get_field("custom_require_over_short_reason"))
		self.assertIsNotNone(frappe.get_meta("POS Profile").get_field("custom_over_short_reason_tolerance"))
		self.assertIsNotNone(frappe.get_meta("POS Closing Entry").get_field("custom_over_short_reason"))

		# Within tolerance (|-8| <= 10): blank reason allowed.
		till_within = till_fx.make_till(
			"_Test Till Over Short Within Tol",
			custom_require_over_short_reason=1,
			custom_over_short_reason_tolerance=10,
		)
		closing_ok = till_fx.close_till(till_within, counted=992)
		self.assertEqual(closing_ok.docstatus, 1)

		# Above tolerance (|-25| > 10): blank reason refused, non-blank reason accepted and copied to JE remark.
		till_above = till_fx.make_till(
			"_Test Till Over Short Above Tol",
			custom_require_over_short_reason=1,
			custom_over_short_reason_tolerance=10,
		)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CLOSE-OVER-SHORT-REASON\]"):
			till_fx.close_till(till_above, counted=975)

		frappe.set_user(till_above.holder)
		try:
			closing = frappe.new_doc("POS Closing Entry")
			closing.pos_opening_entry = till_above.opening.name
			closing.pos_profile = till_above.name
			closing.company = till_fx.COMPANY
			closing.user = till_above.holder
			closing.period_start_date = till_above.opening.period_start_date
			closing.period_end_date = frappe.utils.now_datetime()
			closing.posting_date = frappe.utils.nowdate()
			closing.posting_time = frappe.utils.nowtime()
			closing.custom_over_short_reason = "Miscounted 25 note"
			closing.append(
				"payment_reconciliation",
				{"mode_of_payment": till_fx.CASH, "opening_amount": 1000, "closing_amount": 975},
			)
			closing.insert(ignore_permissions=True)
			closing.submit()
		finally:
			frappe.set_user("Administrator")

		entries = over_short_entries(closing)
		self.assertEqual(len(entries), 1)
		remark = frappe.db.get_value("Journal Entry", entries[0], "user_remark") or ""
		self.assertIn("Miscounted 25 note", remark)
