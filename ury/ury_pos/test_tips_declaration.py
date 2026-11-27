"""Site-less RED->GREEN unit tests for G38: Tips Declaration at Shift Close & HRMS Payroll Bridge.

Covers:
- `POS Tip Declaration` child table DocType schema (optional-HRMS safe)
- `get_shift_tip_declaration_rows` aggregation across shift invoices + Tip Payout JEs
- Multi-currency precision (0dp UGX, 2dp USD, 3dp KWD)
- Zero accounting distortion: declared cash tips NEVER alter cash drawer expected_amount or Over/Short variance
- `validate_tip_declarations` (server-side recomputation, negative/duplicate rejection, opt-in `custom_require_tip_declaration` -> `[POS-CLOSE-TIP-DECLARATION-REQUIRED]`)
- `create_tip_additional_salaries` strict HR Manager / Accounts Manager role gate (refuses URY Cashier / URY Waiter), Draft `Additional Salary`, `payroll_date` from `custom_business_date or posting_date`, and 100% idempotency refusing duplicate runs
"""

from __future__ import annotations

import importlib.util
import json
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


class Row(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


def _stub(name, **attrs):
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


class FakeTipsStore:
    def __init__(self, *, currency="UGX", hrms_installed=True):
        self.currency = currency
        self.hrms_installed = hrms_installed
        self.profiles = {
            "Main Grill POS": Row(
                name="Main Grill POS",
                branch="Main Grill",
                company="Maximus Hospitality",
                restaurant="Main Grill",
                currency=currency,
                disabled=0,
                custom_require_tip_declaration=0,
                custom_tips_salary_component="Staff Tips",
                custom_tips_payable_account="Tips Payable - MH",
                applicable_for_users=[
                    Row(user="cashier@example.test"),
                    Row(user="waiter1@example.test"),
                    Row(user="waiter2@example.test"),
                ],
                role_allowed_for_billing=[Row(role="URY Cashier")],
                payments=[Row(mode_of_payment="Cash")],
            ),
        }
        self.branch_users = {
            "Main Grill": ["cashier@example.test", "waiter1@example.test", "waiter2@example.test"],
            "Other Branch": ["outsider@example.test"],
        }
        self.user_permissions = {
            "cashier@example.test": {"Branch": [{"doc": "Main Grill", "is_default": 1}]},
            "outsider@example.test": {"Branch": [{"doc": "Other Branch", "is_default": 1}]},
        }
        self.roles = {
            "cashier@example.test": ["URY Cashier"],
            "waiter1@example.test": ["URY Waiter"],
            "outsider@example.test": ["URY Cashier"],
            "hr@example.test": ["HR Manager"],
            "acct@example.test": ["Accounts Manager"],
        }
        self.users_fullname = {
            "waiter1@example.test": "Alice Nakato",
            "waiter2@example.test": "Brian Opio",
            "cashier@example.test": "Grace Cashier",
        }
        self.employees = {
            ("waiter1@example.test", "Maximus Hospitality"): "HR-EMP-001",
            ("waiter2@example.test", "Maximus Hospitality"): "HR-EMP-002",
        }
        self.openings = {
            "POS-OPE-001": Row(
                name="POS-OPE-001",
                pos_profile="Main Grill POS",
                company="Maximus Hospitality",
                branch="Main Grill",
                user="cashier@example.test",
                period_start_date="2026-11-24 08:00:00",
                posting_date="2026-11-24",
                custom_business_date="2026-11-24",
                status="Open",
                docstatus=1,
            )
        }
        self.invoices = [
            Row(
                name="POS-INV-1",
                pos_profile="Main Grill POS",
                company="Maximus Hospitality",
                docstatus=1,
                posting_date="2026-11-24",
                posting_time="10:00:00",
                consolidated_invoice=None,
                waiter="waiter1@example.test",
                owner="cashier@example.test",
                custom_tip_server="waiter1@example.test",
                custom_charged_tip=30000 if currency == "UGX" else (12.50 if currency == "USD" else 4.250),
            ),
            Row(
                name="POS-INV-2",
                pos_profile="Main Grill POS",
                company="Maximus Hospitality",
                docstatus=1,
                posting_date="2026-11-24",
                posting_time="11:30:00",
                consolidated_invoice=None,
                waiter="waiter2@example.test",
                owner="cashier@example.test",
                custom_tip_server="waiter2@example.test",
                custom_charged_tip=10000 if currency == "UGX" else (5.00 if currency == "USD" else 2.125),
            ),
        ]
        self.tip_payouts = [
            Row(
                name="ACC-JV-001",
                docstatus=1,
                posting_date="2026-11-24",
                custom_pos_opening_entry="POS-OPE-001",
                custom_cash_movement_type="Paid Out",
                custom_tip_server="waiter1@example.test",
                total_debit=10000 if currency == "UGX" else (2.50 if currency == "USD" else 1.000),
            )
        ]
        self.je_accounts = {
            "ACC-JV-001": [
                Row(
                    account="Cash - MH",
                    account_currency=currency,
                    debit_in_account_currency=0,
                    credit_in_account_currency=10000 if currency == "UGX" else (2.50 if currency == "USD" else 1.000),
                    debit=0,
                    credit=10000 if currency == "UGX" else (2.50 if currency == "USD" else 1.000),
                ),
                Row(
                    account="Tips Payable - MH",
                    account_currency=currency,
                    debit_in_account_currency=10000 if currency == "UGX" else (2.50 if currency == "USD" else 1.000),
                    credit_in_account_currency=0,
                    debit=10000 if currency == "UGX" else (2.50 if currency == "USD" else 1.000),
                    credit=0,
                ),
            ]
        }
        self.closings = {}
        self.additional_salaries: list[Row] = []
        self.salary_components = {"Staff Tips"}
        self.cache_store = {}


def _build_frappe_for_tips(store: FakeTipsStore, user="cashier@example.test"):
    frappe = MagicMock()
    frappe.PermissionError = NotPermitted
    frappe.ValidationError = Refused
    frappe.DoesNotExistError = Refused
    frappe._ = lambda msg, *a: msg
    frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
    frappe.session = SimpleNamespace(user=user, sid="sid-tips-1")
    frappe.request = SimpleNamespace(headers={}, cookies={"sid": "sid-tips-1"})
    frappe.flags = Row()
    frappe.conf = Row()

    def _throw(msg, exc=None, **kwargs):
        raise (exc or Refused)(msg)

    frappe.throw = _throw
    frappe.get_installed_apps = lambda: (["frappe", "erpnext", "hrms", "ury"] if store.hrms_installed else ["frappe", "erpnext", "ury"])
    frappe.get_roles = lambda u=None: list(store.roles.get(u or frappe.session.user, []))
    frappe.has_permission = lambda dt, ptype="read", doc=None: (
        True
        if (dt == "Additional Salary" and {"HR Manager", "Accounts Manager"} & set(frappe.get_roles()))
        or dt != "Additional Salary"
        else False
    )

    def _exists(dt, filt=None):
        if dt == "DocType" and filt == "Additional Salary":
            return store.hrms_installed
        if dt == "Salary Component":
            return filt in store.salary_components
        if dt == "POS Profile":
            return filt in store.profiles
        if dt == "URY User" and isinstance(filt, dict):
            return filt.get("user") in store.branch_users.get(filt.get("parent"), [])
        if dt == "Additional Salary" and isinstance(filt, dict):
            for s in store.additional_salaries:
                if (
                    s.ref_doctype == filt.get("ref_doctype")
                    and s.ref_docname == filt.get("ref_docname")
                    and s.employee == filt.get("employee")
                    and s.docstatus < 2
                ):
                    return s.name
            return False
        return False

    def _get_value(dt, filt, fieldname=None, as_dict=False):
        if dt == "User" and fieldname == "enabled":
            return 1
        if dt == "User" and fieldname == "full_name":
            return store.users_fullname.get(filt, filt)
        if dt == "Company" and fieldname == "default_currency":
            return store.currency
        if dt == "Mode of Payment" and fieldname == "type":
            return "Cash"
        if dt == "Mode of Payment Account":
            return "Cash - MH"
        if dt == "POS Opening Entry":
            op = store.openings.get(filt)
            if not op:
                return None
            if isinstance(fieldname, list):
                return Row({k: op.get(k) for k in fieldname}) if as_dict else tuple(op.get(k) for k in fieldname)
            return op.get(fieldname)
        if dt == "POS Profile":
            prof = store.profiles.get(filt)
            if not prof:
                return None
            if isinstance(fieldname, list):
                return Row({k: prof.get(k) for k in fieldname}) if as_dict else tuple(prof.get(k) for k in fieldname)
            return prof.get(fieldname)
        if dt == "Employee" and isinstance(filt, dict):
            return store.employees.get((filt.get("user_id"), filt.get("company")))
        return None

    def _set_value(dt, name, field, value, update_modified=True):
        if dt == "POS Tip Declaration":
            for closing in store.closings.values():
                for r in closing.get("custom_tip_declarations") or []:
                    if r.get("name") == name:
                        r[field] = value

    def _get_all(dt, filters=None, fields=None, order_by=None, limit=None, limit_page_length=None, pluck=None):
        filters = filters or {}
        if dt == "POS Profile":
            return [p for p in store.profiles.values() if not p.disabled]
        if dt == "POS Profile User":
            return [Row(parent=pn, user=u.user) for pn, p in store.profiles.items() for u in p.applicable_for_users]
        if dt == "POS Invoice":
            return [inv for inv in store.invoices if inv.pos_profile == filters.get("pos_profile") and inv.docstatus == 1]
        if dt == "Journal Entry":
            if filters.get("custom_pos_opening_entry"):
                return [je for je in store.tip_payouts if je.custom_pos_opening_entry == filters["custom_pos_opening_entry"]]
            return []
        if dt == "Journal Entry Account":
            return store.je_accounts.get(filters.get("parent"), [])
        return []

    def _sql(query, values=None, as_dict=False):
        if "tabURY User" in query:
            u = values[0] if isinstance(values, (list, tuple)) else values
            return [Row(branch=br, room="Room 1") for br, users in store.branch_users.items() if u in users]
        return []

    def _get_doc(arg, name=None):
        if isinstance(arg, dict):
            doc = Row(arg)
            doc.name = f"HR-ADS-{len(store.additional_salaries) + 1:04d}"
            doc.docstatus = 0

            def _insert(ignore_permissions=False):
                store.additional_salaries.append(doc)
                return doc

            doc.insert = _insert
            return doc
        if arg == "POS Profile":
            return store.profiles[name]
        if arg == "POS Closing Entry":
            if name not in store.closings:
                raise Refused(f"POS Closing Entry {name} not found")
            return store.closings[name]
        raise Refused(f"Unknown doc {arg} {name}")

    frappe.db = MagicMock()
    frappe.db.exists = _exists
    frappe.db.get_value = _get_value
    frappe.db.set_value = _set_value
    frappe.db.sql = _sql
    frappe.get_cached_value = _get_value
    frappe.get_all = _get_all
    frappe.get_doc = _get_doc
    frappe.get_precision = lambda dt, field: 0 if store.currency == "UGX" else (3 if store.currency == "KWD" else 2)
    frappe.cache = MagicMock()
    frappe.cache.get_value = lambda k: store.cache_store.get(k)
    frappe.cache.set_value = lambda k, v, expires_in_sec=None: store.cache_store.__setitem__(k, v)
    frappe.cache.delete_value = lambda k: store.cache_store.pop(k, None)

    utils = _stub(
        "frappe.utils",
        cint=lambda v: int(v or 0),
        flt=lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0),
        get_datetime=lambda v: v if isinstance(v, datetime) else datetime.fromisoformat(str(v)),
        get_time=lambda v: datetime.strptime(str(v), "%H:%M:%S").time(),
        nowdate=lambda: "2026-11-24",
    )
    frappe.utils = utils
    return frappe, utils


def _load_module(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestTipsDeclaration(unittest.TestCase):
    def _env(self, *, currency="UGX", hrms_installed=True, user="cashier@example.test"):
        store = FakeTipsStore(currency=currency, hrms_installed=hrms_installed)
        frappe, utils = _build_frappe_for_tips(store, user=user)
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
        _load_module("ury.ury.hooks.ury_cash_movement", "ury/ury/hooks/ury_cash_movement.py")
        _load_module("ury.ury_pos.outlet_context", "ury/ury_pos/outlet_context.py")
        td = _load_module("ury.ury_pos.tips_declaration", "ury/ury_pos/tips_declaration.py")
        return store, frappe, td

    def test_doctype_schema_exists_and_is_optional_hrms_safe(self):
        schema_path = ROOT / "ury/ury/doctype/pos_tip_declaration/pos_tip_declaration.json"
        self.assertTrue(schema_path.is_file(), "POS Tip Declaration DocType JSON missing")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema.get("istable"), 1)
        fields = {f["fieldname"]: f for f in schema.get("fields", [])}
        for fname in (
            "user",
            "employee",
            "currency",
            "charged_tips_collected",
            "tips_paid_from_till",
            "net_tips_payable",
            "declared_cash_tips",
            "remarks",
            "custom_additional_salary_ref",
        ):
            self.assertIn(fname, fields)
        # Must NOT be Link to Additional Salary so sites without hrms never fail link validation
        self.assertEqual(fields["custom_additional_salary_ref"]["fieldtype"], "Data")

    def test_get_shift_tip_declaration_rows_aggregates_and_respects_currency_precision(self):
        for currency, expected_dp, w1_net, w2_net in (
            ("UGX", 0, 20000, 10000),
            ("USD", 2, 10.00, 5.00),
            ("KWD", 3, 3.250, 2.125),
        ):
            with self.subTest(currency=currency):
                store, _frappe, td = self._env(currency=currency)
                res = td.get_shift_tip_declaration_rows("POS-OPE-001")
                self.assertEqual(res["currency"], currency)
                self.assertEqual(res["currency_precision"], expected_dp)
                by_user = {r["user"]: r for r in res["rows"]}
                self.assertIn("waiter1@example.test", by_user)
                self.assertIn("waiter2@example.test", by_user)
                self.assertAlmostEqual(by_user["waiter1@example.test"]["net_tips_payable"], w1_net, places=expected_dp)
                self.assertAlmostEqual(by_user["waiter2@example.test"]["net_tips_payable"], w2_net, places=expected_dp)
                self.assertEqual(by_user["waiter1@example.test"]["employee"], "HR-EMP-001")

    def test_unassigned_guest_tip_is_reconciled_without_payroll_employee(self):
        store, _, td = self._env(currency="KWD")
        row = store.invoices[0]
        row.update(custom_ordering_session="QR-1", custom_tip_server="Administrator", waiter=None,
                   owner="Administrator", custom_charged_tip=1.235)
        rows = td.get_shift_tip_declaration_rows("POS-OPE-001")["rows"]
        bucket = next(r for r in rows if r["user"] == "Guest")
        self.assertEqual(bucket["net_tips_payable"], 1.235)
        self.assertIsNone(bucket["employee"])
        self.assertIn("unassigned", bucket["full_name"].lower())

    def test_cross_branch_user_cannot_read_shift_tip_rows(self):
        _store, _frappe, td = self._env(user="outsider@example.test")
        with self.assertRaises(NotPermitted):
            td.get_shift_tip_declaration_rows("POS-OPE-001")

    def test_validate_tip_declarations_enforces_required_and_zero_drawer_impact(self):
        store, _frappe, td = self._env(currency="UGX")
        store.profiles["Main Grill POS"].custom_require_tip_declaration = 1

        # Missing waiter2 when custom_require_tip_declaration = 1 -> refused
        closing = Row(
            name="POS-CLO-001",
            pos_opening_entry="POS-OPE-001",
            pos_profile="Main Grill POS",
            company="Maximus Hospitality",
            payment_reconciliation=[
                Row(mode_of_payment="Cash", opening_amount=50000, expected_amount=100000, closing_amount=100000, difference=0)
            ],
            custom_tip_declarations=[
                Row(name="td-1", user="waiter1@example.test", declared_cash_tips=45000, charged_tips_collected=999999),
            ],
        )
        with self.assertRaises(Refused) as ctx:
            td.validate_tip_declarations(closing)
        self.assertIn("CLOSE-TIP-DECLARATION-REQUIRED", str(ctx.exception))
        self.assertNotRegex(str(ctx.exception), VENDOR_RE)

        # Negative declared cash tips refused
        closing.custom_tip_declarations.append(Row(name="td-2", user="waiter2@example.test", declared_cash_tips=-500))
        with self.assertRaises(Refused) as ctx:
            td.validate_tip_declarations(closing)
        self.assertIn("CLOSE-TIP-NEGATIVE", str(ctx.exception))

        # Valid rows: server recomputes charged/paid/net (ignores tampered 999999), leaves drawer expected/diff untouched
        closing.custom_tip_declarations[1].declared_cash_tips = 0
        td.validate_tip_declarations(closing)
        self.assertEqual(closing.custom_tip_declarations[0].charged_tips_collected, 30000)
        self.assertEqual(closing.custom_tip_declarations[0].tips_paid_from_till, 10000)
        self.assertEqual(closing.custom_tip_declarations[0].net_tips_payable, 20000)
        self.assertEqual(closing.custom_tip_declarations[0].declared_cash_tips, 45000)
        # Accounting separation invariant: drawer expected_amount & difference NEVER change!
        self.assertEqual(closing.payment_reconciliation[0].expected_amount, 100000)
        self.assertEqual(closing.payment_reconciliation[0].difference, 0)

    def test_create_tip_additional_salaries_role_gate_and_idempotency(self):
        store, frappe, td = self._env(currency="UGX", hrms_installed=True, user="cashier@example.test")
        closing = Row(
            name="POS-CLO-001",
            docstatus=1,
            pos_opening_entry="POS-OPE-001",
            pos_profile="Main Grill POS",
            company="Maximus Hospitality",
            posting_date="2026-11-25",
            custom_business_date="2026-11-24",
            custom_tip_declarations=[
                Row(
                    name="td-1",
                    user="waiter1@example.test",
                    employee="HR-EMP-001",
                    currency="UGX",
                    charged_tips_collected=30000,
                    tips_paid_from_till=10000,
                    net_tips_payable=20000,
                    declared_cash_tips=45000,
                    custom_additional_salary_ref=None,
                ),
                Row(
                    name="td-2",
                    user="waiter2@example.test",
                    employee="HR-EMP-002",
                    currency="UGX",
                    charged_tips_collected=10000,
                    tips_paid_from_till=10000,
                    net_tips_payable=0,
                    declared_cash_tips=15000,
                    custom_additional_salary_ref=None,
                ),
            ],
        )
        store.closings["POS-CLO-001"] = closing

        # Cashier & Waiter are refused (zero accounting/HR DocPerm widening)
        for denied_user in ("cashier@example.test", "waiter1@example.test"):
            frappe.session.user = denied_user
            with self.assertRaises(NotPermitted):
                td.create_tip_additional_salaries("POS-CLO-001")

        # HR Manager succeeds: creates 1 Draft Additional Salary ONLY for net_tips_payable > 0 (not declared_cash_tips!)
        frappe.session.user = "hr@example.test"
        res = td.create_tip_additional_salaries("POS-CLO-001")
        self.assertEqual(len(res["created"]), 1)
        self.assertEqual(len(store.additional_salaries), 1)
        sal = store.additional_salaries[0]
        self.assertEqual(sal.docstatus, 0)
        self.assertEqual(sal.employee, "HR-EMP-001")
        self.assertEqual(sal.amount, 20000)
        self.assertEqual(sal.payroll_date, "2026-11-24")
        self.assertEqual(sal.ref_doctype, "POS Closing Entry")
        self.assertEqual(sal.ref_docname, "POS-CLO-001")
        self.assertEqual(closing.custom_tip_declarations[0].custom_additional_salary_ref, sal.name)

        # Duplicate run refused cleanly — zero duplicate Additional Salary rows created!
        frappe.session.user = "acct@example.test"
        with self.assertRaises(Refused) as ctx:
            td.create_tip_additional_salaries("POS-CLO-001")
        self.assertIn("TIPS-PAYROLL-ALREADY-CREATED", str(ctx.exception))
        self.assertEqual(len(store.additional_salaries), 1)

    def test_child_doctype_ignores_personal_user_permissions(self):
        import json
        from pathlib import Path

        dt_path = Path(__file__).resolve().parents[1] / "ury" / "doctype" / "pos_tip_declaration" / "pos_tip_declaration.json"
        spec = json.loads(dt_path.read_text(encoding="utf-8"))
        fields = {f["fieldname"]: f for f in spec["fields"]}
        self.assertEqual(fields["user"].get("ignore_user_permissions"), 1)
        self.assertEqual(fields["employee"].get("ignore_user_permissions"), 1)
        # Never link directly to HRMS Additional Salary so non-HRMS sites stay valid
        self.assertEqual(fields["custom_additional_salary_ref"].get("fieldtype"), "Data")

    def test_get_doc_for_update_mariadb_1020_deadlock_maps_to_417(self):
        """MariaDB 11.8 Error 1020 ('Record has changed since last read in table tabPOS Tip Declaration')
        raised inside `frappe.get_doc(..., for_update=True)` must map to 417 `[POS-TIPS-PAYROLL-ALREADY-CREATED]`,
        never raw HTTP 500 QueryDeadlockError."""
        _store, frappe, td = self._env(currency="UGX", hrms_installed=True, user="hr@example.test")

        class FakeQueryDeadlockError(Exception):
            pass

        frappe.QueryDeadlockError = FakeQueryDeadlockError
        orig_get_doc = frappe.get_doc

        def _get_doc_1020(arg, name=None, **kw):
            if arg == "POS Closing Entry" and kw.get("for_update"):
                raise FakeQueryDeadlockError(
                    "(1020, \"Record has changed since last read in table 'tabPOS Tip Declaration'; try restarting transaction\")"
                )
            return orig_get_doc(arg, name)

        frappe.get_doc = _get_doc_1020
        with self.assertRaises(Refused) as ctx:
            td.create_tip_additional_salaries("POS-CLO-001")
        self.assertIn("TIPS-PAYROLL-ALREADY-CREATED", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
