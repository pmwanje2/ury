"""Site-less unit tests for POS manager PIN authorisation (Simphony/SAP core + Amendment 1 + R2 security hardening).

Tests the fixed shared contract + Amendment 1 + R2 security fixes in `ury/ury_pos/override.py`:
- PINs stored exclusively in Frappe `__Auth` via `update_password`/`check_password` (never on `User`)
- Wrong password in `set_my_pin` refuses with ValidationError (never AuthenticationError) + counts failures
- Impersonated and non-interactive/AI sessions refused across `authorise`, `consume`, `may_perform`, `list_approvers`, enrolment
- Fail closed when company/profile has zero rules (manager roles only, never "everyone"); explicit 0% discount rule is 0%
- `consume()` re-checks approver & requester role/rule/outlet/enabled and survives caller DB rollback
- `requires_reason` enforced server-side against active `POS Reason` rows (+ mandatory note)
- DB-backed lockout survives Redis flush, scoped per `(approver, pos_profile)`, with per-session rate limit
- `list_approvers` enforces requester outlet/company membership; external verifier codes pass through without strikes
"""

import hashlib
import importlib.util
import json
import re
import secrets
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
VENDOR_RE = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala)\b", re.I)

EXPECTED_PRIVILEGES = {
    "discount",
    "discount_remove",
    "complimentary",
    "void_sent_item",
    "reduce_sent_qty",
    "cancel_check",
    "return",
    "reopen_check",
    "adjust_closed_check",
    "price_override",
    "open_item",
    "service_charge_waive",
    "tax_exempt",
    "transfer_check",
    "pickup_other_check",
    "merge_checks",
    "split_check",
    "guest_count_change",
    "order_type_change",
    "reprint_bill",
    "reprint_kot",
    "no_sale",
    "paid_out",
    "cash_pickup",
    "cash_loan",
    "over_short_close",
    "over_halo_tender",
    "on_account_over_limit",
    "room_charge_over_limit",
    "tip_adjust",
    "item_86_restore",
    "reservation_cancel",
    "offline_tender",
    "close_other_till",
    "reopen_till",
    "fx_rate_override",
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
    """In-memory stand-in for MariaDB tables (`__Auth`, `POS Authorisation Log`, etc.)."""

    def __init__(self, case):
        self.case = case
        self.users = {}
        self.user_roles = {}
        self.user_permissions = {}
        self.profiles = {}
        self.branch_users = set()  # (branch, user)
        self.employees = {}  # user -> Doc(name, company)
        self.checkins = {}  # employee -> list[Doc(log_type, time)]
        self.privilege_rules = []  # list[Doc]
        self.reasons = []  # list[Doc]
        self.approval_limits = {}  # company -> Doc
        self.invoices = {}  # name -> Doc
        self.auth_logs = []  # list[Doc]
        self.consumed_hashes = set()
        # __Auth table: (doctype, name_lower, fieldname) -> (canonical_name, salted_hash)
        self.auth_table = {}
        self.defaults = {}
        self.has_approval_limit_doctype = True
        self.has_checkin_doctype = True
        self.pending_logs = []
        self.committed_logs = []
        self.force_consume_collision = False

    def set_auth(self, doctype, user, fieldname, secret):
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 1000).hex()
        self.auth_table[(doctype, user.casefold(), fieldname)] = (user, f"$pbkdf2-sha256${salt}${digest}")

    def verify_auth(self, doctype, user, fieldname, secret):
        entry = self.auth_table.get((doctype, user.casefold(), fieldname))
        if not entry:
            return None
        canonical, stored = entry
        _, salt, digest = stored.split("$")[1:]
        candidate = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), 1000).hex()
        if candidate == digest:
            return canonical
        return None

    def get_auth_hash(self, doctype, user, fieldname):
        entry = self.auth_table.get((doctype, user.casefold(), fieldname))
        return entry[1] if entry else None

    def delete_auth(self, doctype, user, fieldname):
        self.auth_table.pop((doctype, user.casefold(), fieldname), None)

    def users_with_auth(self, doctype, fieldname):
        return [canonical for (dt, _u, fn), (canonical, _h) in self.auth_table.items() if dt == doctype and fn == fieldname]

    def insert_doc(self, doc, *, isolated=False):
        if doc.doctype == "POS Authorisation Log":
            if doc.get("consumed_token_hash"):
                if self.force_consume_collision or doc["consumed_token_hash"] in self.consumed_hashes:
                    raise UniqueCollision("Duplicate consumed_token_hash")
                self.consumed_hashes.add(doc["consumed_token_hash"])
            row = Doc(doc)
            if not row.get("name"):
                row["name"] = f"POS-AUTH-LOG-{len(self.auth_logs) + 1:04d}"
            self.auth_logs.append(row)
            if isolated:
                self.committed_logs.append(row)
            else:
                self.pending_logs.append(row)
        elif doc.doctype == "POS Privilege Rule":
            row = Doc(doc)
            if not row.get("name"):
                row["name"] = f"PPR-{len(self.privilege_rules) + 1:04d}"
            self.privilege_rules.append(row)
        elif doc.doctype == "POS Reason":
            row = Doc(doc)
            if not row.get("name"):
                row["name"] = row.get("code")
            self.reasons.append(row)

    def commit(self):
        self.committed_logs.extend(self.pending_logs)
        self.pending_logs.clear()

    def rollback(self):
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
        self.frappe.session = Doc(user="cashier@example.test", sid="sess-cashier-1", data={})
        self.frappe.flags = Doc()
        self.frappe.conf = {}
        self.frappe.form_dict = {}
        self.frappe.request = None
        self.frappe.local = Doc(session=self.frappe.session)
        self.frappe.has_permission.return_value = True
        self.frappe.get_hooks.side_effect = lambda name: self.hooks.get(name, [])
        self.frappe.generate_hash.side_effect = lambda length=10: secrets.token_hex(length)[:length]

        self.store.users = {
            "cashier@example.test": Doc(name="cashier@example.test", full_name="Carla Cashier", enabled=1, user_type="System User"),
            "manager@example.test": Doc(name="manager@example.test", full_name="Maya Manager", enabled=1, user_type="System User"),
            "manager2@example.test": Doc(name="manager2@example.test", full_name="Moses Manager", enabled=1, user_type="System User"),
            "admin_user@example.test": Doc(name="admin_user@example.test", full_name="Alex Admin", enabled=1, user_type="System User"),
            "sysmgr@example.test": Doc(name="sysmgr@example.test", full_name="Sam SysMgr", enabled=1, user_type="System User"),
            "disabled@example.test": Doc(name="disabled@example.test", full_name="Dana Disabled", enabled=0, user_type="System User"),
            "other_outlet@example.test": Doc(name="other_outlet@example.test", full_name="Omar OtherOutlet", enabled=1, user_type="System User"),
            "other_co@example.test": Doc(name="other_co@example.test", full_name="Olivia OtherCo", enabled=1, user_type="System User"),
            "Administrator": Doc(name="Administrator", full_name="Administrator", enabled=1, user_type="System User"),
            "Guest": Doc(name="Guest", full_name="Guest", enabled=1, user_type="Website User"),
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
            "Administrator": ["Administrator", "System Manager", "URY Admin", "URY Manager", "URY Cashier"],
            "Guest": ["Guest"],
        }
        for u, pw in (
            ("manager@example.test", "secret-pass-1"),
            ("manager2@example.test", "secret-pass-2"),
            ("cashier@example.test", "cashier-pass"),
            ("admin_user@example.test", "admin-pass"),
        ):
            self.store.set_auth("User", u, "password", pw)

        self.store.profiles = {
            "Main Till": Doc(
                name="Main Till", company="Hotel Co", branch="Main Branch", disabled=0,
                custom_override_max_attempts=5, custom_override_lock_minutes=15,
                applicable_for_users=[Doc(user="cashier@example.test")],
            ),
            "Bar Till": Doc(
                name="Bar Till", company="Hotel Co", branch="Main Branch", disabled=0,
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
                pos_profile="Main Till", branch="Main Branch", company="Hotel Co",
            ),
            "POS-INV-MGR": Doc(
                name="POS-INV-MGR", owner="manager@example.test", waiter="manager@example.test",
                pos_profile="Main Till", branch="Main Branch", company="Hotel Co",
            ),
            "POS-INV-POOL": Doc(
                name="POS-INV-POOL", owner="other_outlet@example.test", waiter="other_outlet@example.test",
                pos_profile="Pool Till", branch="Pool Branch", company="Hotel Co",
            ),
        }
        for priv in EXPECTED_PRIVILEGES:
            self.store.privilege_rules.append(Doc(
                name=f"PPR-{priv}-mgr", company="Hotel Co", pos_profile="",
                privilege=priv, role="URY Manager",
                may_perform=0 if priv == "complimentary" else 1,
                may_authorise=1, max_percent=0, max_amount=0,
                requires_reason=0 if priv in ("reprint_bill", "reprint_kot", "transfer_check", "split_check", "guest_count_change", "order_type_change") else 1,
                allow_self_approval=0, require_clocked_in=0, privilege_group=0,
            ))
            self.store.privilege_rules.append(Doc(
                name=f"PPR-{priv}-adm", company="Hotel Co", pos_profile="",
                privilege=priv, role="URY Admin",
                may_perform=0 if priv == "complimentary" else 1,
                may_authorise=1, max_percent=0, max_amount=0,
                requires_reason=0 if priv in ("reprint_bill", "reprint_kot", "transfer_check", "split_check", "guest_count_change", "order_type_change") else 1,
                allow_self_approval=0, require_clocked_in=0, privilege_group=0,
            ))
        self.store.privilege_rules.append(Doc(
            name="PPR-reprint-cashier", company="Hotel Co", pos_profile="",
            privilege="reprint_bill", role="URY Cashier",
            may_perform=1, may_authorise=0, max_percent=0, max_amount=0, requires_reason=0,
            allow_self_approval=0, require_clocked_in=0, privilege_group=0,
        ))
        self.store.privilege_rules.append(Doc(
            name="PPR-discount-cashier", company="Hotel Co", pos_profile="",
            privilege="discount", role="URY Cashier",
            may_perform=1, may_authorise=0, max_percent=0, max_amount=0, requires_reason=1,
            allow_self_approval=0, require_clocked_in=0, privilege_group=0,
        ))
        self.store.reasons = [
            Doc(name="VOID-GUEST", code="VOID-GUEST", description="Guest changed mind", applies_to="void_sent_item\nreduce_sent_qty\ncancel_check", requires_note=0, sort_order=10, disabled=0),
            Doc(name="VOID-OFF", code="VOID-OFF", description="Disabled reason", applies_to="void_sent_item", requires_note=0, sort_order=15, disabled=1),
            Doc(name="RET-QUAL", code="RET-QUAL", description="Quality issue", applies_to="return", requires_note=0, sort_order=20, disabled=0),
            Doc(name="NOSALE-CHANGE", code="NOSALE-CHANGE", description="Making change for guest", applies_to="no_sale", requires_note=0, sort_order=30, disabled=0),
            Doc(name="PRICE-SPECIAL", code="PRICE-SPECIAL", description="Manager agreed price", applies_to="price_override\nopen_item", requires_note=1, sort_order=40, disabled=0),
            Doc(name="OTHER", code="OTHER", description="Other", applies_to="", requires_note=1, sort_order=999, disabled=0),
        ]

        self._wire_frappe_db()

        def check_password(user, pwd, doctype="User", fieldname="password", delete_tracker_cache=True):
            matched = self.store.verify_auth(doctype, user, fieldname, pwd)
            if not matched:
                raise AuthenticationFailed("Incorrect User or Password")
            return matched

        def update_password(user, pwd, doctype="User", fieldname="password", logout_all_sessions=False):
            self.store.set_auth(doctype, user, fieldname, pwd)

        self.audit = MagicMock()
        self.frappe.utils = SimpleNamespace(
            cint=lambda v: int(v or 0),
            flt=lambda v, precision=None: round(float(v or 0), precision) if precision is not None else float(v or 0),
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
                update_password=update_password,
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
                return any(all(row.get(k) == v for k, v in filters.items()) for row in store.privilege_rules)
            if doctype == "POS Authorisation Log":
                return any(all(row.get(k) == v for k, v in filters.items()) for row in store.auth_logs)
            if doctype == "Employee":
                emp = store.employees.get(filters.get("user_id"))
                if not emp:
                    return None
                if "company" in filters and emp.get("company") != filters["company"]:
                    return None
                return emp.get("name")
            if doctype == "Employee Checkin":
                if not filters:
                    return any(bool(rows) for rows in store.checkins.values())
                emp = filters.get("employee")
                return bool(store.checkins.get(emp)) if emp else any(bool(rows) for rows in store.checkins.values())
            return None

        def db_get_value(doctype, filters, fieldname=None, as_dict=False, for_update=False, order_by=None, **kw):
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
            if doctype == "Employee" and isinstance(filters, dict):
                emp = store.employees.get(filters.get("user_id"))
                if not emp:
                    return None
                if "company" in filters and emp.get("company") != filters["company"]:
                    return None
                if "status" in filters and emp.get("status", "Active") != filters["status"]:
                    return None
                return emp.get(fieldname)
            if doctype == "Employee Checkin" and isinstance(filters, dict):
                rows = list(store.checkins.get(filters.get("employee"), []))
                if not rows:
                    return None
                rows.sort(key=lambda r: r.get("time"), reverse=True)
                latest = rows[0]
                if isinstance(fieldname, (list, tuple)):
                    return Doc({f: latest.get(f) for f in fieldname}) if as_dict else tuple(latest.get(f) for f in fieldname)
                return latest.get(fieldname)
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

        def db_sql(query, values=None, as_dict=False, **kw):
            q = " ".join(str(query).split())
            q_upper = q.upper()
            vals = values or ()
            if "`__AUTH`" in q_upper:
                if q_upper.startswith("SELECT"):
                    if "AND NAME =" in q_upper or "AND NAME=" in q_upper:
                        dt, user, fn = vals[0], vals[1], vals[2]
                        entry = store.auth_table.get((dt, str(user).casefold(), fn))
                        if not entry:
                            return []
                        return [Doc(name=entry[0])] if as_dict else [(entry[0],)]
                    dt, fn = vals[0], vals[1]
                    users = store.users_with_auth(dt, fn)
                    return [Doc(name=u) for u in users] if as_dict else [(u,) for u in users]
                if q_upper.startswith("DELETE"):
                    dt, user, fn = vals[0], vals[1], vals[2]
                    store.delete_auth(dt, str(user), fn)
                    return []
            if "`TABPOS AUTHORISATION LOG`" in q_upper:
                if q_upper.startswith("SELECT MAX(EVENT_TIME)"):
                    # Reset timestamp query for approver + pos_profile or password
                    if "OUTCOME IN" in q_upper and "POS_PROFILE" in q_upper:
                        approver, pos_profile, since = vals[0], vals[1], vals[2]
                        times = [
                            r["event_time"] for r in store.auth_logs
                            if (r.get("approver") or "").casefold() == str(approver).casefold()
                            and r.get("event_time") >= since
                            and (
                                (r.get("outcome") == "Approved" and r.get("pos_profile") == pos_profile)
                                or r.get("outcome") == "PIN Reset"
                            )
                        ]
                        mx = max(times) if times else None
                        return [Doc(last_reset=mx)] if as_dict else [(mx,)]
                    if "OUTCOME = 'PIN RESET'" in q_upper or "OUTCOME='PIN RESET'" in q_upper:
                        user, since = vals[0], vals[1]
                        times = [
                            r["event_time"] for r in store.auth_logs
                            if (r.get("approver") or "").casefold() == str(user).casefold()
                            and r.get("outcome") == "PIN Reset"
                            and r.get("event_time") >= since
                        ]
                        mx = max(times) if times else None
                        return [Doc(last_reset=mx)] if as_dict else [(mx,)]
                if q_upper.startswith("SELECT COUNT(*)"):
                    if "OUTCOME = 'FAILED PASSWORD'" in q_upper:
                        user, since = vals[0], vals[1]
                        cnt = sum(
                            1 for r in store.auth_logs
                            if (r.get("approver") or "").casefold() == str(user).casefold()
                            and r.get("outcome") == "Failed Password"
                            and r.get("event_time") > since
                        )
                        return [Doc(cnt=cnt)] if as_dict else [(cnt,)]
                    if "REQUESTER =" in q_upper:
                        requester, pos_profile, since = vals[0], vals[1], vals[2]
                        cnt = sum(
                            1 for r in store.auth_logs
                            if (r.get("requester") or "").casefold() == str(requester).casefold()
                            and r.get("pos_profile") == pos_profile
                            and r.get("outcome") == "Failed PIN"
                            and r.get("event_time") > since
                        )
                        return [Doc(cnt=cnt)] if as_dict else [(cnt,)]
                    if "APPROVER =" in q_upper:
                        approver, pos_profile, since = vals[0], vals[1], vals[2]
                        cnt = sum(
                            1 for r in store.auth_logs
                            if (r.get("approver") or "").casefold() == str(approver).casefold()
                            and r.get("pos_profile") == pos_profile
                            and r.get("outcome") == "Failed PIN"
                            and r.get("event_time") > since
                        )
                        return [Doc(cnt=cnt)] if as_dict else [(cnt,)]
            return []

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
                return [r.get(pluck) for r in rows] if pluck else rows
            if doctype == "POS Reason":
                rows = [Doc(r) for r in store.reasons if all(r.get(k) == v for k, v in filters.items())]
                rows.sort(key=lambda r: (int(r.get("sort_order") or 0), r.get("code") or ""))
                return [r.get(pluck) for r in rows] if pluck else rows
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
                return [r.get(pluck) for r in rows] if pluck else rows
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
            if doctype == "Employee Checkin":
                return store.has_checkin_doctype
            return True

        self.frappe.get_roles.side_effect = get_roles
        self.frappe.db.exists.side_effect = db_exists
        self.frappe.db.get_value.side_effect = db_get_value
        self.frappe.db.set_value.side_effect = db_set_value
        self.frappe.db.sql.side_effect = db_sql
        self.frappe.db.get_default.side_effect = lambda k: store.defaults.get(k)
        self.frappe.db.set_default.side_effect = lambda k, v: store.defaults.__setitem__(k, v)
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
            pw = "pw-" + user
            if not self.store.get_auth_hash("User", user, "password"):
                self.store.set_auth("User", user, "password", pw)
                pass_to_use = pw
            else:
                # Use known fixture passwords for standard test accounts.
                pass_to_use = {
                    "manager@example.test": "secret-pass-1",
                    "manager2@example.test": "secret-pass-2",
                    "cashier@example.test": "cashier-pass",
                    "admin_user@example.test": "admin-pass",
                }.get(user, pw)
            api.set_my_pin(pass_to_use, pin)
        finally:
            self.frappe.session.user = prev_user

    def refused(self, code, exc, fn):
        with self.assertRaises(exc) as caught:
            fn()
        message = str(caught.exception)
        self.assertIn(code, message, f"Expected {code} in {message!r}")
        self.assertFalse(message.startswith(code), f"Plain sentence must come first: {message!r}")
        self.assertFalse(message.startswith("["), f"Plain sentence must come first: {message!r}")
        self.assertNotRegex(message, VENDOR_RE, f"Vendor name leaked in refusal: {message!r}")
        return message

    # --- Schema, Fixtures & Hooks (Contract + Amendment 1 + R2 P0 #1 / P1 #5) ---

    def test_doctypes_exist_with_contract_and_amendment_1_fields_and_docperms(self):
        rule_path = ROOT / "ury/ury/doctype/pos_privilege_rule/pos_privilege_rule.json"
        log_path = ROOT / "ury/ury/doctype/pos_authorisation_log/pos_authorisation_log.json"
        reason_path = ROOT / "ury/ury/doctype/pos_reason/pos_reason.json"
        self.assertTrue(rule_path.is_file(), "Missing POS Privilege Rule DocType JSON")
        self.assertTrue(log_path.is_file(), "Missing POS Authorisation Log DocType JSON")
        self.assertTrue(reason_path.is_file(), "Missing POS Reason DocType JSON (Amendment 1)")

        rule_dt = json.loads(rule_path.read_text())
        rule_fields = {f["fieldname"]: f for f in rule_dt["fields"]}
        for name in (
            "company", "pos_profile", "privilege", "role",
            "may_perform", "may_authorise", "max_percent", "max_amount", "requires_reason",
            "allow_self_approval", "require_clocked_in", "privilege_group",
        ):
            self.assertIn(name, rule_fields, f"POS Privilege Rule missing field {name}")
        self.assertEqual(rule_fields["pos_profile"].get("reqd", 0), 0)
        self.assertEqual(str(rule_fields["allow_self_approval"].get("default", "0")), "0")
        self.assertEqual(str(rule_fields["require_clocked_in"].get("default", "0")), "0")
        self.assertEqual(rule_fields["privilege_group"].get("fieldtype"), "Int")
        self.assertEqual(str(rule_fields["privilege_group"].get("default", "0")), "0")
        options = set((rule_fields["privilege"].get("options") or "").splitlines())
        self.assertEqual(options, EXPECTED_PRIVILEGES)

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
        outcome_opts = set((log_fields["outcome"].get("options") or "").splitlines())
        self.assertTrue({"Approved", "Consumed", "Refused", "Failed PIN", "Failed Password", "Locked", "PIN Reset"} <= outcome_opts)
        for perm in log_dt["permissions"]:
            self.assertEqual(perm.get("create", 0), 0, f"Log must not grant create to {perm['role']}")
            self.assertEqual(perm.get("write", 0), 0, f"Log must not grant write to {perm['role']}")
            self.assertEqual(perm.get("delete", 0), 0, f"Log must not grant delete to {perm['role']}")
        log_roles = {p["role"] for p in log_dt["permissions"] if p.get("read")}
        self.assertTrue({"URY Manager", "Accounts Manager", "Auditor"} <= log_roles)

        reason_dt = json.loads(reason_path.read_text())
        reason_fields = {f["fieldname"]: f for f in reason_dt["fields"]}
        for name in ("code", "description", "applies_to", "requires_note", "sort_order", "disabled"):
            self.assertIn(name, reason_fields, f"POS Reason missing field {name}")
        reason_perms = {p["role"]: p for p in reason_dt["permissions"]}
        self.assertEqual(reason_perms.get("URY Manager", {}).get("write"), 1)
        self.assertEqual(reason_perms.get("URY Cashier", {}).get("read"), 1)
        self.assertEqual(reason_perms.get("URY Cashier", {}).get("write", 0), 0)

    def test_override_pin_hash_removed_from_user_and_company_after_insert_wired(self):
        """P0 #1 & P1 #5: PIN hash must NOT exist on User; Company.after_insert seeds rules."""
        fields = {f["name"]: f for f in json.loads((ROOT / "ury/fixtures/custom_field.json").read_text())}
        self.assertNotIn("User-custom_override_pin_hash", fields, "PIN hash must not be a Custom Field on User")

        setup_src = (ROOT / "ury/setup_customizations.py").read_text()
        self.assertNotIn("custom_override_pin_hash", setup_src, "setup_customizations must not recreate User PIN field")

        for fname in ("custom_override_max_attempts", "custom_override_lock_minutes"):
            cf = fields.get(f"POS Profile-{fname}", {})
            self.assertEqual(cf.get("dt"), "POS Profile", fname)
            self.assertEqual(cf.get("fieldtype"), "Int", fname)

        from ury import hooks
        fixture = next(row for row in hooks.fixtures if row.get("doctype") == "Custom Field")
        names = next(row[2] for row in fixture["filters"] if row[:2] == ["name", "in"])
        self.assertNotIn("User-custom_override_pin_hash", names)
        for name in (
            "POS Profile-custom_override_max_attempts",
            "POS Profile-custom_override_lock_minutes",
        ):
            self.assertIn(name, names)

        company_events = hooks.doc_events.get("Company", {})
        after_insert = company_events.get("after_insert")
        if isinstance(after_insert, str):
            after_insert = [after_insert]
        self.assertIn("ury.ury_pos.override.on_company_after_insert", after_insert or [])

        patches_txt = (ROOT / "ury/patches.txt").read_text()
        self.assertIn("setup_pos_manager_authorisation", patches_txt)
        self.assertIn("migrate_override_pin_out_of_user", patches_txt)

    # --- PRIVILEGES table (Amendment 1: all 35 codes) ---

    def test_privileges_define_all_35_amendment_1_codes_as_four_tuples(self):
        api = self.api()
        self.assertEqual(set(api.PRIVILEGES.keys()), EXPECTED_PRIVILEGES)
        self.assertNotIn("void_after_send", api.PRIVILEGES)
        for code in EXPECTED_PRIVILEGES:
            label, reason_req, perform_roles, authorise_roles = api.PRIVILEGES[code]
            self.assertIsInstance(label, str)
            self.assertTrue(label.strip())
            self.assertNotRegex(label, VENDOR_RE)
            self.assertIsInstance(reason_req, bool)
            self.assertIsInstance(perform_roles, (tuple, list))
            self.assertIsInstance(authorise_roles, (tuple, list))
        self.assertEqual(tuple(api.PRIVILEGES["complimentary"][2]), ())

    # --- may_perform & P1 #5 fail-closed ---

    def test_may_perform_respects_rules_narrower_profile_override_and_limits(self):
        api = self.api()
        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "void_sent_item"))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "void_sent_item"))
        self.assertTrue(api.may_perform("cashier@example.test", "Main Till", "reprint_bill"))

        self.store.privilege_rules.append(Doc(
            name="PPR-narrow-off", company="Hotel Co", pos_profile="Main Till",
            privilege="void_sent_item", role="URY Manager",
            may_perform=0, may_authorise=1, max_percent=0, max_amount=0, requires_reason=1,
            allow_self_approval=0, require_clocked_in=0, privilege_group=0,
        ))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "void_sent_item"))

        self.store.privilege_rules.append(Doc(
            name="PPR-narrow-cap", company="Hotel Co", pos_profile="Main Till",
            privilege="void_sent_item", role="URY Cashier",
            may_perform=1, may_authorise=0, max_percent=0, max_amount=50.0, requires_reason=1,
            allow_self_approval=0, require_clocked_in=0, privilege_group=0,
        ))
        self.assertTrue(api.may_perform("cashier@example.test", "Main Till", "void_sent_item", amount=50.0))
        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "void_sent_item", amount=50.01))

    def test_zero_rules_company_fails_closed_to_manager_roles_only(self):
        """P1 #5: zero rules for a company = manager roles only act alone & authorise, cashiers need PIN."""
        api = self.api()
        self.store.privilege_rules.clear()
        self.enroll("manager@example.test", "2468")

        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "void_sent_item"))
        self.assertFalse(api.may_perform("Administrator", "Main Till", "void_sent_item"))
        self.assertFalse(api.may_perform("sysmgr@example.test", "Main Till", "void_sent_item"))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "void_sent_item"))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "complimentary"))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "discount", percent=1))

        self.frappe.session.user = "cashier@example.test"
        self.refused("POS-AUTH-REQUIRED", Denied, lambda: api.require(
            "void_sent_item", "Main Till", "POS-INV-100", {"amount": 25, "reason": "Guest changed mind"}
        ))
        approvers = api.list_approvers("Main Till", "void_sent_item", reference_name="POS-INV-100")
        self.assertEqual([a["user"] for a in approvers], ["manager@example.test"])
        issued = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"amount": 25, "reason": "Guest changed mind"},
        )
        self.assertEqual(issued["approver"], "manager@example.test")

    def test_may_perform_denies_administrator_system_manager_disabled_and_other_outlet(self):
        api = self.api()
        self.assertFalse(api.may_perform("Administrator", "Main Till", "void_sent_item"))
        self.assertFalse(api.may_perform("Guest", "Main Till", "reprint_bill"))
        self.assertFalse(api.may_perform("sysmgr@example.test", "Main Till", "void_sent_item"))
        self.assertFalse(api.may_perform("disabled@example.test", "Main Till", "void_sent_item"))
        self.assertFalse(api.may_perform("other_outlet@example.test", "Main Till", "void_sent_item"))
        self.assertFalse(api.may_perform("other_co@example.test", "Main Till", "void_sent_item"))

    def test_complimentary_and_100_percent_discount_never_self_performed(self):
        api = self.api()
        self.store.approval_limits["Hotel Co"] = Doc(
            max_document_discount_percent="100",
            discount_limits_by_role="URY Manager: 100",
            discount_approver_role="URY Manager",
        )
        for r in self.store.privilege_rules:
            if r["privilege"] == "discount" and r["role"] == "URY Manager":
                r["max_percent"] = 100
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "complimentary"))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "discount", percent=100))

    def test_discount_defaults_to_zero_and_explicit_zero_rule_never_falls_back(self):
        """P1 #5: explicit max_percent=0 on POS Privilege Rule means 0%, never fallback to Approval Limit."""
        api = self.api()
        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "discount", percent=1))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "discount", percent=1))
        self.assertFalse(api.may_perform("admin_user@example.test", "Main Till", "discount", percent=1))

        # Approval Limit says Cashier=5%, Manager=25%, Approver=URY Admin.
        # Seeded rules still have max_percent=0 -> explicit 0% MUST stay 0%, NOT fall back to 5% or 25%!
        self.store.approval_limits["Hotel Co"] = Doc(
            max_document_discount_percent="",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 25\nBroken: nan\nInf: inf",
            discount_approver_role="URY Admin",
        )
        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "discount", percent=1))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "discount", percent=1))

        # Now raise rule caps: min(Approval Limit, rule) wins!
        for r in self.store.privilege_rules:
            if r["name"] == "PPR-discount-cashier":
                r["max_percent"] = 50
            elif r["privilege"] == "discount" and r["role"] == "URY Manager":
                r["max_percent"] = 25
        self.assertTrue(api.may_perform("cashier@example.test", "Main Till", "discount", percent=5))
        self.assertFalse(api.may_perform("cashier@example.test", "Main Till", "discount", percent=6))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "discount", percent=25))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "discount", percent=30))

        # Tighter profile rule caps Manager at 10% -> min(Approval Limit 25, rule 10) = 10 wins!
        self.store.privilege_rules.append(Doc(
            name="PPR-disc-mgr-tight", company="Hotel Co", pos_profile="Main Till",
            privilege="discount", role="URY Manager",
            may_perform=1, may_authorise=1, max_percent=10, max_amount=0, requires_reason=1,
            allow_self_approval=0, require_clocked_in=0, privilege_group=0,
        ))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "discount", percent=10))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "discount", percent=15))

        # Approvers for 30% discount: admin_user (discount_approver_role), NOT manager (capped at 10%)!
        self.enroll("manager@example.test", "1111")
        self.enroll("admin_user@example.test", "2222")
        approvers = api.list_approvers("Main Till", "discount", percent=30)
        self.assertEqual([a["user"] for a in approvers], ["admin_user@example.test"])

    def test_privilege_group_require_clocked_in_and_allow_self_approval_rules(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        self.enroll("manager2@example.test", "3579")

        self.store.privilege_rules.append(Doc(
            name="PPR-pg1", company="Hotel Co", pos_profile="Main Till",
            privilege="price_override", role="URY Manager",
            may_perform=1, may_authorise=1, max_percent=0, max_amount=0, requires_reason=1,
            allow_self_approval=0, require_clocked_in=0, privilege_group=1,
        ))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "price_override", privilege_group=1))
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "price_override", privilege_group=2))
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "price_override", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"privilege_group": 2, "amount": 10, "reason": "Other: Special menu"},
        ))

        self.store.privilege_rules.append(Doc(
            name="PPR-clock", company="Hotel Co", pos_profile="Main Till",
            privilege="no_sale", role="URY Manager",
            may_perform=1, may_authorise=1, max_percent=0, max_amount=0, requires_reason=1,
            allow_self_approval=0, require_clocked_in=1, privilege_group=0,
        ))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "no_sale"))

        self.store.employees["manager@example.test"] = Doc(name="EMP-MGR-1", company="Hotel Co", status="Active")
        self.store.checkins["EMP-MGR-1"] = [Doc(log_type="OUT", time=self.clock - timedelta(hours=1))]
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "no_sale"))
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "no_sale", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"reason": "Making change for guest"},
        ))
        self.store.checkins["EMP-MGR-1"].append(Doc(log_type="IN", time=self.clock - timedelta(minutes=5)))
        self.assertTrue(api.may_perform("manager@example.test", "Main Till", "no_sale"))
        res = api.authorise(
            "Main Till", "no_sale", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"reason": "Making change for guest"},
        )
        self.assertEqual(res["approver"], "manager@example.test")

        self.store.privilege_rules.append(Doc(
            name="PPR-self-ok", company="Hotel Co", pos_profile="Main Till",
            privilege="void_sent_item", role="URY Manager",
            may_perform=1, may_authorise=1, max_percent=0, max_amount=0, requires_reason=1,
            allow_self_approval=1, require_clocked_in=0, privilege_group=0,
        ))
        self.frappe.session.user = "cashier@example.test"
        r_owner = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-MGR", {"qty": 1, "reason": "Guest changed mind"},
        )
        self.assertEqual(r_owner["approver"], "manager@example.test")
        self.frappe.session.user = "manager@example.test"
        self.refused("POS-AUTH-SELF", Denied, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"qty": 1, "reason": "Guest changed mind"},
        ))

    def test_list_reasons_filters_active_reasons_by_privilege_in_sort_order(self):
        api = self.api()
        void_reasons = api.list_reasons("void_sent_item")
        self.assertEqual([r["code"] for r in void_reasons], ["VOID-GUEST", "OTHER"])
        self.assertEqual(void_reasons[1]["requires_note"], 1)

        ret_reasons = api.list_reasons("return")
        self.assertEqual([r["code"] for r in ret_reasons], ["RET-QUAL", "OTHER"])

    # --- P1 #7: Server-side requires_reason enforcement ---

    def test_requires_reason_enforced_server_side_for_authorise_and_solo_require(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")

        # Empty, unknown, disabled, and wrong-privilege reasons must all be refused.
        for bad_reason in ("", "   ", "Made up excuse", "Disabled reason", "VOID-OFF", "Quality issue", "RET-QUAL"):
            with self.subTest(reason=bad_reason):
                self.refused("POS-AUTH-REASON", Invalid, lambda r=bad_reason: api.authorise(
                    "Main Till", "void_sent_item", "manager@example.test", "2468",
                    "POS Invoice", "POS-INV-100", {"qty": 1, "reason": r},
                ))

        # Reason with requires_note=1 requires a >=4 char note.
        for short_other in ("Other", "OTHER", "Other: ", "Other: ab"):
            with self.subTest(other=short_other):
                self.refused("POS-AUTH-REASON", Invalid, lambda r=short_other: api.authorise(
                    "Main Till", "void_sent_item", "manager@example.test", "2468",
                    "POS Invoice", "POS-INV-100", {"qty": 1, "reason": r},
                ))

        # Valid Other + note succeeds.
        ok = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"qty": 1, "reason": "Other: Dropped plate at pass"},
        )
        self.assertEqual(ok["approver"], "manager@example.test")

        # Manager acting alone via require() is ALSO bound by requires_reason!
        self.frappe.session.user = "manager@example.test"
        self.refused("POS-AUTH-REASON", Invalid, lambda: api.require(
            "void_sent_item", "Main Till", "POS-INV-100", {"qty": 1, "reason": ""},
        ))
        self.refused("POS-AUTH-REASON", Invalid, lambda: api.require(
            "void_sent_item", "Main Till", "POS-INV-100", {"qty": 1, "reason": "Disabled reason"},
        ))
        self.assertIsNone(api.require(
            "void_sent_item", "Main Till", "POS-INV-100", {"qty": 1, "reason": "Guest changed mind"},
        ))

    # --- P0 #1 & P0 #2: PIN enrolment in __Auth, shared PINs, wrong password keeps session alive ---

    def test_pin_enrolment_stores_in_auth_table_never_user_and_allows_shared_pin(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        self.frappe.form_dict = {"password": "secret-pass-1", "pin": "2468", "new_pin": "2468"}
        result = api.set_my_pin("secret-pass-1", "2468")
        self.assertEqual(result, {"ok": True, "has_pin": True})
        self.assertNotIn("pin", self.frappe.form_dict)
        self.assertNotIn("new_pin", self.frappe.form_dict)
        self.assertNotIn("password", self.frappe.form_dict)
        self.assertNotIn("2468", json.dumps(result))

        # Never written onto User doc!
        self.assertNotIn("custom_override_pin_hash", self.store.users["manager@example.test"])
        hash1 = self.store.get_auth_hash("POS Override PIN", "manager@example.test", "pin_hash")
        self.assertIsNotNone(hash1)
        self.assertNotIn("2468", hash1)
        self.assertNotIn("2468", str(self.audit.call_args))

        # Second manager sets the EXACT SAME PIN "2468" -> distinct salted passlib hashes, both work!
        self.frappe.session.user = "manager2@example.test"
        api.set_my_pin("secret-pass-2", "2468")
        hash2 = self.store.get_auth_hash("POS Override PIN", "manager2@example.test", "pin_hash")
        self.assertIsNotNone(hash2)
        self.assertNotEqual(hash1, hash2)

        self.frappe.session.user = "cashier@example.test"
        r1 = api.authorise("Main Till", "void_sent_item", "manager@example.test", "2468", "POS Invoice", "POS-INV-100", {"qty": 1, "reason": "Guest changed mind"})
        r2 = api.authorise("Main Till", "void_sent_item", "manager2@example.test", "2468", "POS Invoice", "POS-INV-100", {"qty": 1, "reason": "Guest changed mind"})
        self.assertEqual(r1["approver"], "manager@example.test")
        self.assertEqual(r2["approver"], "manager2@example.test")

    def test_set_my_pin_wrong_password_raises_validation_error_and_locks_without_ending_session(self):
        """P0 #2: wrong password MUST raise ValidationError (not AuthenticationError) and count failures."""
        api = self.api()
        self.frappe.session.user = "manager@example.test"

        for _ in range(4):
            self.refused("POS-AUTH-PASSWORD", Invalid, lambda: api.set_my_pin("wrong-password", "2468"))
            self.frappe.db.rollback()
        self.assertIsNone(self.store.get_auth_hash("POS Override PIN", "manager@example.test", "pin_hash"))

        # 5th wrong password locks out set_my_pin (still ValidationError, never AuthenticationError!).
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.set_my_pin("wrong-password", "2468"))
        self.frappe.db.rollback()

        # Even correct password is refused while locked, surviving Redis flush.
        self.counts.clear()
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.set_my_pin("secret-pass-1", "2468"))

        # After lockout window expires, bad PIN formats still refused; valid PIN succeeds.
        self.clock += timedelta(minutes=15, seconds=1)
        for bad_pin in ("", "123", "123456789", "abcd", "12 34"):
            with self.subTest(pin=bad_pin):
                with self.assertRaises(Invalid):
                    api.set_my_pin("secret-pass-1", bad_pin)
        self.assertIsNone(self.store.get_auth_hash("POS Override PIN", "manager@example.test", "pin_hash"))

    def test_manager_can_clear_other_users_pin_but_cashier_cannot(self):
        api = self.api()
        self.enroll("manager2@example.test", "3579")
        self.frappe.session.user = "cashier@example.test"
        self.frappe.has_permission.return_value = False
        with self.assertRaises(Denied):
            api.clear_user_pin("manager2@example.test", "Main Till")
        self.assertIsNotNone(self.store.get_auth_hash("POS Override PIN", "manager2@example.test", "pin_hash"))

        self.frappe.session.user = "manager@example.test"
        self.frappe.has_permission.return_value = True
        cleared = api.clear_user_pin("manager2@example.test", "Main Till")
        self.assertEqual(cleared, {"ok": True, "user": "manager2@example.test", "has_pin": False})
        self.assertIsNone(self.store.get_auth_hash("POS Override PIN", "manager2@example.test", "pin_hash"))

    def install_external_pin_hooks(self, manager=None):
        self.hooks["pos_override_pin_verifier"] = "custom_app.pin.verify"
        self.hooks["__attr__:custom_app.pin.verify"] = MagicMock()
        if manager is not None:
            self.hooks["pos_override_pin_manager"] = ["custom_app.old", "", None, "custom_app.pin.manage"]
            self.hooks["__attr__:custom_app.pin.manage"] = manager

    def test_external_pin_status_reports_enrollment_reset_and_lock(self):
        user = "manager@example.test"
        self.frappe.session.user = user
        manager = MagicMock(return_value={"has_pin": 0, "must_reset": 1, "locked": 1, "secret": "2468"})
        self.install_external_pin_hooks(manager)

        self.assertEqual(self.api().my_pin_status(), {
            "has_pin": False, "must_reset": True, "locked": True,
            "external_verifier": True, "can_manage_here": True,
        })
        manager.assert_called_once_with("status", user=user)

    def test_standalone_pin_status_reports_local_enrollment(self):
        self.frappe.session.user = "manager@example.test"
        self.enroll(self.frappe.session.user, "2468")
        self.assertEqual(self.api().my_pin_status(), {
            "has_pin": True, "must_reset": False, "locked": False,
            "external_verifier": False, "can_manage_here": True,
        })

    def test_external_pin_set_checks_password_and_format_then_writes_only_to_manager(self):
        api = self.api()
        user = "manager@example.test"
        self.frappe.session.user = user
        manager = MagicMock(return_value={"ok": True})
        self.install_external_pin_hooks(manager)

        self.frappe.form_dict = {"password": "wrong", "pin": "2468"}
        self.refused("POS-AUTH-PASSWORD", Invalid, lambda: api.set_my_pin("wrong", "2468"))
        self.assertEqual(self.frappe.form_dict, {})
        with self.assertRaisesRegex(Invalid, "4 to 8 digits"):
            api.set_my_pin("secret-pass-1", "bad")
        manager.assert_not_called()

        self.assertEqual(api.set_my_pin("secret-pass-1", "2468"), {"ok": True, "has_pin": True})
        manager.assert_called_once_with("set", user=user, pin="2468")
        self.assertIsNone(self.store.get_auth_hash("POS Override PIN", user, "pin_hash"))
        self.assertEqual(self.store.committed_logs[-1].outcome, "PIN Reset")
        self.assertEqual(self.store.committed_logs[-1].approver, user)
        self.audit.assert_called_once()
        self.assertNotIn("2468", str(self.audit.call_args))

    def test_external_pin_set_keeps_db_password_lockout(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        manager = MagicMock()
        self.install_external_pin_hooks(manager)
        for _ in range(4):
            self.refused("POS-AUTH-PASSWORD", Invalid, lambda: api.set_my_pin("wrong", "2468"))
            self.frappe.db.rollback()
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.set_my_pin("wrong", "2468"))
        self.frappe.db.rollback()
        self.counts.clear()
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.set_my_pin("secret-pass-1", "2468"))
        manager.assert_not_called()

    def test_external_pin_clear_enforces_outlet_permission_and_delegates(self):
        api = self.api()
        caller = "manager@example.test"
        target = "manager2@example.test"
        self.frappe.session.user = caller
        manager = MagicMock(return_value={"ok": True})
        self.install_external_pin_hooks(manager)

        self.frappe.has_permission.return_value = False
        with self.assertRaises(Denied):
            api.clear_user_pin(target, "Main Till")
        manager.assert_not_called()

        self.frappe.has_permission.return_value = True
        with patch.object(api, "_clear_override_pin") as clear_local:
            self.assertEqual(api.clear_user_pin(target, "Main Till"), {
                "ok": True, "has_pin": False, "must_reset": True,
            })
            clear_local.assert_not_called()
        manager.assert_called_once_with("clear", user=target, caller=caller, pos_profile="Main Till")
        self.assertEqual(self.store.committed_logs[-1].outcome, "PIN Reset")
        self.assertEqual(self.store.committed_logs[-1].approver, target)

    def test_staff_pin_endpoints_require_manager_assignment_to_the_target_outlet(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        provider = MagicMock(return_value={"has_pin": True})
        self.install_external_pin_hooks(provider)
        self.frappe.has_permission.return_value = True

        for action in (
            lambda: api.list_pin_users("Pool Till"),
            lambda: api.clear_user_pin("other_outlet@example.test", "Pool Till"),
        ):
            with self.subTest(action=action):
                self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, action)
        provider.assert_not_called()

        for administrator in ("sysmgr@example.test", "Administrator"):
            with self.subTest(administrator=administrator):
                self.frappe.session.user = administrator
                self.assertEqual([row["user"] for row in api.list_pin_users("Pool Till")], [
                    "other_outlet@example.test",
                ])
                self.assertTrue(api.clear_user_pin("other_outlet@example.test", "Pool Till")["ok"])

    def test_pin_dispatcher_errors_are_sanitized_and_never_snapshotted(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        test_pin = "4829"
        self.frappe.form_dict = {"pin": test_pin, "password": "secret-pass-1"}
        self.frappe.get_hooks.side_effect = RuntimeError("Unavailable provider registry handling " + test_pin)

        def dispatch(**kwargs):
            newargs = kwargs.copy()
            return api.set_my_pin(**newargs)

        try:
            dispatch(password="secret-pass-1", pin=test_pin)
        except Exception as caught:
            self.assertIsInstance(caught, Invalid)
            self.assertIn("POS-AUTH-UNAVAILABLE", str(caught))
            self.assertNotIn(test_pin, str(caught))
            self.assertIsNone(caught.__context__)
            self.assertTrue(getattr(caught, "skip_error_log", False))
            self.assertEqual(self.frappe.form_dict, {})
        else:
            self.fail("The unexpected provider error should refuse PIN enrollment.")
        self.frappe.logger.return_value.error.assert_called_once()
        self.assertNotIn(test_pin, repr(self.frappe.logger.return_value.error.call_args))
        self.assertNotIn("Unavailable provider registry", repr(self.frappe.logger.return_value.error.call_args))

    def test_expected_pin_error_preserves_type_and_disables_developer_snapshot(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        expected = Denied("Staff profile access was denied.")
        with patch.object(api, "_get_profile", side_effect=expected):
            with self.assertRaises(Denied) as caught:
                api.authorise("Main Till", "void_sent_item", "manager2@example.test", "4829",
                              "POS Invoice", "POS-INV-100", {})
        self.assertIs(caught.exception, expected)
        self.assertTrue(getattr(caught.exception, "skip_error_log", False))
        self.frappe.logger.return_value.error.assert_not_called()

    def test_till_variance_passed_to_provider_without_a_claim_amount(self):
        api = self.api()
        reference = "OPEN-MAIN-1"
        counts = [{"mode_of_payment": "Cash", "closing_amount": 135}]
        claim = {
            "action": "over_short_close", "pos_profile": "Main Till",
            "pos_opening_entry": reference, "counted_payments": counts,
            "reason": "Other: Recount confirmed shortage",
        }
        self.install_external_pin_hooks()
        verifier = self.hooks["__attr__:custom_app.pin.verify"]
        verifier.return_value = {"ok": True}
        get_value = self.frappe.db.get_value.side_effect
        self.frappe.db.get_value.side_effect = (
            lambda doctype, *args, **kwargs: "UGX" if doctype == "Company"
            else get_value(doctype, *args, **kwargs)
        )
        till_controls = ModuleType("ury.ury_pos.till_controls")
        till_controls.over_short_approval_amount = MagicMock(return_value=15000.0)
        with patch.dict(sys.modules, {"ury.ury_pos.till_controls": till_controls}):
            issued = api.authorise("Main Till", "over_short_close", "manager@example.test", "1357",
                                   "POS Opening Entry", reference, claim)

        till_controls.over_short_approval_amount.assert_called_once()
        verifier.assert_called_once_with(
            "manager@example.test", "1357", "Main Till", amount=15000, currency="UGX",
            action="over_short_close", privilege="over_short_close",
            reference_doctype="POS Opening Entry", reference_name=reference,
            requester="cashier@example.test",
        )
        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            issued["token"], "over_short_close", "Main Till", reference, claim,
            reference_doctype="POS Opening Entry", approval_amount=20000,
        ))
        self.assertEqual(api.consume(
            issued["token"], "over_short_close", "Main Till", reference, claim,
            reference_doctype="POS Opening Entry", approval_amount=15000,
        ).approver, "manager@example.test")

    def test_consume_rejects_a_token_approved_for_a_different_document_type(self):
        api = self.api()
        manager = "manager@example.test"
        self.enroll(manager, "1357")
        claim = {"amount": 50, "reason": "Guest changed mind"}
        reference = "POS-INV-100"
        issued = api.authorise("Main Till", "void_sent_item", manager, "1357",
                               "POS Profile", reference, claim)

        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            issued["token"], "void_sent_item", "Main Till", reference, claim,
        ))
        self.assertEqual(self.store.consumed_hashes, set())
        issued = api.authorise("Main Till", "void_sent_item", manager, "1357",
                               "POS Invoice", reference, claim)
        self.assertEqual(api.consume(
            issued["token"], "void_sent_item", "Main Till", reference, claim,
            reference_doctype="POS Invoice",
        ).approver, manager)

    def test_external_pin_list_reads_status_only_for_outlet_members(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        manager = MagicMock(side_effect=lambda action, *, user: {
            "has_pin": user == "manager2@example.test",
            "must_reset": user == "cashier@example.test",
            "locked": user == "manager2@example.test",
            "secret": "2468",
        })
        self.install_external_pin_hooks(manager)
        self.frappe.has_permission.return_value = False
        with self.assertRaises(Denied):
            api.list_pin_users("Main Till")
        manager.assert_not_called()

        self.frappe.has_permission.return_value = True
        with patch.object(api, "_users_with_override_pin", return_value=set()) as list_local:
            rows = {row["user"]: row for row in api.list_pin_users("Main Till")}
            self.assertEqual(rows["manager2@example.test"], {
                "user": "manager2@example.test", "full_name": "Moses Manager",
                "has_pin": True, "must_reset": False, "locked": True,
            })
            self.assertEqual(rows["cashier@example.test"], {
                "user": "cashier@example.test", "full_name": self.store.users["cashier@example.test"].full_name,
                "has_pin": False, "must_reset": True, "locked": False,
            })
            list_local.assert_not_called()
        self.assertNotIn("other_outlet@example.test", [call.kwargs["user"] for call in manager.call_args_list])
        self.assertEqual(manager.call_count, len(rows))

    def test_locked_external_pin_owner_cannot_clear_their_own_pin(self):
        api = self.api()
        user = "manager@example.test"
        self.frappe.session.user = user
        manager = MagicMock()
        self.install_external_pin_hooks(manager)
        for _ in range(4):
            self.refused("POS-AUTH-PASSWORD", Invalid, lambda: api.set_my_pin("incorrect", "1357"))
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.set_my_pin("incorrect", "1357"))

        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.clear_user_pin(user, "Main Till"))
        manager.assert_not_called()
        tracker, _, minutes = api._password_tracker(user)
        self.assertFalse(tracker.is_user_allowed())
        self.counts.clear()
        self.assertEqual(api._db_failed_password_count(user, minutes), 5)

    def test_external_manager_clear_requires_an_explicit_outlet_profile(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        manager = MagicMock()
        self.install_external_pin_hooks(manager)
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.clear_user_pin("manager2@example.test"))
        manager.assert_not_called()

    def test_manager_hook_without_verifier_refuses_pin_management(self):
        api = self.api()
        user = "manager@example.test"
        self.frappe.session.user = user
        manager = MagicMock()
        self.enroll(user, "1357")
        self.install_external_pin_hooks(manager)
        del self.hooks["pos_override_pin_verifier"]

        with self.assertRaisesRegex(Invalid, "verification.*configured"):
            api.set_my_pin("secret-pass-1", "2468")
        with self.assertRaisesRegex(Invalid, "verification.*configured"):
            api.clear_user_pin("manager2@example.test", "Main Till")
        with self.assertRaisesRegex(Invalid, "verification.*configured"):
            api.my_pin_status()
        manager.assert_not_called()

    def test_external_pin_list_skips_provider_denials_and_keeps_other_staff(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"

        def manage(action, *, user):
            if user == "sysmgr@example.test":
                raise Denied("Provider denied the protected account")
            if user == "admin_user@example.test":
                raise PermissionError("Provider denied the protected account")
            return {"has_pin": user == "manager2@example.test"}

        self.install_external_pin_hooks(manage)
        rows = {row["user"]: row["has_pin"] for row in api.list_pin_users("Main Till")}
        self.assertEqual(rows, {
            "cashier@example.test": False,
            "manager@example.test": False,
            "manager2@example.test": True,
        })

    def test_external_pin_provider_failure_scrubs_credentials_and_error_details(self):
        api = self.api()
        self.frappe.session.user = "manager@example.test"
        current = "secret-pass-1"
        submitted = "2468"
        self.frappe.form_dict = {"pin": submitted, "current_password": current}
        provider_error = f"Third-party storage failed while processing {submitted}"
        self.install_external_pin_hooks(MagicMock(side_effect=RuntimeError(provider_error)))

        try:
            api.set_my_pin(current, submitted)
        except Exception as caught:
            self.assertEqual(self.frappe.form_dict, {})
            self.assertIsInstance(caught, Invalid)
            self.assertIn("[POS-AUTH-UNAVAILABLE]", str(caught))
            self.assertNotIn(provider_error, str(caught))
            self.assertIsNone(caught.__context__)
            trace = caught.__traceback__
            while trace:
                if trace.tb_frame.f_globals.get("__file__") == api.__file__:
                    frame_repr = repr(trace.tb_frame.f_locals)
                    self.assertNotIn(submitted, frame_repr)
                    self.assertNotIn(current, frame_repr)
                trace = trace.tb_next
        else:
            self.fail("An unavailable approval PIN provider must refuse the write.")
        self.audit.assert_not_called()

    def test_external_pin_manager_can_reverify_the_current_password(self):
        api = self.api()
        user = "manager@example.test"
        self.frappe.session.user = user
        written = []

        def manage(action, *, user, pin, current_password):
            self.assertEqual(current_password, "secret-pass-1")
            written.append((action, user, pin))
            return {"ok": True}

        self.install_external_pin_hooks(manage)
        self.assertTrue(api.set_my_pin("secret-pass-1", "2468")["ok"])
        self.assertEqual(written, [("set", user, "2468")])

    def test_unknown_external_verifier_error_is_unavailable_without_a_pin_strike(self):
        api = self.api()
        self.install_external_pin_hooks()
        verifier = self.hooks["__attr__:custom_app.pin.verify"]
        for provider_outcome in (RuntimeError("Third-party trace"), {"ok": False, "code": "Third-party code"}):
            if isinstance(provider_outcome, Exception):
                verifier.side_effect = provider_outcome
            else:
                verifier.side_effect = None
                verifier.return_value = provider_outcome
            with self.subTest(provider_outcome=provider_outcome):
                self.refused("POS-AUTH-UNAVAILABLE", Invalid, lambda: api.authorise(
                    "Main Till", "void_sent_item", "manager@example.test", "2468",
                    "POS Invoice", "POS-INV-100", {"amount": 50, "reason": "Guest changed mind"},
                ))
        self.assertFalse(any(row.outcome == "Failed PIN" for row in self.store.committed_logs))

    def test_verifier_forwards_company_amount_and_reference_to_floor_authority(self):
        api = self.api()
        requester = "cashier@example.test"
        approver = "manager@example.test"
        reference = "POS-INV-100"
        self.store.invoices[reference].update(
            currency="USD", conversion_rate=3700, rounded_total=25, grand_total=25,
        )
        get_value = self.frappe.db.get_value.side_effect
        self.frappe.db.get_value.side_effect = (
            lambda doctype, *args, **kwargs: "UGX" if doctype == "Company"
            else get_value(doctype, *args, **kwargs)
        )

        # Mirror the Floor Authority inputs forwarded by the context-aware POS hook.
        floor_authority = MagicMock(return_value={"ok": True})

        def verify(user, pin, pos_profile=None, *, requester=None, privilege=None,
                   amount=0, currency=None, action=None, reference_doctype="", reference_name=""):
            return floor_authority({
                "approver": user, "pos_profile": pos_profile, "requester": requester,
                "privilege": privilege, "amount": amount, "currency": currency,
                "action": action, "reference_doctype": reference_doctype,
                "reference_name": reference_name,
            })

        self.install_external_pin_hooks()
        self.hooks["__attr__:custom_app.pin.verify"] = verify
        claim = {
            "action": "cancel_check", "amount": 1, "currency": "USD",
            "requester": "ignored@example.test", "reason": "Guest changed mind",
        }
        issued = api.authorise("Main Till", "cancel_check", approver, "2468", "POS Invoice", reference, claim)
        self.assertEqual(issued["approver"], approver)
        floor_authority.assert_called_once_with({
            "approver": approver, "pos_profile": "Main Till", "requester": requester,
            "privilege": "cancel_check", "amount": 92500, "currency": "UGX",
            "action": "cancel_check", "reference_doctype": "POS Invoice", "reference_name": reference,
        })

    def test_verifier_only_keeps_legacy_status_and_refuses_pin_writes(self):
        api = self.api()
        user = "manager@example.test"
        self.frappe.session.user = user
        self.install_external_pin_hooks()

        for write in (
            lambda: api.set_my_pin("secret-pass-1", "2468"),
            lambda: api.clear_user_pin(user, "Main Till"),
        ):
            with self.subTest(write=write), self.assertRaisesRegex(Invalid, "cashier PIN store"):
                write()
        self.assertEqual(api.my_pin_status(), {
            "has_pin": True, "external_verifier": True, "can_manage_here": False,
        })

    # --- P1 #9: list_approvers scoping & outlet isolation ---

    def test_list_approvers_returns_names_only_excluding_requester_owner_and_ineligible(self):
        api = self.api()
        for u in ("manager@example.test", "manager2@example.test", "disabled@example.test",
                  "other_outlet@example.test", "other_co@example.test", "sysmgr@example.test"):
            self.store.set_auth("POS Override PIN", u, "pin_hash", "2468")

        self.frappe.session.user = "cashier@example.test"
        rows = api.list_approvers("Main Till", "void_sent_item", reference_name="POS-INV-MGR")
        self.assertEqual(rows, [{"user": "manager2@example.test", "full_name": "Moses Manager"}])
        for row in rows:
            self.assertEqual(set(row.keys()), {"user", "full_name"})

        self.frappe.session.user = "manager@example.test"
        rows_for_mgr = api.list_approvers("Main Till", "complimentary", reference_name="POS-INV-100")
        self.assertEqual([r["user"] for r in rows_for_mgr], ["manager2@example.test"])

    def test_list_approvers_and_authorise_refuse_cross_outlet_and_cross_company_requesters(self):
        """P1 #9 (Hydrogen #10): requester outside the profile's outlet/company cannot enumerate or authorise."""
        api = self.api()
        self.enroll("manager@example.test", "2468")

        for outsider in ("other_outlet@example.test", "other_co@example.test"):
            self.frappe.session.user = outsider
            with self.subTest(requester=outsider):
                self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.list_approvers("Main Till", "void_sent_item"))
                self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
                    "Main Till", "void_sent_item", "manager@example.test", "2468",
                    "POS Invoice", "POS-INV-100", {"qty": 1, "reason": "Guest changed mind"},
                ))

    # --- authorise adversarial cases ---

    def test_authorise_refuses_self_approval_and_check_owner_approval(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        self.frappe.session.user = "manager@example.test"
        self.refused("POS-AUTH-SELF", Denied, lambda: api.authorise(
            "Main Till", "complimentary", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"percent": 100, "reason": "Other: VIP comp"},
        ))

        self.frappe.session.user = "cashier@example.test"
        self.refused("POS-AUTH-SELF", Denied, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-MGR", {"qty": 1, "reason": "Guest changed mind"},
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
            self.store.set_auth("POS Override PIN", bad_approver, "pin_hash", "2468")
            with self.subTest(approver=bad_approver):
                self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda a=bad_approver: api.authorise(
                    "Main Till", "void_sent_item", a, "2468",
                    "POS Invoice", "POS-INV-100", {"qty": 1, "reason": "Guest changed mind"},
                ))

    def test_authorise_refuses_approver_without_pin(self):
        api = self.api()
        self.refused("POS-AUTH-NO-PIN", Invalid, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", {"qty": 1, "reason": "Guest changed mind"},
        ))

    # --- P1 #8: DB-backed lockout, profile-scoped, session rate limit ---

    def test_db_lockout_survives_redis_flush_is_profile_scoped_and_session_rate_limited(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        self.enroll("manager2@example.test", "3579")
        self.store.profiles["Main Till"]["custom_override_max_attempts"] = 3
        self.store.profiles["Main Till"]["custom_override_lock_minutes"] = 10
        self.store.profiles["Bar Till"]["custom_override_max_attempts"] = 5

        claim = {"amount": 25, "reason": "Guest changed mind"}
        for _ in range(2):
            self.frappe.form_dict = {"pin": "9999"}
            msg = self.refused("POS-AUTH-PIN", Invalid, lambda: api.authorise(
                "Main Till", "void_sent_item", "manager@example.test", "9999",
                "POS Invoice", "POS-INV-100", claim,
            ))
            self.assertNotIn("9999", msg)
            self.assertNotIn("2468", msg)
            self.assertNotIn("pin", self.frappe.form_dict)
            self.frappe.db.rollback()

        # 3rd attempt locks manager on Main Till.
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "9999",
            "POS Invoice", "POS-INV-100", claim,
        ))
        self.frappe.db.rollback()

        # Flush Redis completely -> DB-backed lockout MUST STILL block manager on Main Till!
        self.counts.clear()
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        ))

        # Profile isolation: manager is NOT locked on Bar Till (one till's low max_attempts can't lock elsewhere).
        bar_ok = api.authorise(
            "Bar Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.assertEqual(bar_ok["approver"], "manager@example.test")

        # After 10 minutes, Main Till unlocks cleanly.
        self.clock += timedelta(minutes=10, seconds=1)
        res = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.assertEqual(res["approver"], "manager@example.test")
        self.assertEqual(res["expires_in"], 120)

        # Per-session rate limit: configure tight session cap (4) so spamming across managers
        # locks the requesting session before it can lock an untouched manager2.
        self.frappe.conf["pos_override_session_max_attempts"] = 4
        self.store.profiles["Main Till"]["custom_override_max_attempts"] = 5
        for _ in range(4):
            self.refused("POS-AUTH-PIN", Invalid, lambda: api.authorise(
                "Main Till", "void_sent_item", "manager@example.test", "0001",
                "POS Invoice", "POS-INV-100", claim,
            ))
        # Now cashier session is rate-limited even when targeting a DIFFERENT manager2!
        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager2@example.test", "3579",
            "POS Invoice", "POS-INV-100", claim,
        ))

    # --- P0 #3: Impersonated, AI, and API-key sessions refused everywhere ---

    def test_authorise_consume_and_may_perform_refuse_impersonated_ai_and_api_key_sessions(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        claim = {"amount": 10, "reason": "Guest changed mind"}

        # Impersonated session (sudo) refused everywhere!
        self.frappe.session.user = "manager@example.test"
        self.frappe.session.data = {"impersonated_by": "Administrator"}
        self.assertFalse(api.may_perform("manager@example.test", "Main Till", "void_sent_item"))
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.set_my_pin("secret-pass-1", "9876"))
        self.frappe.session.user = "cashier@example.test"
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.list_approvers("Main Till", "void_sent_item"))
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        ))
        self.frappe.session.data = {}

        # Non-System User session_type also refused.
        self.frappe.session.data = {"user_type": "Website User"}
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        ))
        self.frappe.session.data = {}

        # API key & OAuth Bearer & Agent flag refused.
        self.frappe.request = SimpleNamespace(headers={"Authorization": "token abcdef:123456"}, cookies={})
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        ))
        self.frappe.request = SimpleNamespace(headers={"Authorization": "Bearer eyJ.test"}, cookies={})
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        ))
        self.frappe.request = None
        self.frappe.flags.kamra_agent_call = "MCP"
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        ))
        self.frappe.flags.kamra_agent_call = None

        # Valid interactive session mints token, then impersonation / API key at consume() is refused!
        self.frappe.request = SimpleNamespace(headers={}, cookies={"sid": "sess-cashier-1"})
        issued = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.frappe.session.data = {"impersonated_by": "Administrator"}
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.consume(
            issued["token"], "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))
        self.frappe.session.data = {}
        self.frappe.request = SimpleNamespace(headers={"Authorization": "token k:s"}, cookies={})
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.consume(
            issued["token"], "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))

    # --- P1 #9 (Hydrogen #11): external verifier error codes passed through ---

    def test_pos_override_pin_verifier_hook_replaces_builtin_store_and_passes_codes_through(self):
        api = self.api()
        called = []

        def custom_verifier(user, pin, pos_profile=None):
            called.append((user, pin, pos_profile))
            if pin == "5555":
                return {"ok": False, "code": "POS-AUTH-NOT-ELIGIBLE"}
            if pin == "6666":
                return {"ok": False, "code": "POS-AUTH-LOCKED"}
            return pin == "7788"

        self.hooks["pos_override_pin_verifier"] = ["custom_app.pin.verify"]
        self.hooks["__attr__:custom_app.pin.verify"] = custom_verifier
        claim = {"qty": 1, "reason": "Guest changed mind"}

        approvers = api.list_approvers("Main Till", "void_sent_item", reference_name="POS-INV-100")
        self.assertIn("manager@example.test", [a["user"] for a in approvers])

        # External verifier's POS-AUTH-NOT-ELIGIBLE must pass through as PermissionError without a PIN strike!
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "5555",
            "POS Invoice", "POS-INV-100", claim,
        ))
        failed_pin_logs = [r for r in self.store.auth_logs if r.get("outcome") == "Failed PIN"]
        self.assertEqual(len(failed_pin_logs), 0, "External NOT-ELIGIBLE must not record a Failed PIN strike")

        self.refused("POS-AUTH-LOCKED", Invalid, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "6666",
            "POS Invoice", "POS-INV-100", claim,
        ))
        self.refused("POS-AUTH-PIN", Invalid, lambda: api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "0000",
            "POS Invoice", "POS-INV-100", claim,
        ))
        res = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "7788",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.assertEqual(res["approver"], "manager@example.test")
        self.assertEqual(called[-1], ("manager@example.test", "7788", "Main Till"))

    # --- P1 #6: consume re-checks role/rule/outlet/enabled and survives rollback ---

    def test_consume_binds_claim_check_user_ttl_and_single_use(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        claim = {"amount": 50, "percent": 10, "reason": "Guest changed mind"}
        issued = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        token = issued["token"]
        self.assertNotIn(token, json.dumps(self.store.auth_logs, default=str))

        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            token, "void_sent_item", "Main Till", "POS-INV-100",
            {"amount": 60, "percent": 10, "reason": "Guest changed mind"},
        ))
        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            token, "void_sent_item", "Main Till", "POS-INV-OTHER", claim,
        ))
        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            token, "cancel_check", "Main Till", "POS-INV-100", claim,
        ))
        self.frappe.session.user = "manager2@example.test"
        self.refused("POS-AUTH-CHANGED", Invalid, lambda: api.consume(
            token, "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))
        self.frappe.session.user = "cashier@example.test"

        consumed = api.consume(
            token, "void_sent_item", "Main Till", "POS-INV-100",
            {"reason": "Guest changed mind", "percent": 10.0, "amount": 50.0},
        )
        self.assertEqual(consumed.approver, "manager@example.test")
        self.assertEqual(consumed.percent, 10.0)
        self.assertEqual(consumed.amount, 50.0)

        self.refused("POS-AUTH-EXPIRED", Invalid, lambda: api.consume(
            token, "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))

    def test_consume_rechecks_approver_and_requester_and_survives_rollback(self):
        """P1 #6: role/outlet revocation before consume refuses; consume survives caller rollback."""
        api = self.api()
        self.enroll("manager@example.test", "2468")
        claim = {"amount": 25, "reason": "Guest changed mind"}

        # 1. Approver loses URY Manager role mid-flow -> consume refuses POS-AUTH-NOT-ELIGIBLE!
        issued1 = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.store.user_roles["manager@example.test"] = ["URY Cashier"]
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.consume(
            issued1["token"], "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))
        self.store.user_roles["manager@example.test"] = ["URY Manager"]

        # 2. Approver removed from outlet mid-flow -> consume refuses POS-AUTH-NOT-ELIGIBLE!
        self.store.branch_users.discard(("Main Branch", "manager@example.test"))
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.consume(
            issued1["token"], "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))
        self.store.branch_users.add(("Main Branch", "manager@example.test"))

        # 3. Requester disabled mid-flow -> consume refuses POS-AUTH-NOT-ELIGIBLE!
        self.store.users["cashier@example.test"]["enabled"] = 0
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.consume(
            issued1["token"], "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))
        self.store.users["cashier@example.test"]["enabled"] = 1

        # 4. Valid consume followed by caller rollback MUST NOT allow replay!
        consumed = api.consume(issued1["token"], "void_sent_item", "Main Till", "POS-INV-100", claim)
        self.assertEqual(consumed.approver, "manager@example.test")
        self.frappe.db.rollback()
        self.refused("POS-AUTH-EXPIRED", Invalid, lambda: api.consume(
            issued1["token"], "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))

    def test_consume_refuses_expired_token_and_concurrent_unique_collision(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        claim = {"amount": 20, "reason": "Guest changed mind"}
        issued = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.clock += timedelta(seconds=121)
        self.refused("POS-AUTH-EXPIRED", Invalid, lambda: api.consume(
            issued["token"], "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))

        issued2 = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        self.store.force_consume_collision = True
        self.refused("POS-AUTH-EXPIRED", Invalid, lambda: api.consume(
            issued2["token"], "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))

    def test_require_returns_none_when_allowed_consumes_token_or_raises_required(self):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        claim = {"amount": 15, "reason": "Guest changed mind"}

        self.frappe.session.user = "manager@example.test"
        self.assertIsNone(api.require("void_sent_item", "Main Till", "POS-INV-100", claim))

        self.frappe.session.user = "cashier@example.test"
        self.refused("POS-AUTH-REQUIRED", Denied, lambda: api.require(
            "void_sent_item", "Main Till", "POS-INV-100", claim,
        ))

        issued = api.authorise(
            "Main Till", "void_sent_item", "manager@example.test", "2468",
            "POS Invoice", "POS-INV-100", claim,
        )
        approved = api.require(
            "void_sent_item", "Main Till", "POS-INV-100", claim, token=issued["token"],
        )
        self.assertEqual(approved.approver, "manager@example.test")


    def prepare_foreign_approval(self, privilege):
        api = self.api()
        self.enroll("manager@example.test", "2468")
        original_get_value = self.frappe.db.get_value

        def get_value(doctype, *args, **kwargs):
            if doctype == "Company":
                return "UGX"
            return original_get_value(doctype, *args, **kwargs)

        self.frappe.db.get_value = get_value
        profile = self.store.profiles["Main Till"]
        profile["currency"] = "USD"
        invoice = self.store.invoices["POS-INV-100"]
        invoice.update(currency="USD", conversion_rate=3700, net_total=1000, total=1000, grand_total=1000)
        invoice["items"] = [Doc(name="ROW-1", item_code="FISH", qty=5, rate=200, amount=1000, price_list_rate=200)]
        original_get_doc = self.frappe.get_doc
        self.frappe.get_doc = lambda dt, name=None, **kw: (
            invoice if dt == "POS Invoice" and name == "POS-INV-100" else original_get_doc(dt, name, **kw)
        )
        for rule in self.store.privilege_rules:
            if rule.privilege == privilege and rule.role == "URY Manager":
                rule.update(max_percent=100, max_amount=100000, requires_reason=0)
        return api

    def assert_capped_claim(self, privilege, claim, name="POS-INV-100", doctype="POS Invoice"):
        api = self.prepare_foreign_approval(privilege)
        claim.setdefault("reason", "Other: Guest request")
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", privilege, "manager@example.test", "2468", doctype, name, claim,
        ))

    def test_percentage_check_discount_issuance_is_capped(self):
        self.assert_capped_claim("discount", {"percent": 5, "amount": None, "lines": []})

    def test_percentage_line_discount_issuance_is_capped(self):
        self.assert_capped_claim("discount", {"lines": [{"row": "ROW-1", "percent": 5, "amount": None}]})

    def test_sent_reduction_issuance_is_capped_without_top_level_amount(self):
        self.assert_capped_claim("reduce_sent_qty", {
            "item_row_name": "ROW-1", "previous_qty": 5, "new_qty": 4, "delta": -1,
        })

    def test_sent_void_issuance_is_capped_without_top_level_amount(self):
        self.assert_capped_claim("void_sent_item", {
            "items": [{"item_code": "FISH", "from_qty": 5, "to_qty": 0}],
        })

    def test_new_open_item_issuance_is_capped_using_profile_fx(self):
        import sys
        from types import ModuleType

        setup_utils = ModuleType("erpnext.setup.utils")
        setup_utils.get_exchange_rate = lambda source, target, date, args=None: 3700
        with patch.dict(sys.modules, {setup_utils.__name__: setup_utils}):
            self.assert_capped_claim("open_item", {"rate": 25, "qty": 2, "amount": 50}, name="NEW")

    def test_on_account_issuance_caps_company_currency_proposed_charge(self):
        self.assert_capped_claim("on_account_over_limit", {"proposed": 185000, "currency": "UGX", "limit": 100000})

    def test_room_charge_issuance_converts_foreign_claim_amount(self):
        self.assert_capped_claim("room_charge_over_limit", {"amount": 50, "currency": "USD"})

    def test_offline_tender_issuance_converts_foreign_claim_amount(self):
        self.assert_capped_claim("offline_tender", {"amount": 50, "limit": 100000, "modes": ["Cash"]})

    def test_require_converts_document_claim_when_amount_is_omitted(self):
        api = self.prepare_foreign_approval("price_override")
        self.frappe.session.user = "manager@example.test"
        with self.assertRaisesRegex(Denied, "POS-AUTH-REQUIRED"):
            api.require("price_override", "Main Till", "POS-INV-100", {"amount": 50, "percent": 10, "reason": "Other: Guest request"})


    def test_service_charge_change_tokens_price_zero_and_nonzero_overrides(self):
        from ury.ury_pos.test_pricing_privileged_actions import FakeDoc

        api = self.prepare_foreign_approval("service_charge_waive")
        invoice = FakeDoc(self.store.invoices["POS-INV-100"])
        invoice["taxes"] = [Doc(custom_pos_charge_kind="Service Charge", tax_amount=50)]
        original_get_doc = self.frappe.get_doc
        self.frappe.get_doc = lambda dt, name=None, **kw: (
            invoice if (dt, name) == ("POS Invoice", "POS-INV-100") else original_get_doc(dt, name, **kw)
        )
        for rate, amount in ((0, None), (None, 0), (5, None)):
            with self.subTest(rate=rate, amount=amount):
                claim = {"sc_action": "change", "new_rate": rate, "new_amount": amount, "reason": "Other: Guest request"}
                self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
                    "Main Till", "service_charge_waive", "manager@example.test", "2468", "POS Invoice", "POS-INV-100", claim,
                ))

    def test_top_approver_cannot_mint_over_cap_discount_or_comp(self):
        for privilege, percent in (("discount", 5), ("complimentary", 100)):
            with self.subTest(privilege=privilege):
                api = self.prepare_foreign_approval(privilege)
                self.store.approval_limits["Hotel Co"] = Doc(
                    discount_limits_by_role="URY Manager: 100",
                    discount_approver_role="URY Manager",
                )
                claim = {"amount": 50, "percent": percent, "reason": "Other: Guest request"}
                self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
                    "Main Till", privilege, "manager@example.test", "2468", "POS Invoice", "POS-INV-100", claim,
                ))

    def test_top_approver_cannot_consume_discount_or_comp_after_cap_changes(self):
        api = self.prepare_foreign_approval("discount")
        self.store.approval_limits["Hotel Co"] = Doc(
            discount_limits_by_role="URY Manager: 100",
            discount_approver_role="URY Manager",
        )
        for privilege, percent in (("discount", 5), ("complimentary", 100)):
            with self.subTest(privilege=privilege):
                rule = next(r for r in self.store.privilege_rules if r.privilege == privilege and r.role == "URY Manager")
                rule.update(max_percent=100, max_amount=200000)
                claim = {"amount": 50, "percent": percent, "reason": "Other: Guest request"}
                issued = api.authorise(
                    "Main Till", privilege, "manager@example.test", "2468", "POS Invoice", "POS-INV-100", claim,
                )
                rule.update(max_amount=100000)
                self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.consume(
                    issued["token"], privilege, "Main Till", "POS-INV-100", claim, approval_amount=185000,
                ))


    def test_service_charge_token_uses_post_redistribution_charge(self):
        from ury.ury_pos.test_pricing_privileged_actions import FlatGrandTotalDiscountInvoice

        api = self.prepare_foreign_approval("service_charge_waive")
        profile = self.store.profiles["Main Till"]
        profile.update(custom_service_charge_account="Service Charge - HC", custom_service_charge_rate=5)
        saved_invoice = self.store.invoices["POS-INV-100"]
        doc = FlatGrandTotalDiscountInvoice(saved_invoice)
        doc.update(taxes=[], apply_discount_on="Grand Total", discount_amount=500)
        doc.calculate_taxes_and_totals()
        original_get_doc = self.frappe.get_doc
        self.frappe.get_doc = lambda dt, name=None, **kw: (
            doc if (dt, name) == ("POS Invoice", "POS-INV-100") else original_get_doc(dt, name, **kw)
        )
        for rule in self.store.privilege_rules:
            if rule.privilege == "service_charge_waive" and rule.role == "URY Manager":
                rule.max_amount = 195000

        claim = {"sc_action": "change", "new_rate": 10, "reason": "Other: Guest request"}
        self.refused("POS-AUTH-NOT-ELIGIBLE", Denied, lambda: api.authorise(
            "Main Till", "service_charge_waive", "manager@example.test", "2468", "POS Invoice", "POS-INV-100", claim,
        ))
        self.assertEqual(doc.net_total, 500)
        self.assertEqual(doc.taxes, [])


if __name__ == "__main__":
    unittest.main()
