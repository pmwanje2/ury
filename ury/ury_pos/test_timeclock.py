"""Site-less RED->GREEN unit tests for G36: POS Employee Clock-In / Clock-Out.

Covers:
- Graceful degradation when optional `hrms` app / `Employee Checkin` is absent
- Active Employee resolution scoped by Company (`[POS-TIMECARD-NO-EMPLOYEE]`)
- Outlet authorization fail-closed on status and punch calls
- Native `Employee Checkin` creation (`IN`/`OUT`, `device_id="POS:<profile>"`)
- Double IN (`[POS-TIMECARD-ALREADY-IN]`) and OUT before IN (`[POS-TIMECARD-NOT-CLOCKED-IN]`)
- Open-work guard on OUT (`[POS-TIMECARD-OPEN-WORK]`) for open draft checks and open shifts
- Opt-in `POS Profile.custom_require_clock_in_to_order` gate (`[POS-TIMECARD-CLOCK-IN-REQUIRED]`)
- Wiring `POS Privilege Rule.require_clocked_in` in `ury_pos/override.py`
- Custom field definitions & white-label compliance
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
VENDOR_RE = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala|BuildSuite)\b", re.I)


class Refused(Exception):
    pass


class NotPermitted(Exception):
    pass


class Row(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


def _stub(name, **attrs):
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


class FakeTimeclockStore:
    def __init__(self, *, hrms_installed=True):
        self.hrms_installed = hrms_installed
        self.now = datetime(2026, 11, 24, 10, 30, 0)
        self.profiles = {
            "Main Grill POS": Row(
                name="Main Grill POS",
                branch="Main Grill",
                company="Maximus Hospitality",
                restaurant="Main Grill",
                currency="UGX",
                disabled=0,
                custom_require_clock_in_to_order=0,
                custom_block_clock_out_with_open_work=1,
                applicable_for_users=[Row(user="waiter@example.test"), Row(user="cashier@example.test")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
            ),
            "Pool Bar POS": Row(
                name="Pool Bar POS",
                branch="Pool Bar",
                company="Maximus Hospitality",
                restaurant="Pool Bar",
                currency="USD",
                disabled=0,
                custom_require_clock_in_to_order=1,
                custom_block_clock_out_with_open_work=1,
                applicable_for_users=[Row(user="other@example.test")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
            ),
        }
        self.branch_users = {
            "Main Grill": ["waiter@example.test", "cashier@example.test"],
            "Pool Bar": ["other@example.test"],
        }
        self.user_permissions = {
            "waiter@example.test": {"Branch": [{"doc": "Main Grill", "is_default": 1}]},
            "cashier@example.test": {"Branch": [{"doc": "Main Grill", "is_default": 1}]},
        }
        self.roles = {
            "waiter@example.test": ["URY Waiter", "Employee"],
            "cashier@example.test": ["URY Cashier", "Employee"],
            "other@example.test": ["URY Cashier", "Employee"],
            "no_emp@example.test": ["URY Waiter"],
        }
        self.employees = {
            ("waiter@example.test", "Maximus Hospitality"): Row(
                name="HR-EMP-0001", employee_name="Grace Namono", user_id="waiter@example.test",
                company="Maximus Hospitality", status="Active",
            ),
            ("cashier@example.test", "Maximus Hospitality"): Row(
                name="HR-EMP-0002", employee_name="Daniel Kato", user_id="cashier@example.test",
                company="Maximus Hospitality", status="Active",
            ),
        }
        self.checkins: list[Row] = []
        self.open_invoices: list[Row] = []
        self.open_shifts: list[Row] = []
        self.checkin_insert_permission_error = False
        self.cache_store: dict[str, object] = {}


def _build_frappe_for_timeclock(store: FakeTimeclockStore, user="waiter@example.test"):
    frappe = MagicMock()
    frappe.PermissionError = NotPermitted
    frappe.ValidationError = Refused
    frappe.DoesNotExistError = Refused
    frappe._ = lambda msg, *a: msg
    frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
    frappe.session = SimpleNamespace(user=user, sid="sid-tc-1")
    frappe.request = SimpleNamespace(headers={}, cookies={"sid": "sid-tc-1"})
    frappe.flags = Row()
    frappe.conf = Row()

    def _throw(msg, exc=None, **kwargs):
        raise (exc or Refused)(msg)

    frappe.throw = _throw
    frappe.get_installed_apps = lambda: (["frappe", "erpnext", "hrms", "ury"] if store.hrms_installed else ["frappe", "erpnext", "ury"])
    frappe.get_roles = lambda u=None: list(store.roles.get(u or frappe.session.user, []))

    def _table_exists(dt):
        if dt == "Employee Checkin":
            return store.hrms_installed
        return True

    def _exists(dt, filt=None):
        if dt == "DocType" and filt == "Employee Checkin":
            return store.hrms_installed
        if dt == "POS Profile":
            return filt in store.profiles
        if dt == "URY User" and isinstance(filt, dict):
            return filt.get("user") in store.branch_users.get(filt.get("parent"), [])
        if dt == "Employee Checkin" and isinstance(filt, dict):
            return any(c.employee == filt.get("employee") for c in store.checkins)
        return False

    def _get_value(dt, filt, fieldname=None, as_dict=False, order_by=None, for_update=False):
        if dt == "User":
            if fieldname == "enabled":
                return 1
            if fieldname == "full_name":
                return filt
        if dt == "Employee" and isinstance(filt, dict):
            emp = store.employees.get((filt.get("user_id"), filt.get("company")))
            if not emp and not filt.get("company"):
                emp = next((e for (u, _c), e in store.employees.items() if u == filt.get("user_id")), None)
            if not emp or (filt.get("status") and emp.status != filt["status"]):
                return None
            if isinstance(fieldname, list):
                return Row({k: emp.get(k) for k in fieldname}) if as_dict else tuple(emp.get(k) for k in fieldname)
            return emp.get(fieldname) if fieldname else emp.name
        if dt == "Employee Checkin" and isinstance(filt, dict):
            matches = [c for c in store.checkins if c.employee == filt.get("employee")]
            if not matches:
                return None
            matches.sort(key=lambda c: c.time, reverse=True)
            top = matches[0]
            if isinstance(fieldname, list):
                return Row({k: top.get(k) for k in fieldname}) if as_dict else tuple(top.get(k) for k in fieldname)
            return top.get(fieldname)
        if dt == "POS Profile" and isinstance(filt, str):
            prof = store.profiles.get(filt)
            if not prof:
                return None
            if isinstance(fieldname, list):
                return Row({k: prof.get(k) for k in fieldname}) if as_dict else tuple(prof.get(k) for k in fieldname)
            return prof.get(fieldname)
        return None

    def _get_all(dt, filters=None, fields=None, order_by=None, limit=None, limit_page_length=None, pluck=None):
        filters = filters or {}
        if dt == "POS Profile":
            rows = [p for p in store.profiles.values() if not filters.get("disabled") == 0 or not p.disabled]
            if "disabled" in filters:
                rows = [p for p in store.profiles.values() if int(p.disabled or 0) == int(filters["disabled"])]
            return rows
        if dt == "POS Profile User":
            out = []
            for pname, prof in store.profiles.items():
                for urow in prof.applicable_for_users:
                    out.append(Row(parent=pname, user=urow.user))
            return out
        if dt == "Employee Checkin":
            matches = [c for c in store.checkins if c.employee == filters.get("employee")]
            matches.sort(key=lambda c: c.time, reverse=True)
            if limit:
                matches = matches[:limit]
            return matches
        if dt == "POS Invoice":
            matches = []
            for inv in store.open_invoices:
                if filters.get("docstatus") is not None and inv.docstatus != filters["docstatus"]:
                    continue
                if filters.get("waiter") and inv.waiter != filters["waiter"]:
                    continue
                if filters.get("branch") and inv.branch != filters["branch"]:
                    continue
                if filters.get("company") and inv.company != filters["company"]:
                    continue
                matches.append(inv)
            return [m.name for m in matches] if pluck == "name" else matches
        if dt == "POS Opening Entry":
            matches = []
            for s in store.open_shifts:
                if filters.get("docstatus") is not None and s.docstatus != filters["docstatus"]:
                    continue
                if filters.get("status") and s.status != filters["status"]:
                    continue
                if filters.get("user") and s.user != filters["user"]:
                    continue
                if filters.get("company") and s.company != filters["company"]:
                    continue
                matches.append(s)
            return [m.name for m in matches] if pluck == "name" else matches
        return []

    def _sql(query, values=None, as_dict=False):
        if "tabURY User" in query:
            u = values[0] if isinstance(values, (list, tuple)) else values
            rows = []
            for br, users in store.branch_users.items():
                if u in users:
                    rows.append(Row(branch=br, room="Room 1"))
            return rows
        return []

    def _get_doc(arg, name=None):
        if isinstance(arg, dict):
            doc = Row(arg)
            doc.name = f"EMP-CKIN-{len(store.checkins) + 1:04d}"
            doc.shift = "Morning Shift"

            def _insert(ignore_permissions=False):
                if store.checkin_insert_permission_error and not ignore_permissions:
                    raise NotPermitted("No direct create permission on Employee Checkin")
                store.checkins.append(doc)
                return doc

            doc.insert = _insert
            doc.fetch_shift = MagicMock()
            return doc
        if arg == "POS Profile":
            if name not in store.profiles:
                raise Refused(f"POS Profile {name} not found")
            return store.profiles[name]
        raise Refused(f"Unknown doc {arg} {name}")

    frappe.db = MagicMock()
    frappe.db.table_exists = _table_exists
    frappe.db.exists = _exists
    frappe.db.get_value = _get_value
    frappe.db.sql = _sql
    frappe.get_all = _get_all
    frappe.get_doc = _get_doc
    frappe.get_cached_doc = _get_doc
    frappe.cache = MagicMock()
    frappe.cache.get_value = lambda k: store.cache_store.get(k)
    frappe.cache.set_value = lambda k, v, expires_in_sec=None: store.cache_store.__setitem__(k, v)
    frappe.cache.delete_value = lambda k: store.cache_store.pop(k, None)

    utils = _stub(
        "frappe.utils",
        cint=lambda v: int(v or 0),
        flt=lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0),
        now_datetime=lambda: store.now,
        get_datetime=lambda v: v if isinstance(v, datetime) else datetime.fromisoformat(str(v)),
    )
    frappe.utils = utils
    return frappe, utils


def _load_module(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestTimeclock(unittest.TestCase):
    def _env(self, *, hrms_installed=True, user="waiter@example.test"):
        store = FakeTimeclockStore(hrms_installed=hrms_installed)
        frappe, utils = _build_frappe_for_timeclock(store, user=user)
        user_perm_mod = _stub(
            "frappe.core.doctype.user_permission.user_permission",
            get_user_permissions=lambda u=None: store.user_permissions.get(u or frappe.session.user, {}),
        )
        self.enterContext(
            patch.dict(
                sys.modules,
                {
                    "frappe": frappe,
                    "frappe.utils": utils,
                    "frappe.core": _stub("frappe.core"),
                    "frappe.core.doctype": _stub("frappe.core.doctype"),
                    "frappe.core.doctype.user_permission": _stub("frappe.core.doctype.user_permission"),
                    "frappe.core.doctype.user_permission.user_permission": user_perm_mod,
                },
            )
        )
        _load_module("ury.brand", "ury/brand.py")
        outlet_ctx = _load_module("ury.ury_pos.outlet_context", "ury/ury_pos/outlet_context.py")
        tc = _load_module("ury.ury_pos.timeclock", "ury/ury_pos/timeclock.py")
        return store, frappe, tc, outlet_ctx

    def test_gracefully_unavailable_when_hrms_absent(self):
        store, _frappe, tc, _oc = self._env(hrms_installed=False)
        store.profiles["Main Grill POS"].custom_require_clock_in_to_order = 1

        status = tc.get_my_timeclock_status("Main Grill POS")
        self.assertEqual(status, {"available": False})
        # Order entry must NOT be blocked when hrms is not installed
        tc.enforce_clock_in_for_profile(store.profiles["Main Grill POS"], "waiter@example.test")

        with self.assertRaises(Refused) as ctx:
            tc.punch_timeclock("IN", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-UNAVAILABLE]", str(ctx.exception))
        self.assertNotRegex(str(ctx.exception), VENDOR_RE)

    def test_punch_in_order_gate_and_open_work_block_on_clock_out(self):
        store, _frappe, tc, _oc = self._env(hrms_installed=True)
        store.profiles["Main Grill POS"].custom_require_clock_in_to_order = 1

        # Before IN punch: status clocked out, order entry refused
        status = tc.get_my_timeclock_status("Main Grill POS")
        self.assertTrue(status["available"])
        self.assertFalse(status["is_clocked_in"])
        self.assertEqual(status["employee"], "HR-EMP-0001")

        with self.assertRaises(Refused) as ctx:
            tc.enforce_clock_in_for_profile(store.profiles["Main Grill POS"], "waiter@example.test")
        self.assertIn("[POS-TIMECARD-CLOCK-IN-REQUIRED]", str(ctx.exception))
        self.assertNotRegex(str(ctx.exception), VENDOR_RE)

        # OUT before IN refused
        with self.assertRaises(Refused) as ctx:
            tc.punch_timeclock("OUT", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-NOT-CLOCKED-IN]", str(ctx.exception))

        # Punch IN creates native Employee Checkin with device_id="POS:<profile>"
        res = tc.punch_timeclock("IN", "Main Grill POS")
        self.assertTrue(res["is_clocked_in"])
        self.assertEqual(len(store.checkins), 1)
        self.assertEqual(store.checkins[0].log_type, "IN")
        self.assertEqual(store.checkins[0].device_id, "POS:Main Grill POS")
        self.assertEqual(store.checkins[0].employee, "HR-EMP-0001")

        # Now order entry passes
        tc.enforce_clock_in_for_profile(store.profiles["Main Grill POS"], "waiter@example.test")

        # Double IN refused
        with self.assertRaises(Refused) as ctx:
            tc.punch_timeclock("IN", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-ALREADY-IN]", str(ctx.exception))

        # Open draft check blocks OUT
        store.open_invoices.append(
            Row(name="POS-INV-0042", docstatus=0, waiter="waiter@example.test", branch="Main Grill", company="Maximus Hospitality")
        )
        with self.assertRaises(Refused) as ctx:
            tc.punch_timeclock("OUT", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-OPEN-WORK]", str(ctx.exception))
        self.assertIn("POS-INV-0042", str(ctx.exception))

        # Clearing open check but leaving an open shift also blocks OUT
        store.open_invoices.clear()
        store.open_shifts.append(
            Row(name="POS-OPE-0009", docstatus=1, status="Open", user="waiter@example.test", company="Maximus Hospitality")
        )
        with self.assertRaises(Refused) as ctx:
            tc.punch_timeclock("OUT", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-OPEN-WORK]", str(ctx.exception))
        self.assertIn("POS-OPE-0009", str(ctx.exception))

        # Closing open shift allows OUT
        store.open_shifts.clear()
        store.now = store.now + timedelta(hours=4)
        out_res = tc.punch_timeclock("OUT", "Main Grill POS")
        self.assertFalse(out_res["is_clocked_in"])
        self.assertEqual(len(store.checkins), 2)
        self.assertEqual(store.checkins[1].log_type, "OUT")
        self.assertEqual(store.checkins[1].device_id, "POS:Main Grill POS")

    def test_missing_employee_and_cross_outlet_access_refused(self):
        store, frappe, tc, _oc = self._env(hrms_installed=True, user="waiter@example.test")

        # Cross-outlet attempt on Pool Bar POS fails closed with PermissionError
        with self.assertRaises(NotPermitted) as ctx:
            tc.punch_timeclock("IN", "Pool Bar POS")
        self.assertIn("[POS-OUTLET-FORBIDDEN]", str(ctx.exception))

        # User without Active Employee refused with [POS-TIMECARD-NO-EMPLOYEE]
        store.branch_users["Main Grill"].append("no_emp@example.test")
        store.profiles["Main Grill POS"].applicable_for_users.append(Row(user="no_emp@example.test"))
        store.user_permissions["no_emp@example.test"] = {"Branch": [{"doc": "Main Grill", "is_default": 1}]}
        frappe.session.user = "no_emp@example.test"

        with self.assertRaises(Refused) as ctx:
            tc.punch_timeclock("IN", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-NO-EMPLOYEE]", str(ctx.exception))

    def test_employee_fallback_permission_path_for_self_checkin(self):
        store, _frappe, tc, _oc = self._env(hrms_installed=True)
        store.checkin_insert_permission_error = True
        res = tc.punch_timeclock("IN", "Main Grill POS")
        self.assertTrue(res["is_clocked_in"])
        self.assertEqual(len(store.checkins), 1)

    def test_override_require_clocked_in_uses_timeclock_module(self):
        store, _frappe, tc, _oc = self._env(hrms_installed=True)
        # No checkins yet when hrms is installed -> NOT clocked in
        self.assertFalse(tc.is_user_clocked_in("waiter@example.test", company="Maximus Hospitality"))
        tc.punch_timeclock("IN", "Main Grill POS")
        self.assertTrue(tc.is_user_clocked_in("waiter@example.test", company="Maximus Hospitality"))

        override_src = (ROOT / "ury/ury_pos/override.py").read_text(encoding="utf-8")
        self.assertIn("timeclock", override_src)
        self.assertIn("is_user_clocked_in", override_src)

    def test_custom_fields_and_hooks_ast_clean(self):
        fields_mod = _load_module("ury.ury_pos.timeclock_tips_fields", "ury/ury_pos/timeclock_tips_fields.py")
        defs = fields_mod.get_timeclock_tips_custom_fields()
        profile_fnames = {f["fieldname"] for f in defs["POS Profile"]}
        self.assertTrue(
            {"custom_require_clock_in_to_order", "custom_block_clock_out_with_open_work", "custom_require_tip_declaration"}.issubset(
                profile_fnames
            )
        )
        # Verify hooks.py has zero duplicate dict keys
        tree = ast.parse((ROOT / "ury/hooks.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                str_keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                self.assertEqual(len(str_keys), len(set(str_keys)), f"Duplicate dict keys in hooks.py: {str_keys}")


if __name__ == "__main__":
    unittest.main()
