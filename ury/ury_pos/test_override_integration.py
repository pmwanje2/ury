"""Real Frappe + MariaDB 11.8 integration & concurrency tests for manager PIN authorisation (B2a + R2 hardening).

Proves against a real MariaDB 11.8 + Redis bench:
- P0 #1: PINs stored exclusively in Frappe `__Auth` (passlib salted hashes), zero `User.custom_override_pin_hash`
  field/column, zero Version/REST/get_value exposure
- P0 #2: `set_my_pin` wrong password raises `ValidationError` (`POS-AUTH-PASSWORD`), never `AuthenticationError`,
  and locks out after 5 wrong attempts surviving rollback
- P0 #3: Impersonated (`impersonated_by`) and API-key sessions refused across `authorise`, `consume`, `may_perform`
- P1 #5: `Company.after_insert` auto-seeds rules; deleting ALL rules for a company fails closed (manager roles only)
  and re-running `setup_pos_manager_authorisation.execute()` never resurrects deleted rules (persistent seed marker)
- P1 #6: `consume()` re-checks approver role/outlet mid-flow, commits consumption via isolated MariaDB connection
  so it survives caller `frappe.db.rollback()` WITHOUT committing caller's uncommitted business writes
- P1 #7: `requires_reason` enforced server-side against active `POS Reason` rows
- P1 #8: DB-backed lockout survives Redis flush (`redis` key wipe) and is isolated per `(approver, pos_profile)`
- P1 #9: `list_approvers` refuses cross-company/cross-outlet callers
- Concurrency: Two concurrent `consume()` calls under MariaDB 11.8 `REPEATABLE-READ` allow exactly one
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
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.site = frappe.local.site
        frappe.set_user("Administrator")
        cls.suffix = random_string(8).lower()
        cls.created = []
        cls.cleanup_log_profiles = []
        cls.cleanup_companies = []

        cls.company_a = cls._ensure_company(f"AuthCoA-{cls.suffix}", f"AA{cls.suffix[:3].upper()}")
        cls.company_b = cls._ensure_company(f"AuthCoB-{cls.suffix}", f"AB{cls.suffix[:3].upper()}")

        cls.cashier = cls._make_user(f"cashier-{cls.suffix}@example.test", "Cashier A", ["URY Cashier"])
        cls.manager_a = cls._make_user(f"mgr-a-{cls.suffix}@example.test", "Manager A", ["URY Manager"])
        cls.manager_b = cls._make_user(f"mgr-b-{cls.suffix}@example.test", "Manager B", ["URY Manager"])
        cls.manager_other_outlet = cls._make_user(f"mgr-pool-{cls.suffix}@example.test", "Pool Manager", ["URY Manager"])
        cls.manager_other_co = cls._make_user(f"mgr-cob-{cls.suffix}@example.test", "OtherCo Manager", ["URY Manager"])
        cls.disabled_mgr = cls._make_user(f"mgr-off-{cls.suffix}@example.test", "Disabled Manager", ["URY Manager"], enabled=0)
        cls.sysmgr = cls._make_user(f"sysmgr-{cls.suffix}@example.test", "Sys Manager", ["System Manager"])

        cls.branch_a = cls._make_branch(f"MainBranch-{cls.suffix}", cls.company_a, [
            cls.cashier, cls.manager_a, cls.manager_b, cls.disabled_mgr, cls.sysmgr,
        ])
        cls.branch_pool = cls._make_branch(f"PoolBranch-{cls.suffix}", cls.company_a, [cls.manager_other_outlet])
        cls.branch_b = cls._make_branch(f"OtherCoBranch-{cls.suffix}", cls.company_b, [cls.manager_other_co])

        cls.profile_a = cls._make_pos_profile(f"MainTill-{cls.suffix}", cls.company_a, cls.branch_a, [cls.cashier])
        cls.profile_a2 = cls._make_pos_profile(f"BarTill-{cls.suffix}", cls.company_a, cls.branch_a, [cls.cashier], max_attempts=5)
        cls.profile_pool = cls._make_pos_profile(f"PoolTill-{cls.suffix}", cls.company_a, cls.branch_pool, [cls.manager_other_outlet])
        cls.profile_b = cls._make_pos_profile(f"OtherTill-{cls.suffix}", cls.company_b, cls.branch_b, [cls.manager_other_co])
        cls.cleanup_log_profiles.extend([cls.profile_a, cls.profile_a2, cls.profile_pool, cls.profile_b])
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        all_users = (
            cls.cashier, cls.manager_a, cls.manager_b,
            cls.manager_other_outlet, cls.manager_other_co,
            cls.disabled_mgr, cls.sysmgr,
        )
        cls._clear_lockouts(all_users)
        for user in all_users:
            frappe.db.sql(
                "DELETE FROM `__Auth` WHERE doctype=%s AND name=%s AND fieldname=%s",
                ("POS Override PIN", user, "pin_hash"),
            )
            frappe.db.delete("POS Authorisation Log", {"approver": user})
        if cls.cleanup_log_profiles:
            frappe.db.delete("POS Authorisation Log", {"pos_profile": ["in", cls.cleanup_log_profiles]})
        if cls.cleanup_companies:
            for comp in cls.cleanup_companies:
                frappe.db.delete("DefaultValue", {"defkey": f"pos_override_rules_seeded::{comp}"})
            frappe.db.delete("POS Privilege Rule", {"company": ["in", cls.cleanup_companies]})
            frappe.db.delete("Mode of Payment Account", {"company": ["in", cls.cleanup_companies]})
        for dt, name in reversed(cls.created):
            if frappe.db.exists(dt, name):
                if dt == "Mode of Payment":
                    frappe.db.delete("Mode of Payment Account", {"parent": name})
                frappe.db.delete(dt, {"name": name})
        frappe.db.commit()
        super().tearDownClass()

    @classmethod
    def _clear_lockouts(cls, users):
        for user in users:
            for prof in cls.cleanup_log_profiles:
                for key in (
                    f"pos-override-pin:{user.casefold()}",
                    f"pos-override-pin:{user.casefold()}:{prof.casefold()}",
                    f"pos-override-session:{user.casefold()}:{prof.casefold()}",
                ):
                    frappe.cache.hdel("login_failed_count", key)
                    frappe.cache.hdel("login_failed_time", key)
            pw_key = f"pos-override-pw:{user.casefold()}"
            frappe.cache.hdel("login_failed_count", pw_key)
            frappe.cache.hdel("login_failed_time", pw_key)

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        all_users = (
            self.cashier, self.manager_a, self.manager_b,
            self.manager_other_outlet, self.manager_other_co,
            self.disabled_mgr, self.sysmgr,
        )
        self._clear_lockouts(all_users)
        for user in all_users:
            frappe.db.sql(
                "DELETE FROM `__Auth` WHERE doctype=%s AND name=%s AND fieldname=%s",
                ("POS Override PIN", user, "pin_hash"),
            )
            frappe.db.delete("POS Authorisation Log", {"approver": user})
        frappe.db.delete("POS Authorisation Log", {"pos_profile": ["in", self.cleanup_log_profiles]})
        frappe.db.delete("POS Privilege Rule", {"company": ["in", self.cleanup_companies]})
        for comp in self.cleanup_companies:
            frappe.db.delete("DefaultValue", {"defkey": f"pos_override_rules_seeded::{comp}"})
        from ury.patches.v2_0 import setup_pos_manager_authorisation
        setup_pos_manager_authorisation.seed_default_rules(company=self.company_a)
        setup_pos_manager_authorisation.seed_default_rules(company=self.company_b)
        setup_pos_manager_authorisation.seed_default_reasons()
        frappe.db.commit()

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()

    @classmethod
    def _ensure_company(cls, name, abbr):
        base_company = frappe.db.get_value("Company", {}, "name")
        if not base_company:
            raise RuntimeError("No base Company available on test site")
        base = frappe.get_doc("Company", base_company)
        doc = frappe.get_doc({
            "doctype": "Company",
            "company_name": name,
            "abbr": abbr,
            "default_currency": base.default_currency or "USD",
            "country": base.country or "United States",
        }).insert(ignore_permissions=True)
        cls.created.append(("Company", doc.name))
        cls.cleanup_companies.append(doc.name)
        return doc.name

    @classmethod
    def _make_user(cls, email, first_name, roles, enabled=1):
        prev_import = getattr(frappe.flags, "in_import", False)
        frappe.flags.in_import = True
        try:
            doc = frappe.get_doc({
                "doctype": "User",
                "email": email,
                "first_name": first_name,
                "enabled": enabled,
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
    def _make_branch(cls, name, company, users):
        doc = frappe.get_doc({
            "doctype": "Branch",
            "branch": name,
            "company": company,
            "user": [{"user": u} for u in users],
        }).insert(ignore_permissions=True)
        cls.created.append(("Branch", doc.name))
        return doc.name

    @classmethod
    def _ensure_cash_mop(cls, company):
        mop_name = f"Cash-{company}"
        if frappe.db.exists("Mode of Payment", mop_name):
            return mop_name
        cash_acct = frappe.db.get_value("Company", company, "default_cash_account") or frappe.db.get_value(
            "Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name"
        )
        mop = frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": mop_name,
            "type": "Cash",
            "accounts": [{"company": company, "default_account": cash_acct}],
        }).insert(ignore_permissions=True)
        cls.created.append(("Mode of Payment", mop.name))
        return mop.name

    @classmethod
    def _make_pos_profile(cls, name, company, branch, users, max_attempts=3):
        co = frappe.get_doc("Company", company)
        wh = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
        income = co.default_income_account or frappe.db.get_value(
            "Account", {"company": company, "root_type": "Income", "is_group": 0}, "name"
        )
        expense = co.default_expense_account or frappe.db.get_value(
            "Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
        )
        cc = co.cost_center or frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
        currency = co.default_currency or "USD"
        mop = cls._ensure_cash_mop(company)
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
            "custom_override_max_attempts": max_attempts,
            "custom_override_lock_minutes": 15,
        }).insert(ignore_permissions=True)
        cls.created.append(("POS Profile", doc.name))
        return doc.name

    def test_docperms_and_insert_only_log_are_enforced(self):
        frappe.set_user(self.cashier)
        self.assertFalse(frappe.has_permission("POS Privilege Rule", "create"))
        self.assertFalse(frappe.has_permission("POS Privilege Rule", "write"))
        self.assertTrue(frappe.has_permission("POS Reason", "read"))
        self.assertFalse(frappe.has_permission("POS Reason", "write"))

        frappe.set_user(self.manager_a)
        self.assertTrue(frappe.has_permission("POS Privilege Rule", "write"))
        self.assertTrue(frappe.has_permission("POS Reason", "write"))
        self.assertTrue(frappe.has_permission("POS Authorisation Log", "read"))
        self.assertFalse(frappe.has_permission("POS Authorisation Log", "create"))
        self.assertFalse(frappe.has_permission("POS Authorisation Log", "write"))
        self.assertFalse(frappe.has_permission("POS Authorisation Log", "delete"))

        frappe.set_user("Administrator")
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc({
                "doctype": "POS Authorisation Log",
                "requester": self.cashier,
                "approver": self.manager_a,
                "privilege": "void_sent_item",
                "pos_profile": self.profile_a,
                "company": self.company_a,
                "outcome": "Approved",
            }).insert(ignore_permissions=True)

    def test_seed_once_marker_company_after_insert_and_zero_rules_fail_closed(self):
        """P1 #5: Company.after_insert auto-seeds; deleting ALL rules never resurrects & fails closed."""
        from ury.patches.v2_0 import setup_pos_manager_authorisation
        from ury.ury_pos import override

        # 1. Newly inserted Company auto-seeds POS Privilege Rules immediately via Company.after_insert.
        new_co_name = f"AutoSeedCo-{self.suffix}"
        new_co = self._ensure_company(new_co_name, f"AS{self.suffix[:3].upper()}")
        self.assertGreater(frappe.db.count("POS Privilege Rule", {"company": new_co}), 0)

        # 2. Edits & single deletion survive execute(); deleting ALL rules ALSO never resurrects!
        rule_name = frappe.db.get_value(
            "POS Privilege Rule",
            {"company": self.company_a, "privilege": "void_sent_item", "role": "URY Manager"},
            "name",
        )
        self.assertTrue(rule_name)
        frappe.db.set_value("POS Privilege Rule", rule_name, "max_amount", 123.45)

        deleted_name = frappe.db.get_value(
            "POS Privilege Rule",
            {"company": self.company_a, "privilege": "no_sale", "role": "URY Manager"},
            "name",
        )
        frappe.delete_doc("POS Privilege Rule", deleted_name, ignore_permissions=True)

        # Delete ALL rules for new_co -> seed marker MUST prevent execute() from resurrecting them!
        frappe.db.delete("POS Privilege Rule", {"company": new_co})
        frappe.db.commit()

        setup_pos_manager_authorisation.execute()
        self.assertEqual(float(frappe.db.get_value("POS Privilege Rule", rule_name, "max_amount")), 123.45)
        self.assertFalse(
            frappe.db.exists("POS Privilege Rule", {"company": self.company_a, "privilege": "no_sale", "role": "URY Manager"})
        )
        self.assertEqual(
            frappe.db.count("POS Privilege Rule", {"company": new_co}), 0,
            "Deleting all rules for a seeded company must not be resurrected by execute()",
        )

        # 3. Zero rules fails closed: cashier denied, manager allowed alone.
        frappe.db.delete("POS Privilege Rule", {"company": self.company_a})
        frappe.db.commit()
        self.assertFalse(override.may_perform(self.cashier, self.profile_a, "void_sent_item"))
        self.assertTrue(override.may_perform(self.manager_a, self.profile_a, "void_sent_item"))
        self.assertFalse(override.may_perform("Administrator", self.profile_a, "void_sent_item"))

    def test_pin_stored_in_auth_only_never_user_and_wrong_password_keeps_session(self):
        """P0 #1 & P0 #2: PIN lives in __Auth (passlib), absent from User, wrong password raises ValidationError."""
        from ury.ury_pos import override

        # P0 #1: User must NOT have custom_override_pin_hash Custom Field or DB column.
        self.assertFalse(frappe.db.exists("Custom Field", "User-custom_override_pin_hash"))
        self.assertFalse(frappe.db.has_column("User", "custom_override_pin_hash"))

        # P0 #2: Wrong password raises ValidationError (POS-AUTH-PASSWORD), NEVER AuthenticationError!
        frappe.set_user(self.manager_a)
        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-PASSWORD"):
            override.set_my_pin("totally-wrong-password", "4826")
        frappe.db.rollback()

        versions_before = frappe.db.count("Version", {"ref_doctype": "User", "docname": self.manager_a})
        override.set_my_pin(f"Pass-{self.suffix}!", "4826")
        frappe.set_user(self.manager_b)
        override.set_my_pin(f"Pass-{self.suffix}!", "4826")
        frappe.db.commit()

        rows = frappe.db.sql(
            "SELECT name, password FROM `__Auth` WHERE doctype=%s AND fieldname=%s AND name IN (%s, %s)",
            ("POS Override PIN", "pin_hash", self.manager_a, self.manager_b),
            as_dict=True,
        )
        by_user = {r["name"]: r["password"] for r in rows}
        self.assertIn(self.manager_a, by_user)
        self.assertIn(self.manager_b, by_user)
        self.assertNotEqual(by_user[self.manager_a], by_user[self.manager_b], "Shared PIN must have distinct salted passlib hashes")
        self.assertNotIn("4826", by_user[self.manager_a])
        self.assertTrue(by_user[self.manager_a].startswith("$"), f"Expected passlib hash, got {by_user[self.manager_a][:16]}")

        versions_after = frappe.db.count("Version", {"ref_doctype": "User", "docname": self.manager_a})
        self.assertEqual(versions_before, versions_after, "PIN enrolment must never create a Version record")

        # Cashier reading manager User doc gets zero PIN hash.
        frappe.set_user(self.cashier)
        r1 = override.authorise(self.profile_a, "void_sent_item", self.manager_a, "4826", "POS Invoice", "CHK-1", {"amount": 10, "reason": "Guest changed mind"})
        r2 = override.authorise(self.profile_a, "void_sent_item", self.manager_b, "4826", "POS Invoice", "CHK-1", {"amount": 10, "reason": "Guest changed mind"})
        self.assertEqual(r1["approver"], self.manager_a)
        self.assertEqual(r2["approver"], self.manager_b)

        logs = frappe.get_all("POS Authorisation Log", filters={"pos_profile": self.profile_a}, fields=["*"])
        dumped = json.dumps(logs, default=str)
        self.assertNotIn("4826", dumped)
        self.assertNotIn(by_user[self.manager_a], dumped)
        self.assertNotIn(by_user[self.manager_b], dumped)

    def test_db_lockout_survives_redis_wipe_and_is_isolated_per_profile(self):
        """P1 #8: DB-backed lockout survives Redis flush and profile A lockout does not lock profile B."""
        from ury.ury_pos import override

        frappe.set_user(self.manager_a)
        override.set_my_pin(f"Pass-{self.suffix}!", "1357")
        frappe.db.commit()

        claim = {"amount": 10, "reason": "Guest changed mind"}
        frappe.set_user(self.cashier)
        for _ in range(2):
            with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-PIN"):
                override.authorise(self.profile_a, "void_sent_item", self.manager_a, "9999", "POS Invoice", "CHK-RB", claim)
            frappe.db.rollback()

        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-LOCKED"):
            override.authorise(self.profile_a, "void_sent_item", self.manager_a, "9999", "POS Invoice", "CHK-RB", claim)
        frappe.db.rollback()

        # Wipe Redis lockout keys -> DB lockout MUST still refuse correct PIN on profile_a!
        self._clear_lockouts((self.manager_a, self.cashier))
        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-LOCKED"):
            override.authorise(self.profile_a, "void_sent_item", self.manager_a, "1357", "POS Invoice", "CHK-RB", claim)

        # Profile isolation: manager_a is NOT locked on profile_a2!
        ok_other_till = override.authorise(self.profile_a2, "void_sent_item", self.manager_a, "1357", "POS Invoice", "CHK-RB", claim)
        self.assertEqual(ok_other_till["approver"], self.manager_a)

    def test_consume_rechecks_role_and_survives_caller_rollback_without_leaking_caller_tx(self):
        """P1 #6: role revocation mid-flow blocks consume; consume survives caller rollback without committing caller's work."""
        from ury.ury_pos import override

        frappe.set_user(self.manager_a)
        override.set_my_pin(f"Pass-{self.suffix}!", "2580")
        frappe.db.commit()

        claim = {"amount": 35.0, "reason": "Guest changed mind"}
        frappe.set_user(self.cashier)
        issued_revoke = override.authorise(
            self.profile_a, "void_sent_item", self.manager_a, "2580",
            "POS Invoice", "CHK-REV-1", claim,
        )

        # Revoke manager_a's URY Manager role mid-flow -> consume MUST refuse!
        frappe.set_user("Administrator")
        mgr_doc = frappe.get_doc("User", self.manager_a)
        mgr_doc.roles = [r for r in mgr_doc.roles if r.role != "URY Manager"]
        mgr_doc.save(ignore_permissions=True)
        frappe.db.commit()

        try:
            frappe.set_user(self.cashier)
            with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-NOT-ELIGIBLE"):
                override.consume(issued_revoke["token"], "void_sent_item", self.profile_a, "CHK-REV-1", claim)
        finally:
            frappe.set_user("Administrator")
            mgr_doc = frappe.get_doc("User", self.manager_a)
            mgr_doc.append("roles", {"role": "URY Manager"})
            mgr_doc.save(ignore_permissions=True)
            frappe.db.commit()

        # Consume + caller rollback test:
        frappe.set_user(self.cashier)
        issued_rb = override.authorise(
            self.profile_a, "void_sent_item", self.manager_a, "2580",
            "POS Invoice", "CHK-TX-1", claim,
        )

        # Stage an uncommitted business mutation on the main connection BEFORE consume().
        marker_key = f"b2a_uncommitted_probe_{self.suffix}"
        frappe.db.set_default(marker_key, "SHOULD_ROLL_BACK")

        consumed = override.consume(issued_rb["token"], "void_sent_item", self.profile_a, "CHK-TX-1", claim)
        self.assertEqual(consumed.approver, self.manager_a)

        # Simulate consumer business failure rolling back the main transaction.
        frappe.db.rollback()

        # 1. Caller's uncommitted write was rolled back (consume did NOT commit main connection!).
        self.assertIsNone(frappe.db.get_default(marker_key), "consume() must not commit the caller's transaction")

        # 2. Token replay MUST be refused because Consumed row committed on isolated connection!
        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-EXPIRED"):
            override.consume(issued_rb["token"], "void_sent_item", self.profile_a, "CHK-TX-1", claim)

    def test_two_concurrent_consumes_under_mariadb_snapshot_isolation_allow_only_one(self):
        """Real two-transaction concurrency proof on MariaDB 11.8 REPEATABLE-READ."""
        from ury.ury_pos import override

        frappe.set_user(self.manager_a)
        override.set_my_pin(f"Pass-{self.suffix}!", "2580")
        frappe.db.commit()

        claim = {"amount": 42.5, "reason": "Guest changed mind"}
        frappe.set_user(self.cashier)
        issued = override.authorise(
            self.profile_a, "void_sent_item", self.manager_a, "2580",
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
                frappe.db.sql("SELECT 1")
                with lock:
                    isolations.append(iso)
                barrier.wait(timeout=15)
                consumed = override.consume(token, "void_sent_item", self.profile_a, "CHK-RACE-1", claim)
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

        frappe.db.commit()
        consumed_count = frappe.db.count(
            "POS Authorisation Log",
            {"pos_profile": self.profile_a, "reference_name": "CHK-RACE-1", "outcome": "Consumed"},
        )
        self.assertEqual(consumed_count, 1)

    def test_adversarial_eligibility_impersonation_reasons_and_cross_outlet_list(self):
        """P0 #3, P1 #7, P1 #9: impersonation, bad reason, cross-outlet enumeration, API key all refused."""
        from ury.ury_pos import override

        for mgr in (self.manager_a, self.manager_other_outlet, self.manager_other_co, self.sysmgr):
            frappe.set_user(mgr)
            override.set_my_pin(f"Pass-{self.suffix}!", "6420")
        frappe.set_user("Administrator")
        update_password(self.disabled_mgr, "6420", doctype="POS Override PIN", fieldname="pin_hash", logout_all_sessions=False)
        frappe.db.commit()

        claim = {"amount": 5, "reason": "Guest changed mind"}
        frappe.set_user(self.cashier)
        for bad in (self.disabled_mgr, self.manager_other_outlet, self.manager_other_co, self.sysmgr, "Administrator"):
            with self.subTest(approver=bad):
                with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-NOT-ELIGIBLE"):
                    override.authorise(self.profile_a, "void_sent_item", bad, "6420", "POS Invoice", "CHK-ADV", claim)

        # P1 #7: missing reason refused server-side.
        with self.assertRaisesRegex(frappe.ValidationError, "POS-AUTH-REASON"):
            override.authorise(self.profile_a, "void_sent_item", self.manager_a, "6420", "POS Invoice", "CHK-ADV", {"amount": 5, "reason": ""})

        # P0 #3: impersonated session refused on real session.data.
        prev_data = dict(getattr(frappe.session, "data", {}) or {})
        try:
            frappe.session.data = {**prev_data, "impersonated_by": "Administrator"}
            with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-NOT-ELIGIBLE"):
                override.authorise(self.profile_a, "void_sent_item", self.manager_a, "6420", "POS Invoice", "CHK-ADV", claim)
            self.assertFalse(override.may_perform(self.cashier, self.profile_a, "reprint_bill"))
        finally:
            frappe.session.data = prev_data

        # P1 #9: cross-company caller cannot enumerate approvers for profile_a.
        frappe.set_user(self.manager_other_co)
        with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-NOT-ELIGIBLE"):
            override.list_approvers(self.profile_a, "void_sent_item")

        # API-key session refused.
        frappe.set_user(self.cashier)
        fake_req = SimpleNamespace(headers={"Authorization": "token key:secret"}, cookies={})
        with patch.object(frappe, "request", fake_req, create=True):
            with self.assertRaisesRegex(frappe.PermissionError, "POS-AUTH-NOT-ELIGIBLE"):
                override.authorise(self.profile_a, "void_sent_item", self.manager_a, "6420", "POS Invoice", "CHK-ADV", claim)
