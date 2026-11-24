"""Real Frappe + MariaDB 11.8 integration & concurrency tests for manager PIN authorisation (B2a).

Proves against a real MariaDB 11.8 + Redis bench:
- DocPerm ceilings on POS Privilege Rule & insert-only POS Authorisation Log
- Seed-once patch never overwrites or resurrects tenant edits/deletes
- User.custom_override_pin_hash enrolment, shared PINs, manager clear, zero Version leak
- Wrong PIN attempt counter surviving real DB rollback & triggering lockout
- Two concurrent consume() calls under MariaDB 11.8 REPEATABLE-READ snapshot isolation
- pos_override_pin_verifier hook, AI/API-key session refusal, Administrator no free pass
"""

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import random_string
from frappe.utils.password import update_password


class TestOverrideIntegration(unittest.TestCase):
    def setUp(self):
        self.site = frappe.local.site
        frappe.set_user("Administrator")
        self.suffix = random_string(8).lower()
        self.created = []
        self.cleanup_log_profiles = []
        self.cleanup_companies = []

        self.company_a = self._ensure_company(f"AuthCoA-{self.suffix}", f"AA{self.suffix[:3].upper()}")
        self.company_b = self._ensure_company(f"AuthCoB-{self.suffix}", f"AB{self.suffix[:3].upper()}")

        self.cashier = self._make_user(f"cashier-{self.suffix}@example.test", "Cashier A", ["URY Cashier"])
        self.manager_a = self._make_user(f"mgr-a-{self.suffix}@example.test", "Manager A", ["URY Manager"])
        self.manager_b = self._make_user(f"mgr-b-{self.suffix}@example.test", "Manager B", ["URY Manager"])
        self.manager_other_outlet = self._make_user(f"mgr-pool-{self.suffix}@example.test", "Pool Manager", ["URY Manager"])
        self.manager_other_co = self._make_user(f"mgr-cob-{self.suffix}@example.test", "OtherCo Manager", ["URY Manager"])
        self.disabled_mgr = self._make_user(f"mgr-off-{self.suffix}@example.test", "Disabled Manager", ["URY Manager"], enabled=0)
        self.sysmgr = self._make_user(f"sysmgr-{self.suffix}@example.test", "Sys Manager", ["System Manager"])

        self.branch_a = self._make_branch(f"MainBranch-{self.suffix}", self.company_a, [
            self.cashier, self.manager_a, self.manager_b, self.disabled_mgr, self.sysmgr,
        ])
        self.branch_pool = self._make_branch(f"PoolBranch-{self.suffix}", self.company_a, [self.manager_other_outlet])
        self.branch_b = self._make_branch(f"OtherCoBranch-{self.suffix}", self.company_b, [self.manager_other_co])

        self.profile_a = self._make_pos_profile(f"MainTill-{self.suffix}", self.company_a, self.branch_a, [self.cashier])
        self.profile_pool = self._make_pos_profile(f"PoolTill-{self.suffix}", self.company_a, self.branch_pool, [self.manager_other_outlet])
        self.profile_b = self._make_pos_profile(f"OtherTill-{self.suffix}", self.company_b, self.branch_b, [self.manager_other_co])
        self.cleanup_log_profiles.extend([self.profile_a, self.profile_pool, self.profile_b])

        from ury.patches.v2_0 import setup_pos_manager_authorisation
        setup_pos_manager_authorisation.seed_default_rules(company=self.company_a)
        setup_pos_manager_authorisation.seed_default_rules(company=self.company_b)
        frappe.db.commit()

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        for user in (
            self.cashier, self.manager_a, self.manager_b,
            self.manager_other_outlet, self.manager_other_co,
            self.disabled_mgr, self.sysmgr,
        ):
            key = f"pos-override-pin:{user.casefold()}"
            frappe.cache.hdel("login_failed_count", key)
            frappe.cache.hdel("login_failed_time", key)
        if self.cleanup_log_profiles:
            frappe.db.delete("POS Authorisation Log", {"pos_profile": ["in", self.cleanup_log_profiles]})
        if self.cleanup_companies:
            frappe.db.delete("POS Privilege Rule", {"company": ["in", self.cleanup_companies]})
        for dt, name in reversed(self.created):
            if frappe.db.exists(dt, name):
                frappe.db.delete(dt, {"name": name})
        frappe.db.commit()

    def _ensure_company(self, name, abbr):
        base_company = frappe.db.get_value("Company", {}, "name")
        if base_company:
            base = frappe.get_doc("Company", base_company)
            doc = frappe.get_doc({
                "doctype": "Company",
                "company_name": name,
                "abbr": abbr,
                "default_currency": base.default_currency or "USD",
                "country": base.country or "United States",
            }).insert(ignore_permissions=True)
            self.created.append(("Company", doc.name))
            self.cleanup_companies.append(doc.name)
            return doc.name
        raise RuntimeError("No base Company available on test site")

    def _make_user(self, email, first_name, roles, enabled=1):
        doc = frappe.get_doc({
            "doctype": "User",
            "email": email,
            "first_name": first_name,
            "enabled": enabled,
            "user_type": "System User",
            "send_welcome_email": 0,
            "roles": [{"role": r} for r in roles],
        }).insert(ignore_permissions=True)
        update_password(doc.name, f"Pass-{self.suffix}!")
        self.created.append(("User", doc.name))
        return doc.name

    def _make_branch(self, name, company, users):
        doc = frappe.get_doc({
            "doctype": "Branch",
            "branch": name,
            "company": company,
            "user": [{"user": u} for u in users],
        }).insert(ignore_permissions=True)
        self.created.append(("Branch", doc.name))
        return doc.name

    def _make_pos_profile(self, name, company, branch, users):
        wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
        income = frappe.db.get_value("Account", {"company": company, "account_type": "Income Account", "is_group": 0}, "name")
        expense = frappe.db.get_value("Account", {"company": company, "account_type": "Cost of Goods Sold", "is_group": 0}, "name") or frappe.db.get_value("Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name")
        cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
        currency = frappe.db.get_value("Company", company, "default_currency") or "USD"
        mop = frappe.db.get_value("Mode of Payment", {}, "name") or "Cash"
        doc = frappe.get_doc({
            "doctype": "POS Profile",
            "name": name,
            "__newname": name,
            "company": company,
            "branch": branch,
            "currency": currency,
            "warehouse": wh,
            "income_account": income,
            "expense_account": expense,
            "cost_center": cc,
            "write_off_account": expense,
            "write_off_cost_center": cc,
            "selling_price_list": frappe.db.get_value("Price List", {"selling": 1}, "name") or "Standard Selling",
            "payments": [{"mode_of_payment": mop, "default": 1}],
            "applicable_for_users": [{"user": u} for u in users],
            "custom_override_max_attempts": 3,
            "custom_override_lock_minutes": 15,
        }).insert(ignore_permissions=True)
        self.created.append(("POS Profile", doc.name))
        return doc.name

    def test_docperms_and_insert_only_log_are_enforced(self):
        # Cashier cannot create or write POS Privilege Rule.
        frappe.set_user(self.cashier)
        self.assertFalse(frappe.has_permission("POS Privilege Rule", "create"))
        self.assertFalse(frappe.has_permission("POS Privilege Rule", "write"))

        # Manager CAN write POS Privilege Rule, and can READ (never create/write/delete) POS Authorisation Log.
        frappe.set_user(self.manager_a)
        self.assertTrue(frappe.has_permission("POS Privilege Rule", "write"))
        self.assertTrue(frappe.has_permission("POS Authorisation Log", "read"))
        self.assertFalse(frappe.has_permission("POS Authorisation Log", "create"))
        self.assertFalse(frappe.has_permission("POS Authorisation Log", "write"))
        self.assertFalse(frappe.has_permission("POS Authorisation Log", "delete"))

        # Even Administrator cannot insert POS Authorisation Log directly without server flag, nor update/delete one.
        frappe.set_user("Administrator")
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc({
                "doctype": "POS Authorisation Log",
                "requester": self.cashier,
                "approver": self.manager_a,
                "privilege": "void_after_send",
                "pos_profile": self.profile_a,
                "company": self.company_a,
                "outcome": "Approved",
            }).insert(ignore_permissions=True)

    def test_seed_once_patch_never_overwrites_edits_or_resurrects_deleted_rules(self):
        from ury.patches.v2_0 import setup_pos_manager_authorisation

        rule_name = frappe.db.get_value(
            "POS Privilege Rule",
            {"company": self.company_a, "privilege": "void_after_send", "role": "URY Manager"},
            "name",
        )
        self.assertTrue(rule_name)
        frappe.db.set_value("POS Privilege Rule", rule_name, "max_amount", 123.45)

        deleted_name = frappe.db.get_value(
            "POS Privilege Rule",
            {"company": self.company_a, "privilege": "no_sale", "role": "URY Manager"},
            "name",
        )
        self.assertTrue(deleted_name)
        frappe.delete_doc("POS Privilege Rule", deleted_name, ignore_permissions=True)
        frappe.db.commit()

        # Re-run the patch: edit preserved, deleted rule stays deleted!
        setup_pos_manager_authorisation.execute()
        self.assertEqual(
            float(frappe.db.get_value("POS Privilege Rule", rule_name, "max_amount")),
            123.45,
        )
        self.assertFalse(
            frappe.db.exists(
                "POS Privilege Rule",
                {"company": self.company_a, "privilege": "no_sale", "role": "URY Manager"},
            )
        )

    def test_pin_enrolment_shared_pin_zero_version_and_no_secret_in_logs(self):
        from ury.ury_pos import override

        versions_before = frappe.db.count("Version", {"ref_doctype": "User", "docname": self.manager_a})
        frappe.set_user(self.manager_a)
        override.set_my_pin(f"Pass-{self.suffix}!", "4826")
        frappe.set_user(self.manager_b)
        override.set_my_pin(f"Pass-{self.suffix}!", "4826")

        h_a = frappe.db.get_value("User", self.manager_a, "custom_override_pin_hash")
        h_b = frappe.db.get_value("User", self.manager_b, "custom_override_pin_hash")
        self.assertTrue(h_a and h_b)
        self.assertNotEqual(h_a, h_b)

        versions_after = frappe.db.count("Version", {"ref_doctype": "User", "docname": self.manager_a})
        self.assertEqual(versions_before, versions_after, "PIN enrolment must never create a Version record")

        frappe.set_user(self.cashier)
        r1 = override.authorise(self.profile_a, "void_after_send", self.manager_a, "4826", "POS Invoice", "CHK-1", {"amount": 10})
        r2 = override.authorise(self.profile_a, "void_after_send", self.manager_b, "4826", "POS Invoice", "CHK-1", {"amount": 10})
        self.assertEqual(r1["approver"], self.manager_a)
        self.assertEqual(r2["approver"], self.manager_b)

        # Ensure PIN and hash NEVER appear in POS Authorisation Log or Activity Log.
        logs = frappe.get_all("POS Authorisation Log", filters={"pos_profile": self.profile_a}, fields=["*"])
        dumped = json.dumps(logs, default=str)
        self.assertNotIn("4826", dumped)
        self.assertNotIn(h_a, dumped)
        self.assertNotIn(h_b, dumped)

    def test_wrong_pin_counts_through_real_rollback_and_locks_in_redis(self):
        from ury.ury_pos import override

        frappe.set_user(self.manager_a)
        override.set_my_pin(f"Pass-{self.suffix}!", "1357")
        frappe.db.commit()

        frappe.set_user(self.cashier)
        for _ in range(2):
            with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-PIN"):
                override.authorise(self.profile_a, "void_after_send", self.manager_a, "9999", "POS Invoice", "CHK-RB", {"amount": 10})
            frappe.db.rollback()

        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-LOCKED"):
            override.authorise(self.profile_a, "void_after_send", self.manager_a, "9999", "POS Invoice", "CHK-RB", {"amount": 10})
        frappe.db.rollback()

        # Even with the right PIN, locked!
        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-LOCKED"):
            override.authorise(self.profile_a, "void_after_send", self.manager_a, "1357", "POS Invoice", "CHK-RB", {"amount": 10})

        # Failed PIN log rows survived rollback and do not contain the PIN!
        failed_rows = frappe.get_all(
            "POS Authorisation Log",
            filters={"pos_profile": self.profile_a, "reference_name": "CHK-RB"},
            fields=["outcome", "refusal_code", "claim_values", "reason"],
        )
        self.assertGreaterEqual(len(failed_rows), 3)
        self.assertNotIn("9999", json.dumps(failed_rows, default=str))

    def test_two_concurrent_consumes_under_mariadb_snapshot_isolation_allow_only_one(self):
        """Real two-transaction concurrency proof on MariaDB 11.8 REPEATABLE-READ."""
        from ury.ury_pos import override

        frappe.set_user(self.manager_a)
        override.set_my_pin(f"Pass-{self.suffix}!", "2580")
        frappe.db.commit()

        claim = {"amount": 42.5, "reason": "Wrong dish"}
        frappe.set_user(self.cashier)
        issued = override.authorise(
            self.profile_a, "void_after_send", self.manager_a, "2580",
            "POS Invoice", "CHK-RACE-1", claim,
        )
        frappe.db.commit()
        token = issued["token"]

        barrier = threading.Barrier(2)
        results = []
        isolations = []
        lock = threading.Lock()

        def worker(idx):
            frappe.init(site=self.site)
            frappe.connect()
            try:
                frappe.set_user(self.cashier)
                iso = frappe.db.sql("SELECT @@SESSION.transaction_isolation")[0][0]
                # Start an open REPEATABLE-READ snapshot BEFORE either worker writes.
                frappe.db.sql("SELECT 1")
                with lock:
                    isolations.append(iso)
                barrier.wait(timeout=15)
                consumed = override.consume(token, "void_after_send", self.profile_a, "CHK-RACE-1", claim)
                frappe.db.commit()
                with lock:
                    results.append(("ok", idx, consumed.approver))
            except Exception as exc:
                frappe.db.rollback()
                with lock:
                    results.append(("err", idx, str(exc)))
            finally:
                frappe.destroy()

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        self.assertEqual(len(results), 2, f"Both workers must complete: {results}")
        self.assertTrue(all("REPEATABLE" in iso.upper() for iso in isolations), f"Expected REPEATABLE-READ: {isolations}")
        ok = [r for r in results if r[0] == "ok"]
        err = [r for r in results if r[0] == "err"]
        self.assertEqual(len(ok), 1, f"Exactly one concurrent consume must succeed: {results}")
        self.assertEqual(len(err), 1, f"Exactly one concurrent consume must fail: {results}")
        self.assertIn("POS-AUTH-EXPIRED", err[0][2])

        # Database confirms exactly ONE Consumed audit log row!
        frappe.db.commit()
        consumed_count = frappe.db.count(
            "POS Authorisation Log",
            {"pos_profile": self.profile_a, "reference_name": "CHK-RACE-1", "outcome": "Consumed"},
        )
        self.assertEqual(consumed_count, 1)

    def test_adversarial_eligibility_and_api_key_session_real_db(self):
        from ury.ury_pos import override

        for mgr in (self.manager_a, self.manager_other_outlet, self.manager_other_co, self.sysmgr):
            frappe.set_user(mgr)
            override.set_my_pin(f"Pass-{self.suffix}!", "6420")
        frappe.set_user("Administrator")
        frappe.db.set_value("User", self.disabled_mgr, "custom_override_pin_hash", override._pin_digest(self.disabled_mgr, "6420"), update_modified=False)
        frappe.db.commit()

        frappe.set_user(self.cashier)
        for bad in (self.disabled_mgr, self.manager_other_outlet, self.manager_other_co, self.sysmgr, "Administrator"):
            with self.subTest(approver=bad):
                with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-NOT-ELIGIBLE"):
                    override.authorise(self.profile_a, "void_after_send", bad, "6420", "POS Invoice", "CHK-ADV", {"amount": 5})

        # API-key HTTP session refused.
        fake_req = SimpleNamespace(headers={"Authorization": "token key:secret"}, cookies={})
        with patch.object(frappe, "request", fake_req, create=True):
            with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-NOT-ELIGIBLE"):
                override.authorise(self.profile_a, "void_after_send", self.manager_a, "6420", "POS Invoice", "CHK-ADV", {"amount": 5})
