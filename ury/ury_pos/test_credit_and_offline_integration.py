"""Real MariaDB 11.8 integration tests for B5c+d:
- Cross-app Kamra `pos_override_pin_verifier` hook through URY `override.py`
- Token from another till refused (`POS-AUTH-CHANGED`)
- Hook missing or raising fails closed
- On-account `Customer Credit Limit` + `bypass_credit_limit_check=1` + `credit_controller` never silently bypass
- Two on-account checks racing under one limit (MariaDB 11.8 REPEATABLE READ snapshot isolation, real two transactions)
- Replay of an offline sale after `Mode of Payment.custom_allow_offline` was switched off
- Offline tender above `POS Profile.custom_offline_tender_limit` approved online with manager PIN
"""

from __future__ import annotations

import threading
from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt, now_datetime

from ury.tests.offline_pos_utils import make_offline_pos, send_order
from ury.ury.doctype.ury_order import ury_order as order
from ury.ury_pos import override


def _set_user_pin_or_kamra(user: str, pin: str) -> None:
    if frappe.db.table_exists("Cashier PIN"):
        if frappe.db.exists("Cashier PIN", user):
            doc = frappe.get_doc("Cashier PIN", user)
            doc.pin = pin
            doc.must_reset = 0
            doc.pin_attempts = 0
            doc.locked_until = None
            doc.save(ignore_permissions=True)
        else:
            frappe.get_doc({
                "doctype": "Cashier PIN",
                "user": user,
                "pin": pin,
                "must_reset": 0,
            }).insert(ignore_permissions=True)
        frappe.cache.delete_value(f"kamra_supervisor_pin_fails:{user.casefold()}")
    digest = override._pin_digest(user, pin)
    frappe.db.set_value("User", user, "custom_override_pin_hash", digest, update_modified=False)


def _ensure_user(email: str, roles: list[str], full_name: str) -> str:
    if not frappe.db.exists("User", email):
        frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": full_name,
            "full_name": full_name,
            "enabled": 1,
            "user_type": "System User",
            "send_welcome_email": 0,
            "roles": [{"role": r} for r in roles],
        }).insert(ignore_permissions=True)
    else:
        doc = frappe.get_doc("User", email)
        have = {r.role for r in doc.roles}
        missing = [r for r in roles if r not in have]
        if missing:
            doc.add_roles(*missing)
        doc.enabled = 1
        doc.save(ignore_permissions=True)
    frappe.clear_cache(user=email)
    return email


def _ensure_privilege_rule(company: str, privilege: str, role: str, *, may_authorise: int = 1):
    if not frappe.db.exists(
        "POS Privilege Rule",
        {"company": company, "privilege": privilege, "role": role, "pos_profile": ["in", ["", None]]},
    ):
        frappe.get_doc({
            "doctype": "POS Privilege Rule",
            "company": company,
            "pos_profile": "",
            "privilege": privilege,
            "role": role,
            "may_perform": 0,
            "may_authorise": may_authorise,
            "requires_reason": 1,
            "allow_self_approval": 0,
        }).insert(ignore_permissions=True)


class TestCreditAndOfflineIntegration(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user("Administrator")
        self.pos = make_offline_pos()
        self.company = self.pos.profile.company
        self.suffix = frappe.generate_hash(length=6)
        self.mgr = _ensure_user(
            f"b5c.mgr.{self.suffix}@test.local",
            ["URY Manager", "Hotel Admin", "Accounts Manager"],
            "Manager B5C",
        )
        # Assign manager to the till and branch
        profile = frappe.get_doc("POS Profile", self.pos.profile.name)
        profile.append("applicable_for_users", {"user": self.mgr})
        profile.allow_partial_payment = 1
        profile.custom_offline_tender_limit = 150
        profile.save(ignore_permissions=True)

        branch = frappe.get_doc("Branch", self.pos.branch.name)
        branch.append("user", {"user": self.mgr})
        branch.save(ignore_permissions=True)

        # Create a second till ("Till B") to test token-from-another-till refusal
        self.other_profile_name = f"Till B {self.suffix}"
        other_profile = frappe.copy_doc(profile)
        other_profile.name = self.other_profile_name
        other_profile.insert(ignore_permissions=True)
        self.other_profile = other_profile

        for priv in ("on_account_over_limit", "room_charge_over_limit", "offline_tender"):
            _ensure_privilege_rule(self.company, priv, "URY Manager", may_authorise=1)

        _set_user_pin_or_kamra(self.mgr, "4321")

        # Named credit customer with limit 100 and bypass_credit_limit_check=1
        self.credit_customer = f"B5C Corp {self.suffix}"
        cust = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": self.credit_customer,
            "customer_group": self.pos.customer.customer_group,
            "territory": self.pos.customer.territory,
            "credit_limits": [{
                "company": self.company,
                "credit_limit": 100,
                "bypass_credit_limit_check": 1,
            }],
        }).insert(ignore_permissions=True)
        self.credit_customer = cust.name

        # Airtel Money Mode of Payment (offline-enabled, requires reference)
        self.airtel_mop = f"Airtel Money {self.suffix}"
        cash_acc = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": "Cash", "company": self.company},
            "default_account",
        )
        mop = frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": self.airtel_mop,
            "type": "Bank",
            "enabled": 1,
            "custom_allow_offline": 1,
            "custom_offline_requires_reference": 1,
            "accounts": [{"company": self.company, "default_account": cash_acc}],
        }).insert(ignore_permissions=True)
        self.airtel_mop = mop.name
        profile.reload()
        profile.append("payments", {"mode_of_payment": self.airtel_mop})
        profile.save(ignore_permissions=True)

    def tearDown(self):
        frappe.set_user("Administrator")
        super().tearDown()

    def test_token_from_another_till_is_refused(self):
        frappe.set_user(self.pos.user.name)
        inv = send_order(
            self.pos,
            client_order_id=str(uuid4()),
            round_id="1",
            items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 2}],
        )
        # Mint token on Till B (other_profile), attempt to use on Till A!
        claim = {
            "customer": self.credit_customer,
            "company": self.company,
            "proposed": 200.0,
            "limit": 100.0,
            "currency": self.pos.profile.currency,
            "reason": "Cross-till test",
        }
        auth = override.authorise(
            self.other_profile.name,
            "on_account_over_limit",
            self.mgr,
            "4321",
            "POS Invoice",
            inv.name,
            claim,
        )
        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-CHANGED"):
            order.make_invoice(
                customer=self.credit_customer,
                payments=[{"mode_of_payment": "Cash", "amount": 0}],
                cashier=self.pos.user.name,
                pos_profile=self.pos.profile.name,
                owner=self.pos.user.name,
                invoice=inv.name,
                submit_invoice=1,
                on_account_approval=auth["token"],
                on_account_reason="Cross-till test",
            )

    def test_on_account_over_limit_never_silently_bypasses_and_logs_approval(self):
        frappe.set_user(self.pos.user.name)
        inv = send_order(
            self.pos,
            client_order_id=str(uuid4()),
            round_id="1",
            items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 2}],
        )
        # Customer has bypass_credit_limit_check=1 AND limit=100; bill is 200.
        with self.assertRaisesRegex(frappe.ValidationError, r"\[POS-CREDIT-004\]"):
            order.make_invoice(
                customer=self.credit_customer,
                payments=[{"mode_of_payment": "Cash", "amount": 0}],
                cashier=self.pos.user.name,
                pos_profile=self.pos.profile.name,
                owner=self.pos.user.name,
                invoice=inv.name,
                submit_invoice=1,
            )

        claim = {
            "customer": self.credit_customer,
            "company": self.company,
            "proposed": 200.0,
            "limit": 100.0,
            "currency": self.pos.profile.currency,
            "reason": "Approved corporate lunch",
        }
        auth = override.authorise(
            self.pos.profile.name,
            "on_account_over_limit",
            self.mgr,
            "4321",
            "POS Invoice",
            inv.name,
            claim,
        )
        out = order.make_invoice(
            customer=self.credit_customer,
            payments=[{"mode_of_payment": "Cash", "amount": 0}],
            cashier=self.pos.user.name,
            pos_profile=self.pos.profile.name,
            owner=self.pos.user.name,
            invoice=inv.name,
            submit_invoice=1,
            on_account_approval=auth["token"],
            on_account_reason="Approved corporate lunch",
        )
        self.assertEqual(out["status"], "Submitted")
        self.assertEqual(
            frappe.db.get_value("POS Invoice", inv.name, "custom_override_approver"),
            self.mgr,
        )
        self.assertTrue(
            frappe.db.exists(
                "POS Authorisation Log",
                {
                    "privilege": "on_account_over_limit",
                    "reference_name": inv.name,
                    "approver": self.mgr,
                    "outcome": "Consumed",
                },
            )
        )

    def test_offline_replay_airtel_money_refused_after_flag_switched_off_and_pin_over_limit(self):
        frappe.set_user(self.pos.user.name)
        cid = str(uuid4())
        inv = send_order(
            self.pos,
            client_order_id=cid,
            round_id="r1",
            captured_at=str(now_datetime()),
            offline_printed=1,
            items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 2}],
        )
        # Switch custom_allow_offline OFF before replay!
        frappe.set_user("Administrator")
        frappe.db.set_value("Mode of Payment", self.airtel_mop, "custom_allow_offline", 0)
        frappe.set_user(self.pos.user.name)
        with self.assertRaisesRegex(frappe.ValidationError, "POS-OFFLINE-TENDER-DISABLED"):
            order.make_invoice(
                customer=self.pos.customer.name,
                payments=[{"mode_of_payment": self.airtel_mop, "amount": 200, "reference_no": "AIR-777"}],
                cashier=self.pos.user.name,
                pos_profile=self.pos.profile.name,
                owner=self.pos.user.name,
                invoice=inv.name,
                client_order_id=cid,
                round_id="r1",
                offline_replay=1,
            )

        # Turn flag back ON; amount 200 exceeds custom_offline_tender_limit=150 -> requires offline_tender PIN!
        frappe.set_user("Administrator")
        frappe.db.set_value("Mode of Payment", self.airtel_mop, "custom_allow_offline", 1)
        frappe.set_user(self.pos.user.name)
        with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-REQUIRED"):
            order.make_invoice(
                customer=self.pos.customer.name,
                payments=[{"mode_of_payment": self.airtel_mop, "amount": 200, "reference_no": "AIR-777"}],
                cashier=self.pos.user.name,
                pos_profile=self.pos.profile.name,
                owner=self.pos.user.name,
                invoice=inv.name,
                client_order_id=cid,
                round_id="r1",
                offline_replay=1,
            )

        claim = {
            "amount": 200.0,
            "limit": 150.0,
            "modes": [self.airtel_mop],
            "reason": "Network outage approved",
        }
        auth = override.authorise(
            self.pos.profile.name,
            "offline_tender",
            self.mgr,
            "4321",
            "POS Invoice",
            inv.name,
            claim,
        )
        out = order.make_invoice(
            customer=self.pos.customer.name,
            payments=[{"mode_of_payment": self.airtel_mop, "amount": 200, "reference_no": "AIR-777"}],
            cashier=self.pos.user.name,
            pos_profile=self.pos.profile.name,
            owner=self.pos.user.name,
            invoice=inv.name,
            client_order_id=cid,
            round_id="r1",
            offline_replay=1,
            offline_approval=auth["token"],
            offline_reason="Network outage approved",
        )
        self.assertEqual(out["status"], "Submitted")

    def test_two_on_account_checks_racing_under_one_limit_mariadb_snapshot_isolation(self):
        # Customer limit = 150; two open checks of 100 each race in two real MariaDB 11.8 transactions.
        frappe.set_user("Administrator")
        frappe.db.set_value(
            "Customer Credit Limit",
            {"parent": self.credit_customer, "parenttype": "Customer", "company": self.company},
            "credit_limit",
            150,
        )
        frappe.set_user(self.pos.user.name)
        inv1 = send_order(
            self.pos,
            client_order_id=str(uuid4()),
            round_id="1",
            items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 1}],
        )
        inv2 = send_order(
            self.pos,
            client_order_id=str(uuid4()),
            round_id="1",
            items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 1}],
        )
        frappe.db.commit()

        site = frappe.local.site
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, str]] = []
        lock = threading.Lock()

        def worker(inv_name: str) -> None:
            frappe.init(site=site)
            frappe.connect()
            try:
                frappe.set_user(self.pos.user.name)
                # Force BOTH transactions to open their REPEATABLE READ snapshot first!
                frappe.db.sql("SELECT name FROM `tabCustomer` WHERE name=%s", (self.credit_customer,))
                barrier.wait(timeout=15)
                res = order.make_invoice(
                    customer=self.credit_customer,
                    payments=[{"mode_of_payment": "Cash", "amount": 0}],
                    cashier=self.pos.user.name,
                    pos_profile=self.pos.profile.name,
                    owner=self.pos.user.name,
                    invoice=inv_name,
                    submit_invoice=1,
                )
                frappe.db.commit()
                with lock:
                    outcomes.append(("ok", res["name"]))
            except Exception as exc:
                frappe.db.rollback()
                with lock:
                    outcomes.append(("err", str(exc)))
            finally:
                frappe.destroy()

        t1 = threading.Thread(target=worker, args=(inv1.name,))
        t2 = threading.Thread(target=worker, args=(inv2.name,))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        oks = [o for o in outcomes if o[0] == "ok"]
        errs = [o for o in outcomes if o[0] == "err"]
        self.assertEqual(len(outcomes), 2, f"Unexpected race outcomes: {outcomes}")
        self.assertEqual(len(oks), 1, f"Expected exactly 1 winner under 150 limit, got {outcomes}")
        self.assertEqual(len(errs), 1, f"Expected exactly 1 refusal under 150 limit, got {outcomes}")
        self.assertIn("POS-CREDIT-004", errs[0][1])

        frappe.db.commit()
        submitted_count = frappe.db.count(
            "POS Invoice",
            {"name": ["in", [inv1.name, inv2.name]], "docstatus": 1},
        )
        self.assertEqual(submitted_count, 1)
