"""Live Frappe + ERPNext v16 + MariaDB 11.8 integration tests for POS till cash controls,
closing controls, and tender HALO wired to #147 R2 (__Auth PIN core, non-self-approval,
server-side POS Reason validation, and immutable POS Authorisation Log).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import flt, nowdate, random_string
from frappe.utils.password import update_password


class TestTillControlsIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        frappe.set_user("Administrator")
        cls.suffix = random_string(6).lower()
        cls.created: list[tuple[str, str]] = []
        cls.created_jes: list[str] = []
        cls.created_closings: list[str] = []
        cls.created_openings: list[str] = []
        cls.created_invoices: list[str] = []

        # Ensure POS Settings uses POS Invoice mode for closing consolidation tests
        cls.prev_invoice_type = frappe.db.get_single_value("POS Settings", "invoice_type")
        frappe.db.set_single_value("POS Settings", "invoice_type", "POS Invoice")

        # Ensure UGX currency exists with 0 decimal places
        if not frappe.db.exists("Currency", "UGX"):
            frappe.get_doc({
                "doctype": "Currency",
                "currency_name": "UGX",
                "enabled": 1,
                "fraction": "",
                "fraction_units": 1,
                "smallest_currency_fraction_value": 1,
                "symbol": "UGX",
                "number_format": "#,###",
            }).insert(ignore_permissions=True)
        else:
            frappe.db.set_value("Currency", "UGX", {
                "enabled": 1,
                "fraction_units": 1,
                "smallest_currency_fraction_value": 1,
            })

        cls.company = cls._ensure_company(f"TillCo-{cls.suffix}", f"TC{cls.suffix[:3].upper()}")
        cls.cash_account = cls._make_account("Till Cash", "Cash", "Asset", "Current Assets")
        cls.safe_account = cls._make_account("Till Safe", "Cash", "Asset", "Current Assets")
        cls.paid_out_account = cls._make_account("POS Paid Outs", "Expense Account", "Expense", "Indirect Expenses")
        cls.paid_in_account = cls._make_account("POS Paid Ins", "Income Account", "Income", "Indirect Income")
        cls.over_short_account = cls._make_account("Cash Over and Short", "Expense Account", "Expense", "Indirect Expenses")
        cls.tips_account = cls._make_account("Tips Payable", "", "Liability", "Current Liabilities")
        cls.income_account = frappe.db.get_value(
            "Account", {"company": cls.company, "is_group": 0, "root_type": "Income"}, "name"
        )
        cls.cost_center = frappe.db.get_value("Company", cls.company, "cost_center")
        cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")

        cls.cashier = cls._make_user(f"cashier1-{cls.suffix}@example.test", "Cashier One", ["URY Cashier"])
        cls.cashier2 = cls._make_user(f"cashier2-{cls.suffix}@example.test", "Cashier Two", ["URY Cashier"])
        cls.manager = cls._make_user(f"manager1-{cls.suffix}@example.test", "Manager One", ["URY Manager"])

        cls.branch = cls._make_branch(f"MainBranch-{cls.suffix}", [cls.cashier, cls.cashier2, cls.manager])
        cls.restaurant = cls._make_restaurant(f"Rest-{cls.suffix}")
        cls.cash_mop = cls._make_mop(f"Cash-{cls.suffix}", "Cash", cls.cash_account, halo=500000)
        cls.customer = cls._make_customer(f"WalkIn-{cls.suffix}")
        cls.item = cls._make_item(f"ITEM-TC-{cls.suffix}", rate=50000)

        cls.profile_name = cls._make_pos_profile(f"MainTill-{cls.suffix}")

        # Seed #147 R2 default rules + reasons for this company
        from ury.patches.v2_0 import setup_pos_manager_authorisation

        setup_pos_manager_authorisation.execute()
        setup_pos_manager_authorisation.seed_default_rules(company=cls.company)
        setup_pos_manager_authorisation.seed_default_reasons()

        # Enroll manager's PIN in __Auth via real #147 R2 set_my_pin (interactive session)
        cls.manager_pin = "2468"
        cls.cashier_pin = "1357"
        with cls._interactive_session():
            frappe.set_user(cls.manager)
            from ury.ury_pos import override

            override.set_my_pin(f"Pass-{cls.suffix}!", cls.manager_pin)
            frappe.set_user(cls.cashier)
            override.set_my_pin(f"Pass-{cls.suffix}!", cls.cashier_pin)

        frappe.set_user("Administrator")
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        if cls.prev_invoice_type:
            frappe.db.set_single_value("POS Settings", "invoice_type", cls.prev_invoice_type)
        for u in (cls.cashier, cls.cashier2, cls.manager):
            frappe.db.sql(
                "DELETE FROM `__Auth` WHERE doctype=%s AND name=%s AND fieldname=%s",
                ("POS Override PIN", u, "pin_hash"),
            )
            frappe.db.delete("POS Authorisation Log", {"approver": u})
            frappe.db.delete("POS Authorisation Log", {"requester": u})
        frappe.db.delete("POS Authorisation Log", {"pos_profile": cls.profile_name})
        frappe.db.delete("POS Drawer Event", {"pos_profile": cls.profile_name})
        frappe.db.delete("POS Privilege Rule", {"company": cls.company})
        frappe.db.delete("DefaultValue", {"defkey": f"pos_override_rules_seeded::{cls.company}"})
        frappe.db.commit()
        super().tearDownClass()

    @classmethod
    def _interactive_session(cls):
        from contextlib import nullcontext

        return nullcontext()

    @classmethod
    def _ensure_company(cls, name: str, abbr: str) -> str:
        doc = frappe.get_doc({
            "doctype": "Company",
            "company_name": name,
            "abbr": abbr,
            "default_currency": "UGX",
            "country": "Uganda",
        }).insert(ignore_permissions=True)
        cls.created.append(("Company", doc.name))
        return doc.name

    @classmethod
    def _make_account(cls, base_name: str, acct_type: str, root_type: str, parent_hint: str) -> str:
        parent = frappe.db.get_value(
            "Account",
            {"company": cls.company, "is_group": 1, "account_name": parent_hint},
            "name",
        ) or frappe.db.get_value(
            "Account",
            {"company": cls.company, "is_group": 1, "root_type": root_type},
            "name",
        )
        doc = frappe.get_doc({
            "doctype": "Account",
            "account_name": f"{base_name} {cls.suffix}",
            "company": cls.company,
            "parent_account": parent,
            "account_type": acct_type,
            "account_currency": "UGX",
        }).insert(ignore_permissions=True)
        cls.created.append(("Account", doc.name))
        return doc.name

    @classmethod
    def _make_user(cls, email: str, first_name: str, roles: list[str]) -> str:
        prev_import = getattr(frappe.flags, "in_import", False)
        frappe.flags.in_import = True
        try:
            doc = frappe.get_doc({
                "doctype": "User",
                "email": email,
                "first_name": first_name,
                "enabled": 1,
                "user_type": "System User",
                "send_welcome_email": 0,
                "roles": [{"role": r} for r in roles],
            }).insert(ignore_permissions=True)
        finally:
            frappe.flags.in_import = prev_import
        update_password(doc.name, f"Pass-{cls.suffix}!")
        cls.created.append(("User", doc.name))
        return doc.name

    @classmethod
    def _make_branch(cls, name: str, users: list[str]) -> str:
        doc = frappe.get_doc({
            "doctype": "Branch",
            "branch": name,
            "company": cls.company,
            "user": [{"user": u} for u in users],
        }).insert(ignore_permissions=True)
        cls.created.append(("Branch", doc.name))
        return doc.name

    @classmethod
    def _make_restaurant(cls, name: str) -> str:
        if not frappe.db.exists("URY Restaurant", name):
            doc = frappe.get_doc({
                "doctype": "URY Restaurant",
                "name": name,
                "branch": cls.branch,
                "company": cls.company,
                "default_customer": "",
            })
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            cls.created.append(("URY Restaurant", doc.name))
            return doc.name
        return name

    @classmethod
    def _make_mop(cls, name: str, mop_type: str, account: str, halo: float = 0) -> str:
        doc = frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": name,
            "type": mop_type,
            "enabled": 1,
            "custom_overtender_rule": "Refuse",
            "custom_halo_max_amount": halo,
            "accounts": [{"company": cls.company, "default_account": account}],
        }).insert(ignore_permissions=True)
        cls.created.append(("Mode of Payment", doc.name))
        return doc.name

    @classmethod
    def _make_customer(cls, name: str) -> str:
        cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "All Customer Groups"
        terr = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
        doc = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": name,
            "customer_type": "Individual",
            "customer_group": cg,
            "territory": terr,
        }).insert(ignore_permissions=True)
        cls.created.append(("Customer", doc.name))
        return doc.name

    @classmethod
    def _make_item(cls, code: str, rate: float) -> str:
        ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
        doc = frappe.get_doc({
            "doctype": "Item",
            "item_code": code,
            "item_name": f"Test Meal {cls.suffix}",
            "item_group": ig,
            "stock_uom": "Nos",
            "is_stock_item": 0,
            "standard_rate": rate,
        }).insert(ignore_permissions=True)
        cls.created.append(("Item", doc.name))
        return doc.name

    @classmethod
    def _make_pos_profile(cls, name: str) -> str:
        price_list = frappe.db.get_value("Price List", {"selling": 1}, "name") or "Standard Selling"
        doc = frappe.get_doc({
            "doctype": "POS Profile",
            "name": name,
            "company": cls.company,
            "branch": cls.branch,
            "restaurant": cls.restaurant,
            "currency": "UGX",
            "warehouse": cls.warehouse,
            "customer": cls.customer,
            "write_off_account": cls.paid_out_account,
            "write_off_cost_center": cls.cost_center,
            "income_account": cls.income_account,
            "cost_center": cls.cost_center,
            "selling_price_list": price_list,
            "disable_rounded_total": 1,
            "custom_enable_payment_without_printing": 1,
            "custom_enable_cash_movements": 1,
            "custom_cash_safe_account": cls.safe_account,
            "custom_paid_out_account": cls.paid_out_account,
            "custom_paid_in_account": cls.paid_in_account,
            "custom_cash_over_short_account": cls.over_short_account,
            "custom_tips_payable_account": cls.tips_account,
            "custom_paid_out_approval_threshold": 50000,
            "custom_paid_in_approval_threshold": 50000,
            "custom_cash_pull_threshold": 400000,
            "custom_over_short_tolerance": 5000,
            "custom_cash_movement_approver_role": "URY Manager",
            "applicable_for_users": [
                {"user": cls.cashier, "default": 1},
                {"user": cls.cashier2, "default": 0},
                {"user": cls.manager, "default": 0},
            ],
            "payments": [{"mode_of_payment": cls.cash_mop, "default": 1}],
        }).insert(ignore_permissions=True)
        cls.created.append(("POS Profile", doc.name))
        return doc.name

    def _open_shift(self, user: str, opening_cash: float = 100000) -> str:
        frappe.set_user("Administrator")
        doc = frappe.get_doc({
            "doctype": "POS Opening Entry",
            "period_start_date": frappe.utils.now_datetime(),
            "posting_date": nowdate(),
            "company": self.company,
            "pos_profile": self.profile_name,
            "user": user,
            "branch": self.branch,
            "restaurant": self.restaurant,
            "balance_details": [{"mode_of_payment": self.cash_mop, "opening_amount": opening_cash}],
        })
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        frappe.db.commit()
        self.created_openings.append(doc.name)
        return doc.name

    def _create_draft_pos_invoice(self, amount: float = 50000) -> str:
        frappe.set_user("Administrator")
        inv = frappe.get_doc({
            "doctype": "POS Invoice",
            "company": self.company,
            "pos_profile": self.profile_name,
            "branch": self.branch,
            "restaurant": self.restaurant,
            "customer": self.customer,
            "currency": "UGX",
            "is_pos": 1,
            "posting_date": nowdate(),
            "cashier": self.cashier,
            "waiter": self.cashier,
            "invoice_printed": 1,
            "items": [{
                "item_code": self.item,
                "qty": 1,
                "rate": amount,
                "income_account": self.income_account,
                "cost_center": self.cost_center,
            }],
            "payments": [{
                "mode_of_payment": self.cash_mop,
                "amount": 0,
            }],
        })
        inv.flags.ignore_permissions = True
        inv.insert(ignore_permissions=True)
        frappe.db.commit()
        self.created_invoices.append(inv.name)
        return inv.name

    def _close_any_open_shifts(self):
        frappe.set_user("Administrator")
        for name in frappe.get_all(
            "POS Opening Entry",
            filters={"pos_profile": self.profile_name, "docstatus": 1, "status": "Open"},
            pluck="name",
        ):
            frappe.db.set_value("POS Opening Entry", name, "status", "Closed")
        frappe.db.commit()

    def setUp(self):
        self._close_any_open_shifts()
        frappe.db.set_value("POS Profile", self.profile_name, "custom_blind_cash_count", 0)
        frappe.db.commit()

    def tearDown(self):
        self._close_any_open_shifts()
        frappe.set_user("Administrator")
        frappe.db.rollback()

    def test_live_tender_halo_override_with_r2_auth_pin_and_reason(self):
        """Live proof: over-HALO cash tender refused without PIN, approved via #147 R2 __Auth PIN + reason,
        stamps custom_halo_approver, logs immutable Consumed entry, and rejects token replay."""
        from ury.ury.doctype.ury_order.ury_order import make_invoice
        from ury.ury_pos import override

        self._open_shift(self.cashier, opening_cash=100000)
        inv_name = self._create_draft_pos_invoice(amount=50000)

        with self._interactive_session():
            frappe.set_user(self.cashier)
            # 1. Without Manager PIN: 5,000,000 > HALO 500,000 -> refused [POS-TENDER-HALO]
            with self.assertRaisesRegex(frappe.PermissionError, r"POS-TENDER-HALO"):
                make_invoice(
                    customer=self.customer,
                    payments=[{"mode_of_payment": self.cash_mop, "amount": 5000000}],
                    cashier=self.cashier,
                    pos_profile=self.profile_name,
                    owner=self.cashier,
                    invoice=inv_name,
                    submit_invoice=1,
                )

            # 2. Missing reason on #147 R2 override.authorise -> refused [POS-AUTH-REASON]
            claim_no_reason = {
                "action": "over_halo_tender",
                "invoice": inv_name,
                "pos_profile": self.profile_name,
                "tenders": [{"mode_of_payment": self.cash_mop, "amount": 5000000}],
            }
            with self.assertRaisesRegex(frappe.ValidationError, r"POS-AUTH-REASON"):
                override.authorise(
                    pos_profile=self.profile_name,
                    privilege="over_halo_tender",
                    approver=self.manager,
                    pin=self.manager_pin,
                    reference_doctype="POS Invoice",
                    reference_name=inv_name,
                    claim=claim_no_reason,
                )

            # 3. Real #147 R2 Manager PIN (__Auth) + active POS Reason -> mints single-use token
            reason = "Other: Confirmed large banquet cash"
            claim = {**claim_no_reason, "reason": reason}
            auth = override.authorise(
                pos_profile=self.profile_name,
                privilege="over_halo_tender",
                approver=self.manager,
                pin=self.manager_pin,
                reference_doctype="POS Invoice",
                reference_name=inv_name,
                claim=claim,
            )
            self.assertEqual(auth["approver"], self.manager)

            res = make_invoice(
                customer=self.customer,
                payments=[{"mode_of_payment": self.cash_mop, "amount": 5000000}],
                cashier=self.cashier,
                pos_profile=self.profile_name,
                owner=self.cashier,
                invoice=inv_name,
                submit_invoice=1,
                halo_approval_token=auth["token"],
                halo_approval_reason=reason,
            )
            self.assertEqual(res["status"], "Submitted")
            self.assertEqual(frappe.session.user, self.cashier)

            inv_doc = frappe.get_doc("POS Invoice", inv_name)
            self.assertEqual(inv_doc.docstatus, 1)
            self.assertEqual(inv_doc.custom_halo_approver, self.manager)

            # End request transaction so our REPEATABLE-READ snapshot sees the isolated
            # connection's committed Consumed POS Authorisation Log row (#147 R2 P1 #6).
            frappe.db.commit()
            consumed_logs = frappe.get_all(
                "POS Authorisation Log",
                filters={
                    "privilege": "over_halo_tender",
                    "reference_name": inv_name,
                    "outcome": "Consumed",
                    "approver": self.manager,
                    "requester": self.cashier,
                },
                pluck="name",
            )
            self.assertEqual(len(consumed_logs), 1)

            # 4. Token replay refused [POS-AUTH-EXPIRED]
            inv_name2 = self._create_draft_pos_invoice(amount=50000)
            frappe.set_user(self.cashier)
            with self.assertRaisesRegex(frappe.ValidationError, r"POS-AUTH-EXPIRED"):
                make_invoice(
                    customer=self.customer,
                    payments=[{"mode_of_payment": self.cash_mop, "amount": 5000000}],
                    cashier=self.cashier,
                    pos_profile=self.profile_name,
                    owner=self.cashier,
                    invoice=inv_name2,
                    submit_invoice=1,
                    halo_approval_token=auth["token"],
                    halo_approval_reason=reason,
                )

    def test_live_over_short_close_blind_safe_and_manager_pin_posts_over_short_je(self):
        """Live proof: blind close hides expected cash on variance refusal, over_short_close
        Manager PIN submits closing entry, stamps approver & reason, and posts balanced Over/Short JE."""
        from ury.ury.doctype.ury_order.ury_order import make_invoice
        from ury.ury_pos import override, till_controls

        frappe.db.set_value("POS Profile", self.profile_name, "custom_blind_cash_count", 1)
        frappe.db.commit()

        opening_name = self._open_shift(self.cashier, opening_cash=100000)
        inv_name = self._create_draft_pos_invoice(amount=50000)

        with self._interactive_session():
            frappe.set_user(self.cashier)
            make_invoice(
                customer=self.customer,
                payments=[{"mode_of_payment": self.cash_mop, "amount": 50000}],
                cashier=self.cashier,
                pos_profile=self.profile_name,
                owner=self.cashier,
                invoice=inv_name,
                submit_invoice=1,
            )

            # Expected cash = 150,000; counted = 130,000 (-20,000 > tolerance 5,000)
            counted = [{"mode_of_payment": self.cash_mop, "closing_amount": 130000}]
            with self.assertRaises(frappe.PermissionError) as ctx:
                till_controls.close_till_with_controls(
                    pos_profile=self.profile_name,
                    pos_opening_entry=opening_name,
                    counted_payments=counted,
                )
            err_msg = str(ctx.exception)
            self.assertIn("POS-CLOSE-VARIANCE-APPROVAL", err_msg)
            # Blind close MUST NOT leak expected cash (150,000) or variance (20,000)
            self.assertNotIn("150000", err_msg.replace(",", ""))
            self.assertNotIn("20000", err_msg.replace(",", ""))

            # Cashier self-approval refused [POS-AUTH-SELF]
            os_reason = "Other: Short 20000 UGX coin roll"
            os_claim = {
                "action": "over_short_close",
                "pos_profile": self.profile_name,
                "pos_opening_entry": opening_name,
                "counted_payments": counted,
                "reason": os_reason,
            }
            with self.assertRaisesRegex(frappe.PermissionError, r"POS-AUTH-SELF"):
                override.authorise(
                    pos_profile=self.profile_name,
                    privilege="over_short_close",
                    approver=self.cashier,
                    pin=self.cashier_pin,
                    reference_doctype="POS Opening Entry",
                    reference_name=opening_name,
                    claim=os_claim,
                )

            # Manager PIN approval via real #147 R2 __Auth
            auth = override.authorise(
                pos_profile=self.profile_name,
                privilege="over_short_close",
                approver=self.manager,
                pin=self.manager_pin,
                reference_doctype="POS Opening Entry",
                reference_name=opening_name,
                claim=os_claim,
            )
            res = till_controls.close_till_with_controls(
                pos_profile=self.profile_name,
                pos_opening_entry=opening_name,
                counted_payments=counted,
                over_short_reason=os_reason,
                over_short_approval_token=auth["token"],
            )
            self.assertEqual(res["docstatus"], 1)
            self.assertEqual(res["over_short_approver"], self.manager)
            self.assertEqual(res["over_short_reason"], os_reason)
            self.assertEqual(frappe.session.user, self.cashier)

            # Verify native Over/Short Journal Entry posted for 20,000 UGX short
            os_jes = frappe.get_all(
                "Journal Entry",
                filters={"custom_pos_closing_entry": res["name"], "docstatus": 1},
                fields=["name", "total_debit", "total_credit"],
            )
            self.assertEqual(len(os_jes), 1)
            self.assertEqual(flt(os_jes[0]["total_debit"]), 20000.0)
            self.assertEqual(flt(os_jes[0]["total_credit"]), 20000.0)

    def test_live_close_other_till_reopen_till_and_till_cash_pin_controls(self):
        """Live proof: close_other_till PIN, reopen_till PIN, paid_out/cash_loan/no_sale PIN
        all run end-to-end on real #147 R2 __Auth + MariaDB 11.8."""
        from ury.ury_pos import override, till_controls

        opening_name = self._open_shift(self.cashier, opening_cash=100000)

        with self._interactive_session():
            # 1. Paid Out above threshold (120,000 > 50,000) + Loan + No Sale on cashier's shift
            frappe.set_user(self.cashier)
            po_reason = "Other: Emergency ice delivery"
            po_claim = {
                "action": "paid_out",
                "movement_type": "Paid Out",
                "amount": 120000,
                "reason": po_reason,
                "reference": "RCP-LIVE-1",
                "paid_out_target": "expense",
                "pos_opening_entry": opening_name,
            }
            po_auth = override.authorise(
                pos_profile=self.profile_name,
                privilege="paid_out",
                approver=self.manager,
                pin=self.manager_pin,
                reference_doctype="POS Opening Entry",
                reference_name=opening_name,
                claim=po_claim,
            )
            po_res = till_controls.record_cash_movement(
                pos_profile=self.profile_name,
                movement_type="Paid Out",
                amount=120000,
                reason=po_reason,
                reference="RCP-LIVE-1",
                pos_opening_entry=opening_name,
                approval_token=po_auth["token"],
            )
            self.assertEqual(po_res["docstatus"], 1)
            self.assertEqual(po_res["approver"], self.manager)

            # Loan (cash_loan) with active POS Reason CASH-DROP
            loan_claim = {
                "action": "cash_loan",
                "movement_type": "Loan",
                "amount": 50000,
                "reason": "Safe drop / cash pickup",
                "reference": "",
                "paid_out_target": "expense",
                "pos_opening_entry": opening_name,
            }
            loan_auth = override.authorise(
                pos_profile=self.profile_name,
                privilege="cash_loan",
                approver=self.manager,
                pin=self.manager_pin,
                reference_doctype="POS Opening Entry",
                reference_name=opening_name,
                claim=loan_claim,
            )
            loan_res = till_controls.record_cash_movement(
                pos_profile=self.profile_name,
                movement_type="Loan",
                amount=50000,
                reason="Safe drop / cash pickup",
                pos_opening_entry=opening_name,
                approval_token=loan_auth["token"],
            )
            self.assertEqual(loan_res["docstatus"], 1)
            self.assertEqual(loan_res["approver"], self.manager)

            # No Sale (no_sale) with active POS Reason NOSALE-CHANGE
            ns_claim = {
                "action": "no_sale",
                "pos_profile": self.profile_name,
                "pos_opening_entry": opening_name,
                "reason": "Making change for guest",
                "note": "Coin break",
            }
            ns_auth = override.authorise(
                pos_profile=self.profile_name,
                privilege="no_sale",
                approver=self.manager,
                pin=self.manager_pin,
                reference_doctype="POS Opening Entry",
                reference_name=opening_name,
                claim=ns_claim,
            )
            ns_res = till_controls.trigger_no_sale(
                pos_profile=self.profile_name,
                reason="Making change for guest",
                note="Coin break",
                pos_opening_entry=opening_name,
                approval_token=ns_auth["token"],
            )
            self.assertEqual(ns_res["no_sale_count_for_shift"], 1)
            self.assertEqual(ns_res["approver"], self.manager)

            # 2. Close Another Till (cashier2 closes cashier1's shift)
            # Expected cash = 100,000 - 120,000 + 50,000 = 30,000
            frappe.set_user(self.cashier2)
            counted = [{"mode_of_payment": self.cash_mop, "closing_amount": 30000}]
            with self.assertRaisesRegex(frappe.PermissionError, r"POS-AUTH-REQUIRED"):
                till_controls.close_till_with_controls(
                    pos_profile=self.profile_name,
                    pos_opening_entry=opening_name,
                    counted_payments=counted,
                )

            other_reason = "Other: Cashier handover at end of shift"
            other_claim = {
                "action": "close_other_till",
                "pos_profile": self.profile_name,
                "pos_opening_entry": opening_name,
                "opening_user": self.cashier,
                "reason": other_reason,
            }
            other_auth = override.authorise(
                pos_profile=self.profile_name,
                privilege="close_other_till",
                approver=self.manager,
                pin=self.manager_pin,
                reference_doctype="POS Opening Entry",
                reference_name=opening_name,
                claim=other_claim,
            )
            close_res = till_controls.close_till_with_controls(
                pos_profile=self.profile_name,
                pos_opening_entry=opening_name,
                counted_payments=counted,
                close_other_approval_token=other_auth["token"],
                close_other_reason=other_reason,
            )
            self.assertEqual(close_res["docstatus"], 1)
            self.assertEqual(close_res["user"], self.cashier)
            self.assertEqual(close_res["close_approver"], self.manager)
            self.assertEqual(close_res["no_sale_count"], 1)
            self.assertEqual(frappe.session.user, self.cashier2)

            # 3. Reopen Till with Manager PIN restores shift to Open and restores cashier session
            reopen_reason = "Other: Missed late adjustment"
            reopen_claim = {
                "action": "reopen_till",
                "pos_profile": self.profile_name,
                "pos_closing_entry": close_res["name"],
                "pos_opening_entry": opening_name,
                "reason": reopen_reason,
            }
            reopen_auth = override.authorise(
                pos_profile=self.profile_name,
                privilege="reopen_till",
                approver=self.manager,
                pin=self.manager_pin,
                reference_doctype="POS Closing Entry",
                reference_name=close_res["name"],
                claim=reopen_claim,
            )
            reopen_res = till_controls.reopen_till(
                pos_closing_entry=close_res["name"],
                reason=reopen_reason,
                approval_token=reopen_auth["token"],
            )
            self.assertEqual(reopen_res["status"], "Open")
            self.assertEqual(reopen_res["approver"], self.manager)
            self.assertEqual(frappe.session.user, self.cashier2)
            self.assertEqual(frappe.db.get_value("POS Opening Entry", opening_name, "status"), "Open")
            self.assertEqual(frappe.db.get_value("POS Closing Entry", close_res["name"], "docstatus"), 2)
