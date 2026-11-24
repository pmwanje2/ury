"""Cashier touchscreen till cash movements (Pickup / Loan / Paid Out) under app authority.

Oracle Simphony Cash Management lets a cashier record Pickups (safe drops),
Loans (float in) and Paid-Outs on the POS touchscreen without granting cashiers
general-ledger Journal Entry DocPerms. The POS API validates the till's POS
Profile configuration (`custom_enable_cash_movements`, drawer and counter
accounts, reason requirement, and second-person threshold) and writes the
native Cash Entry Journal Entry under app authority.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.tests import till_money_fixtures as till_fx
from ury.ury_pos import api as pos_api


class TestTillCashMovementsAPI(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		till_fx.ensure_masters()
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		till_fx.set_overtender_rule(till_fx.CARD, "Refuse")
		super().tearDown()

	def test_pure_cashier_has_no_journal_entry_permissions(self):
		for ptype in ("read", "write", "create", "submit", "cancel"):
			self.assertFalse(
				frappe.has_permission("Journal Entry", ptype, user=till_fx.PURE_CASHIER),
				f"URY Cashier must not hold Journal Entry {ptype} permission",
			)

	def test_cash_movements_are_disabled_by_default_per_pos_profile(self):
		from ury.ury_pos import cash_movements

		field = frappe.get_meta("POS Profile").get_field("custom_enable_cash_movements")
		self.assertIsNotNone(field, "POS Profile needs custom_enable_cash_movements")
		self.assertEqual(str(field.default), "0")

		till = till_fx.make_till(
			"_Test Till Cash Disabled",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=0,
		)
		frappe.set_user(till_fx.PURE_CASHIER)
		state = cash_movements.get_till_cash_state(pos_profile=till.name)
		self.assertFalse(state["enabled"])
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-DISABLED\]"):
			cash_movements.record_till_cash_movement(
				pos_profile=till.name,
				movement_type="Pickup",
				amount=100,
			)

	def test_pure_cashier_records_pickup_loan_and_paid_out_without_je_perms(self):
		from ury.ury_pos import cash_movements

		till = till_fx.make_till(
			"_Test Till Cashier Movements",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=1,
		)
		# Pure URY Cashier (zero Journal Entry DocPerms) cannot run Loan without cash_loan
		# privilege/PIN; URY Manager records Pickup & Loan while Pure Cashier records
		# below-threshold Paid Out & Tip Payout under app authority.
		frappe.set_user(till_fx.PURE_CASHIER)
		with self.assertRaisesRegex(frappe.PermissionError, r"\[POS-AUTH-REQUIRED\]"):
			cash_movements.record_till_cash_movement(
				pos_profile=till.name, movement_type="Loan", amount=200
			)

		frappe.set_user(till_fx.PURE_MANAGER)
		pickup = cash_movements.record_till_cash_movement(
			pos_profile=till.name, movement_type="Pickup", amount=300
		)
		self.assertEqual(pickup["docstatus"], 1)
		self.assertEqual(pickup["status"], "Submitted")

		loan = cash_movements.record_till_cash_movement(
			pos_profile=till.name, movement_type="Loan", amount=200
		)
		self.assertEqual(loan["docstatus"], 1)

		frappe.set_user(till_fx.PURE_CASHIER)
		paid_out = cash_movements.record_till_cash_movement(
			pos_profile=till.name,
			movement_type="Paid Out",
			amount=50,
			reason="Fresh mint",
			paid_out_target="expense",
		)
		self.assertEqual(paid_out["docstatus"], 1)
		self.assertEqual(paid_out["approver"], "")

		tip_payout = cash_movements.record_till_cash_movement(
			pos_profile=till.name,
			movement_type="Paid Out",
			amount=40,
			reason="Server tip payout",
			paid_out_target="tips",
		)
		self.assertEqual(tip_payout["docstatus"], 1)

		state = cash_movements.get_till_cash_state(pos_profile=till.name)
		self.assertTrue(state["enabled"])
		self.assertEqual(state["net_drawer_movement"], -190.0)
		self.assertEqual(state["pending_count"], 0)
		self.assertEqual(len(state["movements"]), 4)

		totals = cash_movements.get_shift_cash_movement_totals(till.opening.name)
		self.assertEqual(totals["by_mode"], {till_fx.CASH: -190.0})
		self.assertEqual(totals["pending_count"], 0)

		frappe.set_user("Administrator")
		self.assertEqual(
			till_fx.gl_by_account("Journal Entry", pickup["name"]),
			{till_fx.SAFE_ACCOUNT: (300, 0), till_fx.CASH_ACCOUNT: (0, 300)},
		)
		self.assertEqual(
			till_fx.gl_by_account("Journal Entry", tip_payout["name"]),
			{till_fx.TIPS_ACCOUNT: (40, 0), till_fx.CASH_ACCOUNT: (0, 40)},
		)

		closing = till_fx.close_till(till, counted=810)
		row = closing.payment_reconciliation[0]
		self.assertEqual((row.expected_amount, row.difference), (810, 0))

	def test_paid_out_above_threshold_waits_for_second_person_manager_approval(self):
		from ury.ury_pos import cash_movements

		till = till_fx.make_till(
			"_Test Till Cashier Approval",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=1,
		)
		frappe.set_user(till_fx.PURE_CASHIER)
		pending = cash_movements.record_till_cash_movement(
			pos_profile=till.name,
			movement_type="Paid Out",
			amount=600,
			reason="Kitchen gas cylinder",
			paid_out_target="expense",
		)
		self.assertEqual(pending["docstatus"], 0)
		self.assertEqual(pending["status"], "Pending Approval")
		self.assertEqual(pending["approver"], "")

		totals = cash_movements.get_shift_cash_movement_totals(till.opening.name)
		self.assertEqual(totals["by_mode"], {})
		self.assertEqual(totals["pending_count"], 1)

		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-SECOND-PERSON\]") as ctx:
			cash_movements.approve_till_cash_movement(pending["name"])
		self.assertNotIn("ury", str(ctx.exception).lower())

		# Pure URY Manager approves without holding Journal Entry DocPerms.
		frappe.set_user(till_fx.PURE_MANAGER)
		state = cash_movements.get_till_cash_state(pos_profile=till.name)
		self.assertTrue(state["can_approve"])
		row = next(item for item in state["movements"] if item["name"] == pending["name"])
		self.assertTrue(row["can_approve"])

		approved = cash_movements.approve_till_cash_movement(pending["name"])
		self.assertEqual(approved["docstatus"], 1)
		self.assertEqual(approved["status"], "Submitted")
		self.assertEqual(approved["approver"], till_fx.PURE_MANAGER)

		totals_after = cash_movements.get_shift_cash_movement_totals(till.opening.name)
		self.assertEqual(totals_after["by_mode"], {till_fx.CASH: -600.0})
		self.assertEqual(totals_after["pending_count"], 0)

	def test_manager_cashier_cannot_self_approve_via_pos_api(self):
		from ury.ury_pos import cash_movements

		till = till_fx.make_till(
			"_Test Till Manager API SoD",
			holder=till_fx.PURE_MANAGER,
			custom_enable_cash_movements=1,
		)
		frappe.set_user(till_fx.PURE_MANAGER)
		pending = cash_movements.record_till_cash_movement(
			pos_profile=till.name,
			movement_type="Paid Out",
			amount=600,
			reason="Emergency ice",
		)
		self.assertEqual(pending["docstatus"], 0)
		state = cash_movements.get_till_cash_state(pos_profile=till.name)
		row = next(item for item in state["movements"] if item["name"] == pending["name"])
		self.assertFalse(row["can_approve"])

		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-SECOND-PERSON\]"):
			cash_movements.approve_till_cash_movement(pending["name"])

		frappe.set_user(till_fx.MANAGER_2)
		approved = cash_movements.approve_till_cash_movement(pending["name"])
		self.assertEqual(approved["docstatus"], 1)
		self.assertEqual(approved["approver"], till_fx.MANAGER_2)

	def test_discard_pending_movement_and_forbid_submitted_discard(self):
		from ury.ury_pos import cash_movements

		till = till_fx.make_till(
			"_Test Till Discard Movement",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=1,
		)
		frappe.set_user(till_fx.PURE_CASHIER)
		pending = cash_movements.record_till_cash_movement(
			pos_profile=till.name, movement_type="Paid Out", amount=600, reason="Wrong entry"
		)
		discarded = cash_movements.discard_till_cash_movement(pending["name"])
		self.assertEqual(discarded["status"], "Discarded")
		self.assertFalse(frappe.db.exists("Journal Entry", pending["name"]))

		submitted = cash_movements.record_till_cash_movement(
			pos_profile=till.name, movement_type="Paid Out", amount=100, reason="Ice delivery"
		)
		with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CASH-NOT-DRAFT\]"):
			cash_movements.discard_till_cash_movement(submitted["name"])

	def test_unauthorized_user_and_other_branch_refused(self):
		from ury.ury_pos import cash_movements

		till = till_fx.make_till(
			"_Test Till Cash Authz",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=1,
		)
		frappe.set_user(till_fx.PURE_WAITER)
		with self.assertRaises(frappe.PermissionError):
			cash_movements.record_till_cash_movement(
				pos_profile=till.name, movement_type="Pickup", amount=50
			)

		frappe.set_user(till_fx.OTHER_BRANCH_USER)
		with self.assertRaises(frappe.PermissionError):
			cash_movements.get_till_cash_state(pos_profile=till.name)

	def test_get_pos_profile_exposes_cash_movements_flag_and_mode_meta(self):
		till_fx.set_overtender_rule(till_fx.CARD, "Post to Charged Tip")
		till = till_fx.make_till(
			"_Test Till Profile Meta",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=1,
		)
		frappe.set_user(till_fx.PURE_CASHIER)
		# Point branch lookup to this till profile for getPosProfile()
		frappe.db.set_value("POS Profile", till.name, "disabled", 0)
		details = pos_api.getPosProfile()
		self.assertIn("enable_cash_movements", details)
		self.assertIn("payment_mode_meta", details)
		meta = details["payment_mode_meta"]
		self.assertEqual(meta[till_fx.CASH]["type"], "Cash")
		self.assertEqual(meta[till_fx.CARD]["type"], "Bank")
		self.assertEqual(meta[till_fx.CARD]["overtender_rule"], "Post to Charged Tip")
		self.assertEqual(meta[till_fx.WIRE]["overtender_rule"], "Refuse")
		self.assertIn("require_over_short_reason", details)
		self.assertIn("over_short_reason_tolerance", details)

	def test_tip_payout_api_stores_tip_server_and_lists_servers_in_state(self):
		from ury.ury_pos import cash_movements

		till = till_fx.make_till(
			"_Test Till Tip Server API",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=1,
		)
		frappe.set_user(till_fx.PURE_CASHIER)
		state = cash_movements.get_till_cash_state(pos_profile=till.name)
		server_emails = [s["user"] for s in state.get("servers") or []]
		self.assertIn(till_fx.WAITER, server_emails)

		row = cash_movements.record_till_cash_movement(
			pos_profile=till.name,
			movement_type="Paid Out",
			amount=40,
			reason="Server cash tips paid",
			paid_out_target="tips",
			tip_server=till_fx.WAITER,
		)
		self.assertEqual(row["docstatus"], 1)
		self.assertEqual(row["tip_server"], till_fx.WAITER)
		self.assertEqual(
			frappe.db.get_value("Journal Entry", row["name"], "custom_tip_server"),
			till_fx.WAITER,
		)

	def test_get_till_cash_state_distinguishes_zero_and_blank_threshold(self):
		from ury.ury_pos import cash_movements

		strict_till = till_fx.make_till(
			"_Test Till API Halo Zero",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=1,
			custom_paid_out_approval_threshold="0",
		)
		frappe.set_user(till_fx.PURE_CASHIER)
		strict_state = cash_movements.get_till_cash_state(pos_profile=strict_till.name)
		self.assertEqual(strict_state["paid_out_threshold"], 0.0)
		pending = cash_movements.record_till_cash_movement(
			pos_profile=strict_till.name,
			movement_type="Paid Out",
			amount=5,
			reason="Tiny paid-out",
		)
		self.assertEqual(pending["docstatus"], 0)
		cash_movements.discard_till_cash_movement(pending["name"])

		frappe.set_user("Administrator")
		till_fx.close_till(strict_till, counted=1000)

		no_limit_till = till_fx.make_till(
			"_Test Till API Halo Blank",
			holder=till_fx.PURE_CASHIER,
			custom_enable_cash_movements=1,
			custom_paid_out_approval_threshold="",
		)
		frappe.set_user(till_fx.PURE_CASHIER)
		blank_state = cash_movements.get_till_cash_state(pos_profile=no_limit_till.name)
		self.assertIsNone(blank_state["paid_out_threshold"])
		submitted = cash_movements.record_till_cash_movement(
			pos_profile=no_limit_till.name,
			movement_type="Paid Out",
			amount=50000,
			reason="Unlimited paid-out",
		)
		self.assertEqual(submitted["docstatus"], 1)
