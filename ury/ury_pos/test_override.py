"""Site-less unit tests for POS manager PIN authorisation (Simphony/SAP core, B2a).

Tests the fixed shared contract in `ury/ury_pos/override.py`, the
`POS Privilege Rule` and `POS Authorisation Log` DocTypes, `User.custom_override_pin_hash`
and POS Profile lockout fields, seed-once patch behavior, and all adversarial cases.
"""

import hashlib
import hmac
import importlib.util
import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
VENDOR_RE = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala)\b", re.I)

EXPECTED_PRIVILEGES = {
    "discount",
    "complimentary",
    "void_after_send",
    "cancel_check",
    "return",
    "reopen_check",
    "price_override",
    "transfer_check",
    "room_charge_over_limit",
    "reprint",
    "no_sale",
    "cash_movement",
    "service_charge_waive",
    "tax_exempt",
    "tip_adjust",
}


class Denied(Exception):
    """Stands in for frappe.PermissionError."""


class Invalid(Exception):
    """Stands in for frappe.ValidationError."""


class UniqueCollision(Invalid):
    """Stands in for frappe.UniqueValidationError."""


class AuthenticationFailed(Exception):
    """Stands in for frappe.AuthenticationError."""


class Doc(dict):
    __getattr__ = dict.get
    __setattr__ = dict.__setitem__


class FakeDocRecord(Doc):
    def __init__(self, store, doctype, data):
        super().__init__(data)
        self.doctype = doctype
        self._store = store
        self.flags = Doc()

    def insert(self, ignore_permissions=False, **kwargs):
        self.flags.ignore_permissions = ignore_permissions
        self._store.insert_doc(self)
        return self


class InMemoryStore:
    """In-memory stand-in for MariaDB tables used by override.py."""

    def __init__(self, case):
        self.case = case
        self.users = {}
        self.user_roles = {}
        self.user_permissions = {}
        self.profiles = {}
        self.branch_users = set()  # (branch, user)
        self.employees = {}  # user -> company
        self.privilege_rules = []  # list[Doc]
        self.approval_limits = {}  # company -> Doc
        self.invoices = {}  # name -> Doc
        self.auth_logs = []  # list[Doc]
        self.consumed_hashes = set()
        self.passwords = {}  # user -> plain password
        self.has_approval_limit_doctype = True
        self.pending_logs = []
        self.committed_logs = []
        self.force_consume_collision = False

    def insert_doc(self, doc):
        if doc.doctype == "POS Authorisation Log":
            if doc.get("consumed_token_hash"):
                if self.force_consume_collision or doc["consumed_token_hash"] in self.consumed_hashes:
                    raise UniqueCollision("Duplicate consumed_token_hash")
                self.consumed_hashes.add(doc["consumed_token_hash"])
            row = Doc(doc)
            if not row.get("name"):
                row["name"] = f"POS-AUTH-LOG-{len(self.auth_logs) + 1:04d}"
            self.auth_logs.append(row)
            self.pending_logs.append(row)
        elif doc.doctype == "POS Privilege Rule":
            row = Doc(doc)
            if not row.get("name"):
                row["name"] = f"PPR-{len(self.privilege_rules) + 1:04d}"
            self.privilege_rules.append(row)

    def commit(self):
        self.committed_logs.extend(self.pending_logs)
        self.pending_logs.clear()

    def rollback(self):
        # Roll back uncommitted log rows, leaving Redis cache untouched.
        for row in self.pending_logs:
            if row in self.auth_logs:
                self.auth_logs.remove(row)
            if row.get("consumed_token_hash"):
                self.consumed_hashes.discard(row["consumed_token_hash"])
        self.pending_logs.clear()


class TestOverrideContract(unittest.TestCase):
    def setUp(self):
        self.clock = datetime(2026, 11, 24, 9, 0, 0)
        self.counts = {}
        self.tracker_options = []
        self.hooks = {}
        self.store = InMemoryStore(self)
        case = self

        class Tracker:
            def __init__(self, key, max_consecutive_login_attempts=3, lock_interval=300):
                self.key = key
                self.maximum = max_consecutive_login_attempts
                self.interval = lock_interval
                case.tracker_options.append((key, self.maximum, self.interval))

            def is_user_allowed(self):
                start, count = case.counts.get(self.key, (case.clock, 0))
                return not (start + timedelta(seconds=self.interval) > case.clock and count > self.maximum)

            def add_failure_attempt(self):
                start, count = case.counts.get(self.key, (case.clock, 0))
                if start + timedelta(seconds=self.interval) <= case.clock:
                    start, count = case.clock, 0
                case.counts[self.key] = (start, count + 1)

            def add_success_attempt(self):
                case.counts.pop(self.key, None)

        def throw(message, exc=None, *args, **kwargs):
            raise (exc or Invalid)(message)

        self.frappe = MagicMock()
        self.frappe._dict = Doc
        self.frappe.whitelist.side_effect = lambda *a, **kw: (lambda fn: fn) if not a else a[0]
        self.frappe.throw.side_effect = throw
        self.frappe._.side_effect = lambda text, *a, **kw: text
        self.frappe.PermissionError = Denied
        self.frappe.ValidationError = Invalid
        self.frappe.UniqueValidationError = UniqueCollision
        self.frappe.AuthenticationError = AuthenticationFailed
        self.frappe.session = Doc(user="cashier@example.test", sid="sess-cashier-1")
        self.frappe.flags = Doc()
        self.frappe.conf = {}
        self.frappe.form_dict = {}
        self.frappe.request = None
        self.frappe.local = Doc(session=self.frappe.session)
        self.frappe.has_permission.return_value = True
        self.frappe.get_hooks.side_effect = lambda name: self.hooks.get(name, [])

        # Default fixture world: two companies, two outlets/branches, users.
        self.store.users = {
            "cashier@example.test": Doc(name="cashier@example.test", full_name="Carla Cashier", enabled=1, user_type="System User", custom_override_pin_hash=None),
            "manager@example.test": Doc(name="manager@example.test", full_name="Maya Manager", enabled=1, user_type="System User", custom_override_pin_hash=None),
            "manager2@example.test": Doc(name="manager2@example.test", full_name="Moses Manager", enabled=1, user_type="System User", custom_override_pin_hash=None),
            "admin_user@example.test": Doc(name="admin_user@example.test", full_name="Alex Admin", enabled=1, user_type="System User", custom_override_pin_hash=None),
            "sysmgr@example.test": Doc(name="sysmgr@example.test", full_name="Sam SysMgr", enabled=1, user_type="System User", custom_override_pin_hash=None),
            "disabled@example.test": Doc(name="disabled@example.test", full_name="Dana Disabled", enabled=0, user_type="System User", custom_override_pin_hash=None),
            "other_outlet@example.test": Doc(name="other_outlet@example.test", full_name="Omar OtherOutlet", enabled=1, user_type="System User", custom_override_pin_hash=None),
            "other_co@example.test": Doc(name="other_co@example.test", full_name="Olivia OtherCo", enabled=1, user_type="System User", custom_override_pin_hash=None),
            "Administrator": Doc(name="Administrator", full_name="Administrator", enabled=1, user_type="System User", custom_override_pin_hash=None),
            "Guest": Doc(name="Guest", full_name="Guest", enabled=1, user_type="Website User", custom_override_pin_hash=None),
        }
        self.store.user_roles = {
            "cashier@example.test": ["URY Cashier"],
            "manager@example.test": ["URY Manager"],
            "manager2@example.test": ["URY Manager"],
            "admin_user@example.test": ["URY Admin"],
            "sysmgr@example.test": ["System Manager"],
            "disabled@example.test": ["URY Manager"],
            "other_outlet@example.test": ["URY Manager"],
            "other_co@example.test": ["URY Manager"],
            # Administrator holds all roles in Frappe!
            "Administrator": ["Administrator", "System Manager", "URY Admin", "URY Manager", "URY Cashier"],
            "Guest": ["Guest"],
        }
        self.store.passwords = {
            "manager@example.test": "secret-pass-1",
            "manager2@example.test": "secret-pass-2",
            "cashier@example.test": "cashier-pass",
        }
        self.store.profiles = {
            "Main Till": Doc(
                name="Main Till", company="Hotel Co", branch="Main Branch", disabled=0,
                custom_override_max_attempts=5, custom_override_lock_minutes=15,
                applicable_for_users=[Doc(user="cashier@example.test")],
            ),
            "Pool Till": Doc(
                name="Pool Till", company="Hotel Co", branch="Pool Branch", disabled=0,
                custom_override_max_attempts=5, custom_override_lock_minutes=15,
                applicable_for_users=[Doc(user="other_outlet@example.test")],
            ),
            "Other Co Till": Doc(
                name="Other Co Till", company="Other Co", branch="Other Co Branch", disabled=0,
                custom_override_max_attempts=5, custom_override_lock_minutes=15,
                applicable_for_users=[Doc(user="other_co@example.test")],
            ),
        }
        self.store.branch_users = {
            ("Main Branch", "cashier@example.test"),
            ("Main Branch", "manager@example.test"),
            ("Main Branch", "manager2@example.test"),
            ("Main Branch", "admin_user@example.test"),
            ("Main Branch", "sysmgr@example.test"),
            ("Main Branch", "disabled@example.test"),
            ("Pool Branch", "other_outlet@example.test"),
            ("Other Co Branch", "other_co@example.test"),
        }
        self.store.invoices = {
            "POS-INV-100": Doc(
                name="POS-INV-100", owner="cashier@example.test", waiter="cashier@example.test",
                pos_profile="Main Till", company="Hotel Co",
            ),
            "POS-INV-MGR": Doc(
                name="POS-INV-MGR", owner="manager@example.test", waiter="manager@example.test",
                pos_profile="Main Till", company="Hotel Co",
            ),
        }
        # Seed default rules for Hotel Co
        for priv in EXPECTED_PRIVILEGES:
            self.store.privilege_rules.append(Doc(
                name=f"PPR-{priv}-mgr", company="Hotel Co", pos_profile="",
                privilege=priv, role="URY Manager",
                may_perform=0 if priv == "complimentary" else 1,
                may_authorise=1, max_percent=0, max_amount=0,
                requires_reason=0 if priv in ("reprint", "transfer_check") else 1,
            ))
            self.store.privilege_rules.append(Doc(
                name=f"PPR-{priv}-adm", company="Hotel Co", pos_profile="",
                privilege=priv, role="URY Admin",
                may_perform=0 if priv == "complimentary" else 1,
                may_authorise=1, max_percent=0, max_amount=0,
                requires_reason=0 if priv in ("reprint", "transfer_check") else 1,
            ))
        self.store.privilege_rules.append(Doc(
            name="PPR-reprint-cashier", company="Hotel Co", pos_profile="",
            privilege="reprint", role="URY Cashier",
            may_perform=1, may_authorise=0, max_percent=0, max_amount=0, requires_reason=0,
        ))

        self._wire_frappe_db()

        def check_password(user, pwd, **kw):
            if self.store.passwords.get(user) != pwd:
                raise AuthenticationFailed("Incorrect password")
            return user

        self.audit = MagicMock()
        self.frappe.utils = SimpleNamespace(
            cint=lambda v: int(v or 0),
            flt=lambda v: float(v or 0),
            get_fullname=lambda u: self.store.users.get(u, Doc()).get("full_name") or u,
            now_datetime=lambda: self.clock,
            get_datetime=lambda v=None: self.clock if v is None else v,
        )
        user_perm_mod = ModuleType("frappe.core.doctype.user_permission.user_permission")
        user_perm_mod.get_user_permissions = lambda user=None: self.store.user_permissions.get(
            user or self.frappe.session.user, {}
        )
        modules = {
            "frappe": self.frappe,
            "frappe.auth": SimpleNamespace(LoginAttemptTracker=Tracker),
            "frappe.utils": self.frappe.utils,
            "frappe.utils.password": SimpleNamespace(
                get_encryption_key=lambda: "test-site-encryption-key",
                check_password=check_password,
            ),
            "frappe.core": ModuleType("frappe.core"),
            "frappe.core.doctype": ModuleType("frappe.core.doctype"),
            "frappe.core.doctype.activity_log": ModuleType("frappe.core.doctype.activity_log"),
            "frappe.core.doctype.activity_log.activity_log": SimpleNamespace(add_authentication_log=self.audit),
            "frappe.core.doctype.user_permission": ModuleType("frappe.core.doctype.user_permission"),
            "frappe.core.doctype.user_permission.user_permission": user_perm_mod,
        }
        self.enterContext(patch.dict(sys.modules, modules))
        self.module = None

    def _wire_frappe_db(self):
        store = self.store

        def get_roles(user=None):
            return list(store.user_roles.get(user or self.frappe.session.user, []))

        def db_exists(doctype, filters=None):
            if doctype == "DocType":
                if filters == "Approval Limit":
                    return "Approval Limit" if store.has_approval_limit_doctype else None
                return filters
            if doctype == "User":
                if isinstance(filters, str):
                    for name in store.users:
                        if name.casefold() == filters.casefold():
                            return name
                    return None
                return None
            if doctype == "POS Profile":
                return filters if filters in store.profiles else None
            if doctype == "URY User":
                return (filters.get("parent"), filters.get("user")) in store.branch_users
            if doctype == "Approval Limit":
                return filters if filters in store.approval_limits else None
            if doctype == "POS Privilege Rule":
                return any(
                    all(row.get(k) == v for k, v in filters.items())
                    for row in store.privilege_rules
                )
            if doctype == "POS Authorisation Log":
                return any(
                    all(row.get(k) == v for k, v in filters.items())
                    for row in store.auth_logs
                )
            if doctype == "Employee":
                return store.employees.get(filters.get("user_id")) == filters.get("company")
            return None

        def db_get_value(doctype, filters, fieldname=None, as_dict=False, for_update=False, **kw):
            if doctype == "User" and isinstance(filters, str):
                u = store.users.get(filters)
                if not u:
                    return None
                if isinstance(fieldname, (list, tuple)):
                    return Doc({f: u.get(f) for f in fieldname}) if as_dict else tuple(u.get(f) for f in fieldname)
                return u.get(fieldname)
            if doctype == "POS Profile" and isinstance(filters, str):
                p = store.profiles.get(filters)
                if not p:
                    return None
                if isinstance(fieldname, (list, tuple)):
                    return Doc({f: p.get(f) for f in fieldname}) if as_dict else tuple(p.get(f) for f in fieldname)
                return p.get(fieldname)
            if doctype == "Approval Limit" and isinstance(filters, str):
                al = store.approval_limits.get(filters)
                if not al:
                    return None
                if isinstance(fieldname, (list, tuple)):
                    return Doc({f: al.get(f) for f in fieldname}) if as_dict else tuple(al.get(f) for f in fieldname)
                return al.get(fieldname)
            if doctype == "POS Invoice" and isinstance(filters, str):
                inv = store.invoices.get(filters)
                if not inv:
                    return None
                if isinstance(fieldname, (list, tuple)):
                    return Doc({f: inv.get(f) for f in fieldname}) if as_dict else tuple(inv.get(f) for f in fieldname)
                return inv.get(fieldname)
            if doctype == "POS Authorisation Log" and isinstance(filters, dict):
                for row in store.auth_logs:
                    if all(row.get(k) == v for k, v in filters.items()):
                        if isinstance(fieldname, (list, tuple)):
                            return Doc({f: row.get(f) for f in fieldname}) if as_dict else tuple(row.get(f) for f in fieldname)
                        return row.get(fieldname)
                return None
            return None

        def db_set_value(doctype, name, field, value=None, **kw):
            if doctype == "User" and name in store.users:
                store.users[name][field] = value

        def get_doc(arg1, arg2=None, **kw):
            if isinstance(arg1, dict):
                return FakeDocRecord(store, arg1["doctype"], arg1)
            if arg1 == "POS Profile":
                if arg2 not in store.profiles:
                    raise Invalid(f"POS Profile {arg2} not found")
                return store.profiles[arg2]
            if arg1 == "User":
                return store.users[arg2]
            raise KeyError((arg1, arg2))

        def get_all(doctype, filters=None, fields=None, pluck=None, order_by=None, **kw):
            filters = filters or {}
            if doctype == "POS Privilege Rule":
                rows = []
                for r in store.privilege_rules:
                    ok = True
                    for k, v in filters.items():
                        if isinstance(v, (list, tuple)) and len(v) == 2 and v[0] == "in":
                            if r.get(k) not in v[1]:
                                ok = False
                                break
                        elif r.get(k) != v:
                            ok = False
                            break
                    if ok:
                        rows.append(Doc(r))
                if pluck:
                    return [r.get(pluck) for r in rows]
                return rows
            if doctype == "User":
                rows = []
                for u in store.users.values():
                    ok = True
                    for k, v in filters.items():
                        if isinstance(v, (list, tuple)) and len(v) == 2 and v[0] == "in":
                            if u.get(k) not in v[1]:
                                ok = False
                        elif isinstance(v, (list, tuple)) and len(v) == 2 and v[0] == "is":
                            if v[1] == "set" and not u.get(k):
                                ok = False
                        elif u.get(k) != v:
                            ok = False
                    if ok:
                        rows.append(Doc(u))
                rows.sort(key=lambda r: (r.get("full_name") or r.get("name") or ""))
                if pluck:
                    return [r.get(pluck) for r in rows]
                return rows
            if doctype == "URY User":
                rows = [Doc(parent=b, user=u) for b, u in store.branch_users if b == filters.get("parent")]
                return [r.get(pluck) for r in rows] if pluck else rows
            if doctype == "POS Authorisation Log":
                rows = [Doc(r) for r in store.auth_logs if all(r.get(k) == v for k, v in filters.items())]
                return [r.get(pluck) for r in rows] if pluck else rows
            return []

        def db_table_exists(doctype):
            if doctype == "Approval Limit":
                return store.has_approval_limit_doctype
            return True

        self.frappe.get_roles.side_effect = get_roles
        self.frappe.db.exists.side_effect = db_exists
        self.frappe.db.get_value.side_effect = db_get_value
        self.frappe.db.set_value.side_effect = db_set_value
        self.frappe.db.table_exists.side_effect = db_table_exists
        self.frappe.db.commit.side_effect = store.commit
        self.frappe.db.rollback.side_effect = store.rollback
        self.frappe.db.is_unique_key_violation.side_effect = lambda err: isinstance(err, UniqueCollision)
        self.frappe.get_doc.side_effect = get_doc
        self.frappe.get_all.side_effect = get_all
        self.frappe.get_attr.side_effect = lambda path: self.hooks.get(f"__attr__:{path}")

    def api(self):
        path = ROOT / "ury/ury_pos/override.py"
        self.assertTrue(path.exists(), "Missing ury/ury_pos/override.py")
        if self.module is None:
            spec = importlib.util.spec_from_file_location("override_under_test", path)
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        return self.module

    def enroll(self, user, pin):
        api = self.api()
        prev_user = self.frappe.session.user
        try:
            self.frappe.session.user = user
            self.store.passwords.setdefault(user, "pw-" + user)
            api.set_my_pin(self.store.passwords[user], pin)
        finally:
            self.frappe.session.user = prev_user

    def refused(self, code, exc, fn):
        with self.assertRaises(exc) as caught:
            fn()
        message = str(caught.exception)
        self.assertIn(code, message, f"Expected {code} in {message!r}")
        # Plain sentence first, neutral wording (never starts with code bracket).
        self.assertFalse(message.startswith(code), f"Plain sentence must come first: {message!r}")
        self.assertFalse(message.startswith("["), f"Plain sentence must come first: {message!r}")
        self.assertNotRegex(message, VENDOR_RE, f"Vendor name leaked in refusal: {message!r}")
        return message

    # --- Schema, Fixtures & Hooks ---

    def test_doctypes_exist_with_contract_fields_and_docperms(self):
        rule_path = ROOT / "ury/ury/doctype/pos_privilege_rule/pos_privilege_rule.json"
        log_path = ROOT / "ury/ury/doctype/pos_authorisation_log/pos_authorisation_log.json"
        self.assertTrue(rule_path.is_file(), "Missing POS Privilege Rule DocType JSON")
        self.assertTrue(log_path.is_file(), "Missing POS Authorisation Log DocType JSON")

        rule_dt = json.loads(rule_path.read_text())
        rule_fields = {f["fieldname"]: f for f in rule_dt["fields"]}
        for name in (
            "company", "pos_profile", "privilege", "role",
            "may_perform", "may_authorise", "max_percent", "max_amount", "requires_reason",
        ):
            self.assertIn(name, rule_fields, f"POS Privilege Rule missing field {name}")
        self.assertEqual(rule_fields["pos_profile"].get("reqd", 0), 0)
        rule_perms = {p["role"]: p for p in rule_dt["permissions"]}
        self.assertEqual(rule_perms.get("URY Manager", {}).get("write"), 1)
        self.assertEqual(rule_perms.get("URY Manager", {}).get("create"), 1)
        self.assertNotIn("URY Cashier", rule_perms)
        self.assertNotIn("URY Captain", rule_perms)

        log_dt = json.loads(log_path.read_text())
        log_fields = {f["fieldname"]: f for f in log_dt["fields"]}
        for name in (
            "requester", "approver", "privilege", "reference_doctype", "reference_name",
            "pos_profile", "company", "claim_values", "reason", "outcome",
            "refusal_code", "event_time", "token_hash", "consumed_token_hash", "claim_hash", "expires_at",
        ):
            self.assertIn(name, log_fields, f"POS Authorisation Log missing field {name}")
        self.assertEqual(log_fields["consumed_token_hash"].get("unique"), 1)
        # Insert-only by server: NO role gets create/write/delete in DocPerm.
        for perm in log_dt["permissions"]:
            self.assertEqual(perm.get("create", 0), 0, f"Log must not grant create to {perm['role']}")
            self.assertEqual(perm.get("write", 0), 0, f"Log must not grant write to {perm['role']}")
            self.assertEqual(perm.get("delete", 0), 0, f"Log must not grant delete to {perm['role']}")
        log_roles = {p["role"] for p in log_dt["permissions"] if p.get("read")}
        self.assertTrue({"URY Manager", "Accounts Manager", "Auditor"} <= log_roles)

    def test_custom_fields_and_hooks_declare_override_pin_hash_non_unique_and_lockout_config(self):
        fields = {f["name"]: f for f in json.loads((ROOT / "ury/fixtures/custom_field.json").read_text())}
        pin_field = fields.get("User-custom_override_pin_hash", {})
        self.assertEqual(pin_field.get("dt"), "User")
        self.assertEqual(pin_field.get("fieldname"), "custom_override_pin_hash")
        self.assertEqual(pin_field.get("fieldtype"), "Data")
        self.assertEqual(pin_field.get("unique", 0), 0, "Shared PINs between users must be permitted")
        for flag in ("hidden", "read_only", "no_copy", "print_hide", "report_hide"):
            self.assertEqual(pin_field.get(flag), 1, flag)

        for fname in ("custom_override_max_attempts", "custom_override_lock_minutes"):
            cf = fields.get(f"POS Profile-{fname}", {})
            self.assertEqual(cf.get("dt"), "POS Profile", fname)
            self.assertEqual(cf.get("fieldtype"), "Int", fname)

        from ury import hooks
        fixture = next(row for row in hooks.fixtures if row.get("doctype") == "Custom Field")
        names = next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])
        for name in (
            "User-custom_override_pin_hash",
            "POS Profile-custom_override_max_attempts",
            "POS Profile-custom_override_lock_minutes",
        ):
            self.assertIn(name, names)

        patches_txt = (ROOT / "ury/patches.txt").read_text()
        self.assertIn("setup_pos_manager_authorisation", patches_txt)

    # --- PRIVILEGES table ---

    def test_privileges_define_all_contract_codes_as_four_tuples(self):
        api = self.api()
        self.assertTrue(EXPECTED_PRIVILEGES <= set(api.PRIVILEGES.keys()))
        for code in EXPECTED_PRIVILEGES:
            label, reason_req, perform_roles, authorise_roles = api.PRIVILEGES[code]
            self.assertIsInstance(label, str)
            self.assertTrue(label.strip())
            self.assertNotRegex(label, VENDOR_RE)
            self.assertIsInstance(reason_req, bool)
            self.assertIsInstance(perform_roles, (tuple, list))
            self.assertIsInstance(authorise_roles, (tuple, list))
        # Complimentary (100% comp) always requires a second person.
        self.assertEqual(tuple(api.PRIVILEGES["complimentary"][2]), ())

    # --- may_perform ---

    def test_may_perform_respects_rules_narrower_profile_override_and_limits(self):
        api = self.api()
        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "void_after_send"))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "void_after_send"))
        self.assertTrue(api.may_perform("cashier@example.test", "Main Till", "reprint"))

        # Narrower POS Profile rule disables manager on Main Till even though company rule allows it.
        self.store.privilege_rules.append(Doc(
            name="PPR-narrow-off", company="Hotel Co", pos_profile="Main Till",
            privilege="void_after_send", role="URY Manager",
            may_perform=0, may_authorise=1, max_percent=0, max_amount=0, requires_reason=1,
        ))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "void_after_send"))

        # Narrower rule with max_amount limit.
        self.store.privilege_rules.append(Doc(
            name="PPR-narrow-cap", company="Hotel Co", pos_profile="Main Till",
            privilege="void_after_send", role="URY Cashier",
            may_perform=1, may_authorise=0, max_percent=0, max_amount=50.0, requires_reason=1,
        ))
        self.assertTrue(api.may_perform("cashier@example.test", "Main Till", "void_after_send", amount=50.0))
        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "void_after_send", amount=50.01))

    def test_may_perform_denies_administrator_system_manager_disabled_and_other_outlet(self):
        api = self.api()
        self.assertFalse(api.may_perform("Administrator", "Main Till", "void_after_send"))
        self.assertFalse(api.may_perform("Guest", "Main Till", "reprint"))
        self.assertFalse(api.may_perform("sysmgr@example.test", "Main Till", "void_after_send"))
        self.assertFalse(api.may_perform("disabled@example.test", "Main Till", "void_after_send"))
        self.assertFalse(api.may_perform("other_outlet@example.test", "Main Till", "void_after_send"))
        self.assertFalse(api.may_perform("other_co@example.test", "Main Till", "void_after_send"))

    def test_complimentary_and_100_percent_discount_never_self_performed(self):
        api = self.api()
        self.store.approval_limits["Hotel Co"] = Doc(
            max_document_discount_percent="100",
            discount_limits_by_role="URY Manager: 100",
            discount_approver_role="URY Manager",
        )
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "complimentary"))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "discount", percent=100))

    def test_discount_limits_come_from_approval_limit_not_privilege_rule(self):
        api = self.api()
        # Even if a POS Privilege Rule has max_percent=50 for cashier, Approval Limit governs discount!
        self.store.privilege_rules.append(Doc(
            name="PPR-disc-cashier", company="Hotel Co", pos_profile="",
            privilege="discount", role="URY Cashier",
            may_perform=1, may_authorise=0, max_percent=50, max_amount=0, requires_reason=1,
        ))
        self.store.approval_limits["Hotel Co"] = Doc(
            max_document_discount_percent="",  # blank base = 0% at the till
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 25\nBroken: nan\nInf: inf",
            discount_approver_role="URY Admin",
        )
        self.assertTrue(api.may_perform("cashier@example.test", "Main Till", "discount", percent=5))
        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "discount", percent=6))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "discount", percent=25))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "discount", percent=30))

        # Approvers for 30% discount: admin_user (discount_approver_role), NOT manager (capped at 25%)!
        self.enroll("manager@example.test", "1111")
        self.enroll("admin_user@example.test", "2222")
        approvers = api.list_approvers("Main Till", "discount", percent=30)
        self.assertEqual([a["user"] for a in approvers], ["admin_user@example.test"])

    # --- PIN enrolment, shared PINs, manager clear ---

    def test_pin_enrolment_requires_own_password_stores_per_user_hmac_and_allows_shared_pin(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        self.frappe.form_dict = {"password": "secret-pass-1", "pin": "2468"}
        result = api.set_my_pin("secret-pass-1", "2468")
        self.assertEqual(result, {"ok": True, "has_pin": True})
        self.assertNotIn("pin", self.frappe.form_dict)
        self.assertNotIn("password", self.frappe.form_dict)
        self.assertNotIn("2468", json.dumps(result))

        hash1 = self.store.users["manager@example.test"]["custom_override_pin_hash"]
        self.assertEqual(len(hash1), 64)
        self.assertNotEqual(hash1, hashlib.sha256(b"2468").hexdigest())
        self.assertNotIn("2468", str(self.audit.call_args))

        # Second manager sets the EXACT SAME PIN "2468" -> distinct hash, both work!
        self.frappe.session.user = "manager2@example.test"
        api.set_my_pin("secret-pass-2", "2468")
        hash2 = self.store.users["manager2@example.test"]["custom_override_pin_hash"]
        self.assertNotEqual(hash1, hash2, "Per-user HMAC must produce distinct digests for shared PINs")

        # Both managers can authorise with "2468"
        self.frappe.session.user = "cashier@example.test"
        r1 = api.authorise("Main Till", "void_after_send", "manager@example.test", "2468", "POS Invoice", "POS-INV-100", {"qty": 1})
        r2 = api.authorise("Main Till", "void_after_send", "manager2@example.test", "2468", "POS Invoice", "POS-INV-100", {"qty": 1})
        self.assertEqual(r1["approver"], "manager@example.test")
        self.assertEqual(r2["approver"], "manager2@example.test")

    def test_set_my_pin_refuses_wrong_password_and_bad_pin_format(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        with self.assertRaises((AuthenticationFailed, Invalid)):
            api.set_my_pin("wrong-password", "2468")
        self.assertIsNone(self.store.users["manager@example.test"]["custom_override_pin_hash"])

        for bad_pin in ("", "123", "123456789", "abcd", "12 34"):
            with self.subTest(pin=bad_pin):
                with self.assertRaises(Invalid):
                    api.set_my_pin("secret-pass-1", bad_pin)
        self.assertIsNone(self.store.users["manager@example.test"]["custom_override_pin_hash"])

    def test_manager_can_clear_other_users_pin_but_cashier_cannot(self):
        api = self.api()
        self.enroll("manager2@example.test", "3579")
        self.frappe.session.user = "cashier@example.test"
        self.frappe.has_permission.return_value = False
        with self.assertRaises(Denied):
            api.clear_user_pin("manager2@example.test", "Main Till")
        self.assertIsNotNone(self.store.users["manager2@example.test"]["custom_override_pin_hash"])

        self.frappe.session.user = "manager@example.test"
        self.frappe.has_permission.return_value = True
        cleared = api.clear_user_pin("manager2@example.test", "Main Till")
        self.assertEqual(cleared, {"ok": True, "user": "manager2@example.test", "has_pin": False})
        self.assertIsNone(self.store.users["manager2@example.test"]["custom_override_pin_hash"])

    # --- list_approvers ---

    def test_list_approvers_returns_names_only_excluding_requester_owner_and_ineligible(self):
        api = self.api()
        for u in ("manager@example.test", "manager2@example.test", "disabled@example.test",
                  "other_outlet@example.test", "other_co@example.test", "sysmgr@example.test"):
            self.store.users[u]["custom_override_pin_hash"] = api._pin_digest(u, "2468")

        self.frappe.session.user = "cashier@example.test"
        # On POS-INV-MGR, manager@example.test owns the check so must be excluded!
        rows = api.list_approvers("Main Till", "void_after_send", reference_name="POS-INV-MGR")
        self.assertEqual(rows, [{"user": "manager2@example.test", "full_name": "Moses Manager"}])
        # Strict shape: names only, never hashes.
        for row in rows:
            self.assertEqual(set(row.keys()), {"user", "full_name"})

        # Requester is never included in their own list_approvers.
        self.frappe.session.user = "manager@example.test"
        rows_for_mgr = api.list_approvers("Main Till", "complimentary", reference_name="POS-INV-100")
        self.assertEqual([r["user"] for r in rows_for_mgr], ["manager2@example.test"])

    # --- authorise adversarial cases ---

    def test_authorise_refuses_self_approval_and_check_owner_approval(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        self.frappe.session.user = "manager@example.test"
        self.refused("POS-AUTH-SELF", Denied, lambda: api.authorise(
            "Main Till", "complimentary", "manager@example.test", "2468", "POS Invoice", "POS-INV-100", {"percent": 100}
        ))

        # Cashier requests approval on a check owned by manager@example.test -> manager cannot approve their own check!
        self.frappe.session.user = "cashier@example.test"
        self.refused("POS-AUTH-SELF", Denied, lambda: api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468", "POS Invoice", "POS-INV-MGR", {"qty": 1}
        ))

    def test_authorise_refuses_disabled_admin_sysmgr_and_other_outlet_or_company(self):
        api = self.api()
        for bad_approver in (
            "disabled@example.test",
            "Administrator",
            "Guest",
            "sysmgr@example.test",
            "other_outlet@example.test",
            "other_co@example.test",
        ):
            self.store.users[bad_approver]["custom_override_pin_hash"] = api._pin_digest(bad_approver, "2468")
            with self.subTest(approver=bad_approver):
                self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda a=bad_approver: api.authorise(
                    "Main Till", "void_after_send", a, "2468", "POS Invoice", "POS-INV-100", {"qty": 1}
                ))

    def test_authorise_refuses_approver_without_pin(self):
        api = self.api()
        self.refused("POS-AUTH-NO-PIN", Invalid, lambda: api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468", "POS Invoice", "POS-INV-100", {"qty": 1}
        ))

    def test_wrong_pin_counts_through_rollback_locks_and_never_leaks_pin(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        self.store.profiles["Main Till"]["custom_override_max_attempts"] = 3
        self.store.profiles["Main Till"]["custom_override_lock_minutes"] = 10

        for attempt in range(2):
            self.frappe.form_dict = {"pin": "9999"}
            msg = self.refused("POS-AUTH-PIN", Invalid, lambda: api.authorise(
                "Main Till", "void_after_send", "manager@example.test", "9999",
                "POS Invoice", "POS-INV-100", {"amount": 25},
            ))
            self.assertNotIn("9999", msg)
            self.assertNotIn("2468", msg)
            self.assertNotIn("pin", self.frappe.form_dict)
            # Simulate request rollback after refusal: attempt counter in cache must survive!
            self.frappe.db.rollback()

        # 3rd wrong attempt triggers lockout!
        msg = self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "9999",
            "POS Invoice", "POS-INV-100", {"amount": 25},
        ))
        self.assertNotIn("9999", msg)
        self.frappe.db.rollback()

        # Even the RIGHT PIN is refused during lockout!
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"amount": 25},
        ))

        # Failed PIN audit rows survived rollback and never contain PIN or hash!
        dumped_logs = json.dumps(self.store.committed_logs, default=str)
        self.assertGreaterEqual(len(self.store.committed_logs), 3)
        self.assertNotIn("9999", dumped_logs)
        self.assertNotIn("2468", dumped_logs)
        self.assertNotIn(self.store.users["manager@example.test"]["custom_override_pin_hash"], dumped_logs)

        # After lock interval expires, right PIN succeeds!
        self.clock += timedelta(minutes=10, seconds=1)
        res = api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"amount": 25},
        )
        self.assertEqual(res["approver"], "manager@example.test")
        self.assertEqual(res["expires_in"], 120)

    def test_authorise_and_consume_refuse_ai_and_api_key_sessions(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")

        # 1. API-key Authorization header
        self.frappe.request = SimpleNamespace(
            headers={"Authorization": "token abcdef:123456"},
            cookies={},
        )
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"amount": 10},
        ))

        # 2. Bearer OAuth token (Eve / MCP)
        self.frappe.request = SimpleNamespace(
            headers={"Authorization": "Bearer eyJ.test"},
            cookies={},
        )
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"amount": 10},
        ))

        # 3. Kamra #32 agent call flag
        self.frappe.request = None
        self.frappe.flags.kamra_agent_call = "MCP"
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"amount": 10},
        ))
        self.frappe.flags.kamra_agent_call = None

        # 4. Mint token in interactive session, then try to consume from API-key session -> refused!
        self.frappe.request = SimpleNamespace(headers={}, cookies={"sid": "sess-cashier-1"})
        issued = api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"amount": 10},
        )
        self.frappe.request = SimpleNamespace(headers={"Authorization": "token k:s"}, cookies={})
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.consume(
            issued["token"], "void_after_send", "Main Till", "POS-INV-100", {"amount": 10},
        ))

    def test_pos_override_pin_verifier_hook_replaces_builtin_store(self):
        api = self.api()
        called = []

        def custom_verifier(user, pin, pos_profile=None):
            called.append((user, pin, pos_profile))
            return pin == "7788"

        self.hooks["pos_override_pin_verifier"] = ["custom_app.pin.verify"]
        self.hooks["__attr__:custom_app.pin.verify"] = custom_verifier

        # manager@example.test has NO custom_override_pin_hash, yet hook verifies!
        self.assertIsNone(self.store.users["manager@example.test"]["custom_override_pin_hash"])
        # list_approvers also includes manager when external hook is active.
        approvers = api.list_approvers("Main Till", "void_after_send", reference_name="POS-INV-100")
        self.assertIn("manager@example.test", [a["user"] for a in approvers])

        self.refused("POS-AUTH-PIN", Invalid, lambda: api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "0000",
            "POS Invoice", "POS-INV-100", {"qty": 1},
        ))
        res = api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "7788",
            "POS Invoice", "POS-INV-100", {"qty": 1},
        )
        self.assertEqual(res["approver"], "manager@example.test")
        self.assertEqual(called[-1], ("manager@example.test", "7788", "Main Till"))

    # --- consume & require adversarial cases ---

    def test_consume_binds_claim_check_user_ttl_and_single_use(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        claim = {"amount": 50, "percent": 10, "reason": "Kitchen error"}
        issued = api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        token = issued["token"]

        # Raw token is NOT stored in POS Authorisation Log (only sha256).
        self.assertNotIn(token, json.dumps(self.store.auth_logs, default=str))

        # Changed amount -> POS-AUTH-CHANGED
        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            token, "void_after_send", "Main Till", "POS-INV-100",
            {"amount": 60, "percent": 10, "reason": "Kitchen error"},
        ))
        # Changed check -> POS-AUTH-CHANGED
        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            token, "void_after_send", "Main Till", "POS-INV-OTHER", claim,
        ))
        # Changed privilege -> POS-AUTH-CHANGED
        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            token, "cancel_check", "Main Till", "POS-INV-100", claim,
        ))
        # Other session user -> POS-AUTH-CHANGED
        self.frappe.session.user = "manager2@example.test"
        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            token, "void_after_send", "Main Till", "POS-INV-100", claim,
        ))
        self.frappe.session.user = "cashier@example.test"

        # Canonical JSON: different key order & 50.0 vs 50 still match!
        consumed = api.consume(
            token, "void_after_send", "Main Till", "POS-INV-100",
            {"reason": "Kitchen error", "percent": 10.0, "amount": 50.0},
        )
        self.assertEqual(consumed.approver, "manager@example.test")
        self.assertEqual(consumed.percent, 10.0)
        self.assertEqual(consumed.amount, 50.0)

        # Replay of consumed token -> POS-AUTH-EXPIRED
        self.refused("POS-AUTH-EXPIRED", Invalid, lambda: api.consume(
            token, "void_after_send", "Main Till", "POS-INV-100", claim,
        ))

    def test_consume_refuses_expired_token_and_concurrent_unique_collision(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        claim = {"amount": 20}
        issued = api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.clock += timedelta(seconds=121)
        self.refused("POS-AUTH-EXPIRED", Invalid, lambda: api.consume(
            issued["token"], "void_after_send", "Main Till", "POS-INV-100", claim,
        ))

        # Fresh token with simulated DB unique collision on concurrent consume -> POS-AUTH-EXPIRED
        issued2 = api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.store.force_consume_collision = True
        self.refused("POS-AUTH-EXPIRED", Invalid, lambda: api.consume(
            issued2["token"], "void_after_send", "Main Till", "POS-INV-100", claim,
        ))

    def test_require_returns_none_when_allowed_consumes_token_or_raises_required(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        claim = {"amount": 15}

        # Manager acts alone -> None
        self.frappe.session.user = "manager@example.test"
        self.assertIsNone(api.require("void_after_send", "Main Till", "POS-INV-100", claim))

        # Cashier without token -> POS-AUTH-REQUIRED
        self.frappe.session.user = "cashier@example.test"
        self.refused("POS-AUTH-REQUIRED", Denied, lambda: api.require(
            "void_after_send", "Main Till", "POS-INV-100", claim,
        ))

        # Cashier with valid token -> consumed approval dict
        issued = api.authorise(
            "Main Till", "void_after_send", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        approved = api.require(
            "void_after_send", "Main Till", "POS-INV-100", claim, token=issued["token"],
        )
        self.assertEqual(approved.approver, "manager@example.test")


if __name__ == "__main__":
    unittest.main()
