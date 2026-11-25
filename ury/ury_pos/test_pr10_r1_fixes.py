"""Site-less RED->GREEN regression tests for Hydrogen R1 review findings on PR #157.

Covers:
- [P1-1] Safe PermissionError handling in `api.py` and `timeclock.py` when `frappe` is stubbed/mocked
- [P1-2] `ury/hooks.py` `doctype_js["POS Closing Entry"]` resolves to `ury/public/js/pos_closing_entry.js`
- [P1-3a] `G36` clock-in gate enforced on `split_bill`, `merge_bills`, `table_transfer`, `reduce_order_item_qty`
- [P1-3b] `_open_work_for_user` blocks cashier-owned (`waiter=""`, `owner`/`cashier=user`) and cross-outlet open drafts
- [P1-6] `G44` `shift_report_available` checks Report read, `POS Invoice` report, and report role (`Custom Role`/`Has Role`)
- [P2-2] `create_tip_additional_salaries` acquires `FOR UPDATE` lock on `POS Closing Entry` and maps deadlocks to 417
- [#151+#157] `close_till_with_controls` persists `tip_declarations` onto `POS Closing Entry.custom_tip_declarations`
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
VENDOR_RE = re.compile(r"\b(?:URY|Frappe|ERPNext|Kamra|HUF|Tridz|HeyKoala|BuildSuite)\b", re.I)


class Refused(Exception):
    pass


class NotPermitted(Exception):
    pass


class QueryDeadlockError(Exception):
    pass


class Row(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


class Doc:
    """Attribute-backed fake Document so `.items` never collides with `dict.items()`."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def get(self, key, default=None):
        return self.__dict__.get(key, default)


def _stub(name, **attrs):
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _load_module(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestP1_1SafePermissionErrorCatch(unittest.TestCase):
    def test_api_handles_mocked_frappe_without_base_exception_type_error(self):
        """[P1-1] Stubbing/mocking `frappe` where `frappe.PermissionError` is a MagicMock
        must NEVER raise `TypeError: catching classes that do not inherit from BaseException`."""
        fake_frappe = MagicMock()
        fake_frappe._ = lambda m, *a: m
        fake_frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
        fake_frappe.utils = _stub(
            "frappe.utils",
            cint=lambda v: int(v or 0),
            flt=lambda v, p=None: float(v or 0),
            get_datetime=lambda v=None: "2026-11-24 10:00:00",
            getdate=lambda v=None: "2026-11-24",
            nestedset=MagicMock(),
            validate_phone_number=MagicMock(),
        )
        # Deliberately keep fake_frappe.PermissionError as a MagicMock (not BaseException)
        self.assertFalse(isinstance(fake_frappe.PermissionError, type))

        # outlet_context fails with AttributeError (like site-less test_merged_bill_submit_efris)
        broken_oc = _stub(
            "ury.ury_pos.outlet_context",
            resolve_session_selected_branch=MagicMock(side_effect=AttributeError("no get_all")),
            get_authorized_outlets=MagicMock(side_effect=AttributeError("no get_all")),
            resolve_active_pos_profile=MagicMock(side_effect=AttributeError("no get_all")),
            assert_outlet_access=MagicMock(side_effect=AttributeError("no get_all")),
            get_business_date_and_report_meta=MagicMock(side_effect=AttributeError("no get_all")),
        )

        with patch.dict(
            sys.modules,
            {
                "frappe": fake_frappe,
                "frappe.utils": fake_frappe.utils,
                "ury.ury_pos.outlet_context": broken_oc,
                "ury.ury_pos.default_customer": _stub(
                    "ury.ury_pos.default_customer", get_default_customer_config=lambda p: {}
                ),
                "ury.ury.api.item_price": _stub(
                    "ury.ury.api.item_price", item_price_release_states=MagicMock(), price_valid_on=MagicMock()
                ),
            },
        ):
            _load_module("ury.brand", "ury/brand.py")
            api = _load_module("ury.ury_pos.api", "ury/ury_pos/api.py")
            fake_frappe.session.user = "cashier@example.test"
            fake_frappe.db.sql.return_value = [Row(branch="Main Grill", room="Room 1")]
            # Must NOT raise TypeError
            rows = api._user_branch_assignments()
            self.assertEqual(rows[0]["branch"], "Main Grill")

            # Also verify getPosProfile falls through cleanly without TypeError
            fake_frappe.db.exists.return_value = "Main Grill POS"
            fake_frappe.db.get_value.return_value = None
            fake_frappe.get_roles.return_value = ["URY Cashier"]
            fake_frappe.get_single.return_value = Row(disable_rounded_total=0)
            fake_frappe.get_meta.return_value = MagicMock(has_field=lambda _f: False)
            profile = Row(
                name="Main Grill POS",
                branch="Main Grill",
                company="Maximus Hospitality",
                restaurant="Main Grill",
                currency="UGX",
                country="Uganda",
                warehouse="Stores",
                print_format="Receipt",
                qz_print=0,
                qz_host=None,
                table_attention_time=10,
                paid_limit=0,
                custom_enable_discount=0,
                custom_enable_multiple_cashier=0,
                custom_edit_order_type=0,
                custom_enable_kot_reprint=0,
                custom_invoice_warning_time=0,
                applicable_for_users=[Row(user="cashier@example.test")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
                printer_settings=[],
                payments=[],
            )
            fake_frappe.get_doc.return_value = profile
            with patch.dict(
                sys.modules,
                {
                    "ury.ury_pos.offline_tenders": _stub(
                        "ury.ury_pos.offline_tenders",
                        offline_tender_limit=lambda _p: 0,
                        profile_offline_payment_modes=lambda _p, **_kw: [],
                    ),
                },
            ):
                res = api.getPosProfile()
            self.assertEqual(res["pos_profile"], "Main Grill POS")


class TestP1_2HooksDeskScriptPath(unittest.TestCase):
    def test_pos_closing_entry_doctype_js_path_resolves_cleanly(self):
        """[P1-2] `ury/hooks.py` must register `public/js/pos_closing_entry.js` (no double `ury/` prefix)."""
        tree = ast.parse((ROOT / "ury/hooks.py").read_text(encoding="utf-8"))
        doctype_js_val = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "doctype_js":
                        doctype_js_val = ast.literal_eval(node.value)
        self.assertIsInstance(doctype_js_val, dict)
        self.assertIn("POS Closing Entry", doctype_js_val)
        entry = doctype_js_val["POS Closing Entry"]
        self.assertEqual(entry, "public/js/pos_closing_entry.js")
        self.assertTrue((ROOT / "ury" / entry).is_file(), f"Missing Desk script at ury/{entry}")


class TestP1_3TimeclockEnforcementAndOpenWork(unittest.TestCase):
    def _build_env(self, *, user="cashier@example.test", clocked_in=False):
        store = SimpleNamespace(
            hrms_installed=True,
            now=datetime(2026, 11, 24, 11, 0, 0),
            profiles={
                "Main Grill POS": Row(
                    name="Main Grill POS",
                    branch="Main Grill",
                    company="Maximus Hospitality",
                    restaurant="Main Grill",
                    currency="UGX",
                    disabled=0,
                    custom_require_clock_in_to_order=1,
                    custom_block_clock_out_with_open_work=1,
                    custom_qty_reduction_allowed_order_types="Dine In,Take Away",
                    custom_kot_naming_series="KOT-.#####",
                    applicable_for_users=[Row(user="cashier@example.test"), Row(user="waiter@example.test")],
                    role_allowed_for_billing=[Row(role="URY Cashier")],
                    transfer_role_permissions=[Row(role="URY Cashier"), Row(role="URY Waiter")],
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
                    applicable_for_users=[Row(user="cashier@example.test"), Row(user="waiter@example.test")],
                    role_allowed_for_billing=[Row(role="URY Cashier")],
                    transfer_role_permissions=[Row(role="URY Cashier"), Row(role="URY Waiter")],
                ),
            },
            branch_users={
                "Main Grill": ["cashier@example.test", "waiter@example.test"],
                "Pool Bar": ["cashier@example.test", "waiter@example.test"],
            },
            user_permissions={
                "cashier@example.test": {"Branch": [{"doc": "Main Grill", "is_default": 1}, {"doc": "Pool Bar", "is_default": 0}]},
                "waiter@example.test": {"Branch": [{"doc": "Main Grill", "is_default": 1}, {"doc": "Pool Bar", "is_default": 0}]},
            },
            employees={
                ("cashier@example.test", "Maximus Hospitality"): Row(
                    name="HR-EMP-0001", employee_name="Cashier One", user_id="cashier@example.test",
                    company="Maximus Hospitality", status="Active",
                ),
                ("waiter@example.test", "Maximus Hospitality"): Row(
                    name="HR-EMP-0002", employee_name="Waiter Two", user_id="waiter@example.test",
                    company="Maximus Hospitality", status="Active",
                ),
            },
            checkins=[],
            invoices={},
            tables={
                "T-1": Row(name="T-1", branch="Main Grill", occupied=1),
                "T-2": Row(name="T-2", branch="Main Grill", occupied=0),
            },
            open_invoices=[],
            open_shifts=[],
            cache_store={},
        )
        if clocked_in:
            emp = store.employees[(user, "Maximus Hospitality")]
            store.checkins.append(Row(name="CK-1", employee=emp.name, log_type="IN", time=store.now))

        frappe = MagicMock()
        frappe.PermissionError = NotPermitted
        frappe.ValidationError = Refused
        frappe.DoesNotExistError = Refused
        frappe._ = lambda m, *a: m
        frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
        frappe.session = SimpleNamespace(user=user, sid="sid-r1")
        frappe.request = SimpleNamespace(headers={}, cookies={"sid": "sid-r1"})
        frappe.flags = Row()
        frappe.conf = Row()
        frappe.throw = lambda msg, exc=None, **kw: (_ for _ in ()).throw((exc or Refused)(msg))
        frappe.get_installed_apps = lambda: ["frappe", "erpnext", "hrms", "ury"]
        frappe.get_roles = lambda u=None: ["URY Cashier", "URY Waiter", "Employee"]
        frappe.has_permission = lambda dt, ptype="read", doc=None, user=None: True
        frappe._dict = lambda *a, **kw: Row(*a, **kw)

        def _exists(dt, filt=None):
            if dt == "DocType" and filt == "Employee Checkin":
                return True
            if dt == "POS Invoice":
                return filt in store.invoices
            if dt == "POS Profile":
                return filt in store.profiles
            return False

        def _get_value(dt, filt, fieldname=None, as_dict=False, order_by=None):
            if dt == "User" and fieldname == "enabled":
                return 1
            if dt == "Employee" and isinstance(filt, dict):
                emp = store.employees.get((filt.get("user_id"), filt.get("company")))
                if not emp and not filt.get("company"):
                    emp = next((e for (u, _c), e in store.employees.items() if u == filt.get("user_id")), None)
                if not emp:
                    return None
                if isinstance(fieldname, list):
                    return Row({k: emp.get(k) for k in fieldname}) if as_dict else tuple(emp.get(k) for k in fieldname)
                return emp.get(fieldname) if fieldname else emp.name
            if dt == "POS Profile" and isinstance(filt, str):
                prof = store.profiles.get(filt)
                if not prof:
                    return None
                if isinstance(fieldname, list):
                    return Row({k: prof.get(k) for k in fieldname}) if as_dict else tuple(prof.get(k) for k in fieldname)
                return prof.get(fieldname)
            if dt == "Employee Checkin" and isinstance(filt, dict):
                matches = [c for c in store.checkins if c.employee == filt.get("employee")]
                return matches[-1].get(fieldname) if matches else None
            if dt == "Branch" and fieldname == "company":
                return "Maximus Hospitality"
            return None

        def _matches_clause(row, field, op, val):
            actual = row.get(field)
            if op == "=":
                return actual == val
            if op == "!=":
                return actual != val
            return False

        def _get_all(dt, filters=None, or_filters=None, fields=None, order_by=None, limit=None, limit_page_length=None, pluck=None):
            filters = filters or {}
            if dt == "POS Profile":
                return [p for p in store.profiles.values() if int(p.disabled or 0) == 0]
            if dt == "POS Profile User":
                return [Row(parent=pn, user=u.user) for pn, p in store.profiles.items() for u in p.applicable_for_users]
            if dt == "Employee Checkin":
                matches = [c for c in store.checkins if c.employee == filters.get("employee")]
                matches.sort(key=lambda c: c.time, reverse=True)
                return matches[:limit] if limit else matches
            if dt == "POS Invoice":
                matches = []
                for inv in store.open_invoices:
                    ok = True
                    if isinstance(filters, dict):
                        for k, v in filters.items():
                            if inv.get(k) != v:
                                ok = False
                                break
                    if not ok:
                        continue
                    if or_filters:
                        or_ok = False
                        if isinstance(or_filters, dict):
                            or_ok = any(inv.get(k) == v for k, v in or_filters.items())
                        elif isinstance(or_filters, (list, tuple)):
                            for clause in or_filters:
                                if isinstance(clause, (list, tuple)) and len(clause) >= 4:
                                    if _matches_clause(inv, clause[1], clause[2], clause[3]):
                                        or_ok = True
                                        break
                                elif isinstance(clause, (list, tuple)) and len(clause) == 3:
                                    if _matches_clause(inv, clause[0], clause[1], clause[2]):
                                        or_ok = True
                                        break
                        if not or_ok:
                            continue
                    matches.append(inv)
                return [m.name for m in matches] if pluck == "name" else matches
            if dt == "POS Opening Entry":
                matches = [
                    s for s in store.open_shifts
                    if s.docstatus == filters.get("docstatus", 1)
                    and (not filters.get("status") or s.status == filters["status"])
                    and (not filters.get("user") or s.user == filters["user"])
                    and (not filters.get("company") or s.company == filters["company"])
                ]
                return [m.name for m in matches] if pluck == "name" else matches
            return []

        def _sql(query, values=None, as_dict=False):
            if "tabURY User" in query:
                u = values[0] if isinstance(values, (list, tuple)) else values
                return [Row(branch=br, room="Room 1") for br, users in store.branch_users.items() if u in users]
            return []

        def _get_doc(arg, name=None, for_update=False):
            if isinstance(arg, dict):
                doc = Row(arg)
                doc.name = f"EMP-CKIN-{len(store.checkins) + 1:04d}"
                doc.insert = lambda ignore_permissions=False: (store.checkins.append(doc) or doc)
                doc.fetch_shift = MagicMock()
                return doc
            if arg == "POS Profile":
                return store.profiles[name]
            if arg == "POS Invoice":
                return store.invoices[name]
            if arg == "URY Table":
                return store.tables[name]
            raise Refused(f"Unknown doc {arg} {name}")

        frappe.db = MagicMock()
        frappe.db.table_exists = lambda dt: True
        frappe.db.exists = _exists
        frappe.db.get_value = _get_value
        frappe.db.sql = _sql
        frappe.get_all = _get_all
        frappe.get_doc = _get_doc
        frappe.cache = MagicMock()
        frappe.cache.get_value = lambda k: store.cache_store.get(k)
        frappe.cache.set_value = lambda k, v, expires_in_sec=None: store.cache_store.__setitem__(k, v)
        frappe.cache.delete_value = lambda k: store.cache_store.pop(k, None)

        utils = _stub(
            "frappe.utils",
            cint=lambda v: int(v or 0),
            flt=lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0),
            now_datetime=lambda: store.now,
            get_datetime=lambda v=None: v if isinstance(v, datetime) else datetime.fromisoformat(str(v or "2026-11-24T11:00:00")),
            get_system_timezone=lambda: "Africa/Kampala",
            getdate=lambda v=None: "2026-11-24",
            nestedset=MagicMock(),
            validate_phone_number=MagicMock(),
        )
        frappe.utils = utils
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
                    "ury.ury_pos.default_customer": _stub("ury.ury_pos.default_customer", get_default_customer_config=lambda p: {}),
                    "ury.ury.api.item_price": _stub("ury.ury.api.item_price", item_price_release_states=MagicMock(), price_valid_on=MagicMock()),
                    "ury.ury.api.ury_kot_generate": _stub("ury.ury.api.ury_kot_generate", _line_keyed_items=MagicMock(), process_items_for_cancel_kot=MagicMock()),
                    "ury.ury_pos.floor_actions": _stub("ury.ury_pos.floor_actions", assert_table_free_for_transfer=MagicMock()),
                    "ury.ury_pos.privileged_auth": _stub(
                        "ury.ury_pos.privileged_auth",
                        DEFAULT_MANAGER_ROLES=frozenset({"URY Manager"}),
                        _is_valid_str=lambda v: isinstance(v, str) and bool(v.strip()),
                        _table_exists=lambda _dt: False,
                        assert_not_efris_fiscalised=lambda _d: None,
                        assert_not_offline_replay=lambda: None,
                        authorize_or_perform=MagicMock(side_effect=AssertionError("Must refuse clock-in BEFORE authorize_or_perform")),
                        is_company_policy_active=lambda _c: False,
                    ),
                },
            )
        )
        _load_module("ury.brand", "ury/brand.py")
        oc = _load_module("ury.ury_pos.outlet_context", "ury/ury_pos/outlet_context.py")
        tc = _load_module("ury.ury_pos.timeclock", "ury/ury_pos/timeclock.py")
        api = _load_module("ury.ury_pos.api", "ury/ury_pos/api.py")
        qty_mod = _load_module(
            "ury.ury.api.ury_pos_invoice_qty_reduction",
            "ury/ury/api/ury_pos_invoice_qty_reduction.py",
        )
        return store, frappe, oc, tc, api, qty_mod

    def test_clocked_out_operator_refused_on_merge_and_qty_reduction(self):
        """[P1-3] `merge_bills` and `reduce_order_item_qty` must enforce `custom_require_clock_in_to_order`."""
        store, _frappe, _oc, _tc, api, qty_mod = self._build_env(user="cashier@example.test", clocked_in=False)
        item1 = Row(name="ROW-1", item_code="STEAK", item_name="Steak", qty=2, rate=25000, ury_held=0)
        item2 = Row(name="ROW-2", item_code="JUICE", item_name="Juice", qty=1, rate=8000, ury_held=0)
        store.invoices["POS-INV-1"] = Doc(
            name="POS-INV-1", docstatus=0, branch="Main Grill", company="Maximus Hospitality",
            pos_profile="Main Grill POS", order_type="Dine In", waiter="cashier@example.test",
            custom_merged_pos_invoice=None, items=[item1, item2],
        )
        store.invoices["POS-INV-2"] = Doc(
            name="POS-INV-2", docstatus=0, branch="Main Grill", company="Maximus Hospitality",
            pos_profile="Main Grill POS", order_type="Dine In", waiter="cashier@example.test",
            custom_merged_pos_invoice=None, items=[item2],
        )

        with self.assertRaises(Refused) as ctx:
            api.merge_bills("POS-INV-1", "POS-INV-2")
        self.assertIn("[POS-TIMECARD-CLOCK-IN-REQUIRED]", str(ctx.exception))

        with self.assertRaises(Refused) as ctx:
            qty_mod.reduce_order_item_qty("POS-INV-1", "ROW-1", 1)
        self.assertIn("[POS-TIMECARD-CLOCK-IN-REQUIRED]", str(ctx.exception))

    def test_clocked_out_operator_refused_on_split_bill_and_table_transfer(self):
        """[P1-3] `split_bill` and `table_transfer` must enforce `custom_require_clock_in_to_order`."""
        store, frappe, _oc, _tc, _api, _qty_mod = self._build_env(user="cashier@example.test", clocked_in=False)
        item1 = Row(name="ROW-1", item_code="STEAK", item_name="Steak", qty=2, rate=25000)
        item2 = Row(name="ROW-2", item_code="JUICE", item_name="Juice", qty=1, rate=8000)
        store.invoices["POS-INV-1"] = Doc(
            name="POS-INV-1", docstatus=0, branch="Main Grill", company="Maximus Hospitality",
            pos_profile="Main Grill POS", order_type="Dine In", restaurant_table="T-1",
            waiter="cashier@example.test", items=[item1, item2],
        )

        # Stub ury_order's Frappe/ERPNext transitive imports so we can execute real split_bill & table_transfer
        with patch.dict(
            sys.modules,
            {
                "frappe.model": _stub("frappe.model"),
                "frappe.model.document": _stub("frappe.model.document", Document=object),
                "erpnext": _stub("erpnext"),
                "erpnext.controllers": _stub("erpnext.controllers"),
                "erpnext.controllers.queries": _stub("erpnext.controllers.queries", item_query=MagicMock()),
                "ury.ury_pos.default_customer": MagicMock(),
                "ury.ury_pos.discount": MagicMock(),
                "ury.ury.api.ury_kot_generate": MagicMock(),
                "ury.ury.api.ury_kot_notification": MagicMock(),
                "ury.ury.api.ury_availability": MagicMock(),
                "ury.ury.api.ury_feature_flags": MagicMock(),
                "ury.ury.api.ury_order_reservation_service": MagicMock(),
                "ury.ury.api.ury_stock_policy": MagicMock(),
                "ury.ury.doctype.alert_settings": _stub("ury.ury.doctype.alert_settings"),
                "ury.ury.doctype.alert_settings.alert_settings": MagicMock(),
                "ury.ury.hooks.ury_pos_tender": MagicMock(),
                "ury.ury_pos.credit": MagicMock(),
                "ury.ury_pos.offline_tenders": MagicMock(),
                "ury.ury_pos.override": MagicMock(),
                "ury.ury_pos.pricing_actions": MagicMock(),
                "ury.ury_pos.room_charge": MagicMock(),
                "ury.ury.doctype.ury_order": _stub("ury.ury.doctype.ury_order"),
            },
        ):
            ury_order = _load_module("ury.ury.doctype.ury_order.ury_order", "ury/ury/doctype/ury_order/ury_order.py")
            with self.assertRaises(Refused) as ctx:
                ury_order.split_bill("POS-INV-1", [{"name": "ROW-1", "qty": 1}])
            self.assertIn("[POS-TIMECARD-CLOCK-IN-REQUIRED]", str(ctx.exception))

            with self.assertRaises(Refused) as ctx:
                ury_order.table_transfer("T-1", "T-2", "POS-INV-1")
            self.assertIn("[POS-TIMECARD-CLOCK-IN-REQUIRED]", str(ctx.exception))

    def test_open_work_blocks_cashier_owned_and_cross_outlet_draft_bills(self):
        """[P1-3] `_open_work_for_user` must check `waiter OR owner OR cashier` across ALL active outlets."""
        # Case A: Cashier with waiter="" and owner=cashier cannot clock out
        store, _frappe, _oc, tc, _api, _qty = self._build_env(user="cashier@example.test", clocked_in=True)
        store.open_invoices.append(
            Row(
                name="POS-DRAFT-CASHIER",
                docstatus=0,
                waiter="",
                owner="cashier@example.test",
                cashier="cashier@example.test",
                branch="Main Grill",
                company="Maximus Hospitality",
            )
        )
        with self.assertRaises(Refused) as ctx:
            tc.punch_timeclock("OUT", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-OPEN-WORK]", str(ctx.exception))
        self.assertIn("POS-DRAFT-CASHIER", str(ctx.exception))

        # Case B: Cashier field alone (owner=someone else, waiter="") also blocks clock out
        store.open_invoices[:] = [
            Row(
                name="POS-DRAFT-CASHIER-FIELD",
                docstatus=0,
                waiter="",
                owner="other@example.test",
                cashier="cashier@example.test",
                branch="Main Grill",
                company="Maximus Hospitality",
            )
        ]
        with self.assertRaises(Refused) as ctx:
            tc.punch_timeclock("OUT", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-OPEN-WORK]", str(ctx.exception))
        self.assertIn("POS-DRAFT-CASHIER-FIELD", str(ctx.exception))

        # Case C: Waiter holding open draft on Pool Bar cannot clock OUT via Main Grill POS
        store2, _frappe2, _oc2, tc2, _api2, _qty2 = self._build_env(user="waiter@example.test", clocked_in=True)
        store2.open_invoices.append(
            Row(
                name="POS-DRAFT-POOLBAR",
                docstatus=0,
                waiter="waiter@example.test",
                owner="waiter@example.test",
                cashier="",
                branch="Pool Bar",
                company="Maximus Hospitality",
            )
        )
        with self.assertRaises(Refused) as ctx:
            tc2.punch_timeclock("OUT", "Main Grill POS")
        self.assertIn("[POS-TIMECARD-OPEN-WORK]", str(ctx.exception))
        self.assertIn("POS-DRAFT-POOLBAR", str(ctx.exception))

        # Case D: Unrelated user's draft does NOT block waiter from clocking out
        store2.open_invoices[:] = [
            Row(
                name="POS-DRAFT-OTHER",
                docstatus=0,
                waiter="someone.else@example.test",
                owner="someone.else@example.test",
                cashier="someone.else@example.test",
                branch="Main Grill",
                company="Maximus Hospitality",
            )
        ]
        res = tc2.punch_timeclock("OUT", "Main Grill POS")
        self.assertFalse(res["is_clocked_in"])


class TestP1_6ShiftReportPermissions(unittest.TestCase):
    def _run_case(
        self,
        *,
        user_roles,
        report_read_perm=True,
        pos_invoice_report_perm=True,
        report_disabled=0,
        custom_roles=None,
        report_has_roles=None,
    ):
        frappe = MagicMock()
        frappe.PermissionError = NotPermitted
        frappe.ValidationError = Refused
        frappe._ = lambda m, *a: m
        frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
        frappe.session = SimpleNamespace(user="cashier@example.test", sid="sid-g44")
        frappe.request = SimpleNamespace(headers={}, cookies={"sid": "sid-g44"})
        frappe.get_roles = lambda u=None: list(user_roles)

        def _has_perm(dt, ptype="read", doc=None, user=None):
            if dt == "Report" and ptype == "read":
                return report_read_perm
            if dt == "POS Invoice" and ptype == "report":
                return pos_invoice_report_perm
            return True

        frappe.has_permission = _has_perm
        frappe.db = MagicMock()
        frappe.db.exists = lambda dt, filt=None: (dt == "Report" and filt == "Day Close Summary")

        def _get_value(dt, filt, fieldname=None, as_dict=False):
            if dt == "Report" and filt == "Day Close Summary":
                if fieldname == "disabled":
                    return report_disabled
                if fieldname == "ref_doctype":
                    return "POS Invoice"
            if dt == "Custom Role" and isinstance(filt, dict) and filt.get("report") == "Day Close Summary":
                return "CR-DAYCLOSE" if custom_roles is not None else None
            return None

        frappe.db.get_value = _get_value

        def _get_all(dt, filters=None, fields=None, **kw):
            filters = filters or {}
            if dt == "Has Role":
                if filters.get("parent") == "CR-DAYCLOSE":
                    return [Row(role=r) for r in (custom_roles or [])]
                if filters.get("parent") == "Day Close Summary":
                    return [Row(role=r) for r in (report_has_roles or [])]
            return []

        frappe.get_all = _get_all
        frappe.get_meta = lambda _dt: MagicMock(has_field=lambda _f: False)
        frappe.utils = _stub("frappe.utils", cint=lambda v: int(v or 0))

        report_mod = _stub(
            "frappe.core.doctype.report.report",
            get_custom_allowed_roles=lambda field, name: list(custom_roles) if custom_roles is not None else [],
        )
        with patch.dict(
            sys.modules,
            {
                "frappe": frappe,
                "frappe.utils": frappe.utils,
                "frappe.core": _stub("frappe.core"),
                "frappe.core.doctype": _stub("frappe.core.doctype"),
                "frappe.core.doctype.report": _stub("frappe.core.doctype.report"),
                "frappe.core.doctype.report.report": report_mod,
            },
        ):
            _load_module("ury.brand", "ury/brand.py")
            oc = _load_module("ury.ury_pos.outlet_context", "ury/ury_pos/outlet_context.py")
            return oc.get_business_date_and_report_meta("Main Grill POS", user="cashier@example.test")

    def test_shift_report_available_gates_on_report_and_doctype_permissions(self):
        """[P1-6] Must NOT advertise `shift_report_available=True` when user lacks report access or POS Invoice report perm."""
        # Stage 1: Report exists with Accounts Manager role only; URY Cashier lacks role -> False
        meta_s1 = self._run_case(
            user_roles=["URY Cashier"],
            report_read_perm=True,
            pos_invoice_report_perm=True,
            custom_roles=None,
            report_has_roles=["Accounts Manager"],
        )
        self.assertFalse(meta_s1["shift_report_available"])

        # Stage 2: Custom Role includes URY Cashier, but POS Invoice report perm is False -> False
        meta_s2 = self._run_case(
            user_roles=["URY Cashier"],
            report_read_perm=True,
            pos_invoice_report_perm=False,
            custom_roles=["URY Cashier", "URY Manager"],
            report_has_roles=["Accounts Manager"],
        )
        self.assertFalse(meta_s2["shift_report_available"])

        # Stage 2b: Report read perm is False -> False
        meta_s2b = self._run_case(
            user_roles=["URY Cashier"],
            report_read_perm=False,
            pos_invoice_report_perm=True,
            custom_roles=["URY Cashier"],
        )
        self.assertFalse(meta_s2b["shift_report_available"])

        # Stage 2c: Disabled Report -> False
        meta_disabled = self._run_case(
            user_roles=["URY Cashier"],
            report_read_perm=True,
            pos_invoice_report_perm=True,
            report_disabled=1,
            custom_roles=["URY Cashier"],
        )
        self.assertFalse(meta_disabled["shift_report_available"])

        # Stage 3: Both Custom Role + Report read + POS Invoice report -> True
        meta_s3 = self._run_case(
            user_roles=["URY Cashier"],
            report_read_perm=True,
            pos_invoice_report_perm=True,
            custom_roles=["URY Cashier", "URY Manager"],
        )
        self.assertTrue(meta_s3["shift_report_available"])


class TestP2_2TipsPayrollSerializationAndControlledClose(unittest.TestCase):
    def test_create_tip_additional_salaries_locks_for_update_and_maps_deadlock_to_417(self):
        """[P2-2] Must lock POS Closing Entry `FOR UPDATE` and map concurrent deadlock to `[POS-TIPS-PAYROLL-ALREADY-CREATED]`."""
        for_update_calls = []
        additional_salaries = []
        simulate_deadlock = [False]

        closing = Row(
            name="POS-CLO-LOCK-1",
            docstatus=1,
            pos_opening_entry="POS-OPE-1",
            pos_profile="Main Grill POS",
            company="Maximus Hospitality",
            posting_date="2026-11-24",
            custom_business_date="2026-11-24",
            custom_tip_declarations=[
                Row(
                    name="TD-1",
                    user="waiter1@example.test",
                    employee="HR-EMP-001",
                    currency="UGX",
                    net_tips_payable=15000,
                    custom_additional_salary_ref="",
                )
            ],
        )

        frappe = MagicMock()
        frappe.PermissionError = NotPermitted
        frappe.ValidationError = Refused
        frappe.QueryDeadlockError = QueryDeadlockError
        frappe._ = lambda m, *a: m
        frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
        frappe.session = SimpleNamespace(user="hr@example.test", sid="sid-hr")
        frappe.get_installed_apps = lambda: ["frappe", "erpnext", "hrms", "ury"]
        frappe.get_roles = lambda u=None: ["HR Manager"]
        frappe.has_permission = lambda dt, ptype="read", doc=None: True
        frappe.throw = lambda msg, exc=None, **kw: (_ for _ in ()).throw((exc or Refused)(msg))

        def _get_doc(arg, name=None, for_update=False):
            if arg == "POS Closing Entry":
                if for_update:
                    for_update_calls.append(("get_doc", arg, name))
                return closing
            if arg == "POS Profile":
                return Row(name="Main Grill POS", currency="UGX", custom_tips_salary_component="Staff Tips")
            if isinstance(arg, dict):
                doc = Row(arg)
                doc.name = "HR-ADS-0001"
                doc.docstatus = 0

                def _insert(ignore_permissions=False):
                    if simulate_deadlock[0]:
                        raise QueryDeadlockError("Deadlock found when trying to get lock")
                    additional_salaries.append(doc)
                    return doc

                doc.insert = _insert
                return doc
            raise Refused(f"Unknown doc {arg}")

        def _sql(query, values=None, as_dict=False):
            if "FOR UPDATE" in query.upper():
                for_update_calls.append(("sql", query))
            return []

        frappe.db = MagicMock()
        frappe.db.exists = lambda dt, filt=None: (
            (dt == "DocType" and filt == "Additional Salary")
            or (dt == "Salary Component" and filt == "Staff Tips")
            or (dt == "Additional Salary" and bool(additional_salaries))
        )
        frappe.db.get_value = lambda dt, filt, fieldname=None, as_dict=False, for_update=False: (
            for_update_calls.append(("get_value", dt, filt)) or Row(name=closing.name, docstatus=1)
            if dt == "POS Closing Entry" and for_update
            else ("UGX" if dt == "Company" else None)
        )
        frappe.db.sql = _sql
        frappe.db.set_value = MagicMock()
        frappe.get_doc = _get_doc
        frappe.utils = _stub(
            "frappe.utils",
            cint=lambda v: int(v or 0),
            flt=lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0),
            get_datetime=lambda v: v,
            get_time=lambda v: v,
            nowdate=lambda: "2026-11-24",
        )

        with patch.dict(
            sys.modules,
            {
                "frappe": frappe,
                "frappe.utils": frappe.utils,
                "ury.ury.hooks.ury_cash_movement": _stub(
                    "ury.ury.hooks.ury_cash_movement",
                    _drawer_accounts=MagicMock(return_value=set()),
                    line_amount_in_profile_currency=MagicMock(return_value=0.0),
                ),
            },
        ):
            _load_module("ury.brand", "ury/brand.py")
            _load_module("ury.ury_pos.outlet_context", "ury/ury_pos/outlet_context.py")
            td = _load_module("ury.ury_pos.tips_declaration", "ury/ury_pos/tips_declaration.py")

            # Normal run MUST acquire FOR UPDATE lock
            res = td.create_tip_additional_salaries("POS-CLO-LOCK-1")
            self.assertEqual(len(res["created"]), 1)
            self.assertTrue(len(for_update_calls) > 0, "Expected FOR UPDATE serialization lock on POS Closing Entry")

            # Simulated concurrent QueryDeadlockError MUST translate cleanly to ValidationError (417), NOT raw 500!
            additional_salaries.clear()
            closing.custom_tip_declarations[0].custom_additional_salary_ref = ""
            simulate_deadlock[0] = True
            with self.assertRaises(Refused) as ctx:
                td.create_tip_additional_salaries("POS-CLO-LOCK-1")
            self.assertIn("TIPS-PAYROLL-ALREADY-CREATED", str(ctx.exception))

            # Stock Accounts Manager without Additional Salary create perm stays fail-closed (403)
            frappe.get_roles = lambda u=None: ["Accounts Manager"]
            frappe.has_permission = lambda dt, ptype="read", doc=None: False if dt == "Additional Salary" else True
            with self.assertRaises(NotPermitted) as ctx:
                td.create_tip_additional_salaries("POS-CLO-LOCK-1")
            self.assertIn("TIPS-PAYROLL-FORBIDDEN", str(ctx.exception))

    def test_controlled_close_accepts_and_attaches_tip_declarations(self):
        """[#151+#157] `close_till_with_controls` must accept `tip_declarations` and populate `custom_tip_declarations`."""
        src = (ROOT / "ury/ury_pos/till_controls.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn_args = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ("close_till_with_controls", "force_close_till"):
                fn_args[node.name] = [a.arg for a in node.args.args]
        self.assertIn("tip_declarations", fn_args.get("close_till_with_controls", []))
        self.assertIn("tip_declarations", fn_args.get("force_close_till", []))
        self.assertIn("custom_tip_declarations", src)


if __name__ == "__main__":
    unittest.main()
