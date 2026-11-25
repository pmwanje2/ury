"""Site-less RED->GREEN unit tests for G43 (Multi-Outlet Switcher) & G44 (Business Date + Shift X/Z).

Covers:
- `get_available_outlets()` intersection of Company/Branch/POS Profile User Permissions + applicable_for_users / Branch.user
- `set_active_outlet()` session-isolated cache keyed by `(user, sid)` with ZERO User Permission mutation
- Unauthorized 3rd branch/profile refused with 403 PermissionError (`[POS-OUTLET-FORBIDDEN]`)
- Revoked permission fails closed even when cached in session
- Multi-outlet switch updates `getBranch()`, `getPosProfile()`, `getPosProfileFull()`, `getRestaurantMenu()`
- Multi-outlet bootstrap returns `requires_outlet_selection: True` instead of crashing when ambiguous
- Open-till cashier guard `[POS-OUTLET-OPEN-TILL-ACTIVE]` on `create_pos_opening_entry` and `make_invoice`
- G44 `custom_business_date` & `Day Close Summary` / `get_shift_x_report()` graceful presence & absence
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
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


class FakeOutletStore:
    def __init__(self, *, has_business_date=True, has_day_close=True):
        self.has_business_date = has_business_date
        self.has_day_close = has_day_close
        self.profiles = {
            "Main Grill POS": Row(
                name="Main Grill POS",
                branch="Main Grill",
                company="Maximus Hospitality",
                restaurant="Main Grill",
                currency="UGX",
                country="Uganda",
                warehouse="Grill Stores",
                disabled=0,
                print_format="Receipt",
                qz_print=0,
                qz_host=None,
                table_attention_time=10,
                paid_limit=0,
                custom_enable_discount=1,
                custom_enable_multiple_cashier=0,
                custom_edit_order_type=0,
                custom_enable_kot_reprint=0,
                custom_invoice_warning_time=0,
                applicable_for_users=[Row(user="staff@example.test")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
                role_restricted_for_table_order=[],
                transfer_role_permissions=[],
                printer_settings=[],
                payments=[Row(mode_of_payment="Cash", default=1)],
            ),
            "Pool Bar POS": Row(
                name="Pool Bar POS",
                branch="Pool Bar",
                company="Maximus Hospitality",
                restaurant="Pool Bar",
                currency="USD",
                country="Uganda",
                warehouse="Pool Stores",
                disabled=0,
                print_format="Receipt",
                qz_print=0,
                qz_host=None,
                table_attention_time=10,
                paid_limit=0,
                custom_enable_discount=1,
                custom_enable_multiple_cashier=0,
                custom_edit_order_type=0,
                custom_enable_kot_reprint=0,
                custom_invoice_warning_time=0,
                applicable_for_users=[Row(user="staff@example.test")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
                role_restricted_for_table_order=[],
                transfer_role_permissions=[],
                printer_settings=[],
                payments=[Row(mode_of_payment="USD Cash", default=1)],
            ),
            "VIP Lounge POS": Row(
                name="VIP Lounge POS",
                branch="VIP Lounge",
                company="Maximus Hospitality",
                restaurant="VIP Lounge",
                currency="KWD",
                country="Kuwait",
                warehouse="VIP Stores",
                disabled=0,
                applicable_for_users=[Row(user="vip@example.test")],
                role_allowed_for_billing=[Row(role="URY Cashier")],
                role_restricted_for_table_order=[],
                transfer_role_permissions=[],
                printer_settings=[],
                payments=[Row(mode_of_payment="KWD Cash", default=1)],
            ),
            "Disabled Bar POS": Row(
                name="Disabled Bar POS",
                branch="Main Grill",
                company="Maximus Hospitality",
                restaurant="Main Grill",
                currency="UGX",
                disabled=1,
                applicable_for_users=[Row(user="staff@example.test")],
                payments=[],
            ),
        }
        self.branch_users = {
            "Main Grill": ["staff@example.test"],
            "Pool Bar": ["staff@example.test"],
            "VIP Lounge": ["vip@example.test"],
        }
        self.user_permissions = {
            "staff@example.test": {
                "Branch": [
                    {"doc": "Main Grill", "is_default": 0},
                    {"doc": "Pool Bar", "is_default": 0},
                ],
                "Company": [{"doc": "Maximus Hospitality", "is_default": 1}],
            }
        }
        self.roles = {
            "staff@example.test": ["URY Cashier", "URY Waiter"],
        }
        self.open_shifts: list[Row] = []
        self.cache_store: dict[str, object] = {}
        self.user_permission_writes: list[tuple] = []
        self.menus = {
            "Main Grill": ("Main Grill Menu", [Row(item="STEAK", item_name="Grilled Steak", rate=45000, special_dish=0, disabled=0, course="Mains", idx=1)]),
            "Pool Bar": ("Pool Bar Menu", [Row(item="MOJITO", item_name="Mint Mojito", rate=12.50, special_dish=0, disabled=0, course="Drinks", idx=1)]),
        }


def _build_frappe_for_outlets(store: FakeOutletStore, user="staff@example.test", sid="sid-a"):
    frappe = MagicMock()
    frappe.PermissionError = NotPermitted
    frappe.ValidationError = Refused
    frappe.DoesNotExistError = Refused
    frappe._ = lambda msg, *a: msg
    frappe.whitelist = lambda *a, **kw: (lambda fn: fn)
    frappe.session = SimpleNamespace(user=user, sid=sid)
    frappe.request = SimpleNamespace(headers={}, cookies={"sid": sid})
    frappe.flags = Row()
    frappe.conf = Row()

    def _throw(msg, exc=None, **kwargs):
        raise (exc or Refused)(msg)

    frappe.throw = _throw
    frappe.get_roles = lambda u=None: list(store.roles.get(u or frappe.session.user, []))
    frappe.has_permission = lambda dt, ptype="read", doc=None: True
    frappe.get_single = lambda dt: Row(disable_rounded_total=0)

    def _get_meta(dt):
        m = MagicMock()
        if dt == "POS Opening Entry":
            m.has_field = lambda f: (f == "custom_business_date" and store.has_business_date)
        elif dt == "Company":
            m.has_field = lambda f: False
        else:
            m.has_field = lambda f: False
        return m

    frappe.get_meta = _get_meta

    def _exists(dt, filt=None):
        if dt == "POS Profile":
            if isinstance(filt, str):
                return filt in store.profiles
            if isinstance(filt, dict) and "branch" in filt:
                for p in store.profiles.values():
                    if p.branch == filt["branch"] and not p.disabled:
                        return p.name
                return None
        if dt == "Report" and filt == "Day Close Summary":
            return store.has_day_close
        if dt == "Server Script" and isinstance(filt, dict) and filt.get("api_method") == "pos_day_close.shift_z_summary":
            return store.has_day_close
        if dt == "Print Format" and filt == "POS Shift Z Report":
            return store.has_day_close
        if dt == "URY User" and isinstance(filt, dict):
            return filt.get("user") in store.branch_users.get(filt.get("parent"), [])
        return False

    def _get_value(dt, filt, fieldname=None, as_dict=False):
        if dt == "User" and fieldname == "enabled":
            return 1
        if dt == "Company" and fieldname == "default_currency":
            return "UGX"
        if dt == "Company" and fieldname == "country":
            return "Uganda"
        if dt == "Mode of Payment":
            return "Cash" if fieldname == "type" else "Refuse"
        if dt == "Mode of Payment Account":
            return "Cash - MH"
        if dt == "URY Restaurant":
            if isinstance(filt, dict) and "branch" in filt:
                return filt["branch"]
            if fieldname == "active_menu":
                return store.menus[filt][0]
            return 0
        if dt == "URY Menu" and fieldname == "modified":
            return "2026-11-24 08:00:00"
        if dt == "Item" and fieldname == "image":
            return None
        if dt == "POS Profile":
            prof = store.profiles.get(filt) if isinstance(filt, str) else None
            if not prof:
                return None
            return prof.get(fieldname)
        return None

    def _get_all(dt, filters=None, fields=None, order_by=None, limit=None, limit_page_length=None, pluck=None):
        filters = filters or {}
        if dt == "POS Profile":
            rows = list(store.profiles.values())
            if "disabled" in filters:
                rows = [p for p in rows if int(p.disabled or 0) == int(filters["disabled"])]
            return rows
        if dt == "POS Profile User":
            return [Row(parent=pn, user=u.user) for pn, p in store.profiles.items() for u in p.applicable_for_users]
        if dt == "POS Opening Entry":
            rows = []
            for s in store.open_shifts:
                if filters.get("docstatus") is not None and s.docstatus != filters["docstatus"]:
                    continue
                if filters.get("status") and s.status != filters["status"]:
                    continue
                if filters.get("user") and s.user != filters["user"]:
                    continue
                if filters.get("pos_profile") and s.pos_profile != filters["pos_profile"]:
                    continue
                rows.append(s)
            return rows[:limit] if limit else rows
        if dt == "URY Menu Item":
            for _br, (menu_name, items) in store.menus.items():
                if menu_name == filters.get("parent"):
                    return items
            return []
        if dt == "URY Menu Course":
            return [Row(name="Mains", custom_serving_priority=1), Row(name="Drinks", custom_serving_priority=2)]
        return []

    def _sql(query, values=None, as_dict=False):
        if "User Permission" in query and any(w in query.upper() for w in ("INSERT", "UPDATE", "DELETE")):
            store.user_permission_writes.append((query, values))
        if "tabURY User" in query:
            u = values[0] if isinstance(values, (list, tuple)) else values
            return [Row(branch=br, room=f"Room {br}") for br, users in store.branch_users.items() if u in users]
        return []

    def _get_doc(arg, name=None):
        if isinstance(arg, dict):
            if arg.get("doctype") == "User Permission":
                store.user_permission_writes.append(("insert", arg))
            doc = Row(arg)
            doc.name = f"DOC-{arg.get('doctype')}"
            doc.set = lambda k, v: doc.__setitem__(k, v)
            doc.submit = MagicMock()
            doc.as_dict = lambda: dict(doc)
            return doc
        if arg == "POS Profile":
            return store.profiles[name]
        raise Refused(f"Unknown doc {arg} {name}")

    def _set_value(dt, *args, **kwargs):
        if dt == "User Permission":
            store.user_permission_writes.append(("set_value", args, kwargs))

    frappe.db = MagicMock()
    frappe.db.exists = _exists
    frappe.db.get_value = _get_value
    frappe.db.set_value = _set_value
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
        get_datetime=lambda v=None: "2026-11-24 10:00:00",
        getdate=lambda v=None: "2026-11-24",
        nestedset=MagicMock(),
        validate_phone_number=MagicMock(),
    )
    frappe.utils = utils
    return frappe, utils


def _load_module(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestMultiOutletSwitcherAndBusinessDate(unittest.TestCase):
    def _env(self, *, has_business_date=True, has_day_close=True, user="staff@example.test", sid="sid-a"):
        store = FakeOutletStore(has_business_date=has_business_date, has_day_close=has_day_close)
        frappe, utils = _build_frappe_for_outlets(store, user=user, sid=sid)
        user_perm_mod = _stub(
            "frappe.core.doctype.user_permission.user_permission",
            get_user_permissions=lambda u=None: store.user_permissions.get(u or frappe.session.user, {}),
        )
        query_report_mod = _stub(
            "frappe.desk.query_report",
            run=MagicMock(
                return_value={
                    "columns": [{"fieldname": "metric", "label": "Metric"}, {"fieldname": "amount", "label": "Amount"}],
                    "result": [{"metric": "Gross Sales", "amount": 150000}],
                    "report_summary": [{"label": "Gross Sales", "value": 150000}],
                    "message": "",
                }
            ),
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
                    "frappe.desk": _stub("frappe.desk"),
                    "frappe.desk.query_report": query_report_mod,
                    "ury.ury_pos.default_customer": _stub(
                        "ury.ury_pos.default_customer", get_default_customer_config=lambda p: {}
                    ),
                    "ury.ury.api.item_price": _stub(
                        "ury.ury.api.item_price", item_price_release_states=MagicMock(), price_valid_on=MagicMock()
                    ),
                },
            )
        )
        _load_module("ury.brand", "ury/brand.py")
        oc = _load_module("ury.ury_pos.outlet_context", "ury/ury_pos/outlet_context.py")
        api = _load_module("ury.ury_pos.api", "ury/ury_pos/api.py")
        return store, frappe, oc, api, query_report_mod

    def test_available_outlets_and_session_isolated_switching_without_user_permission_mutation(self):
        store, frappe, oc, api, _qr = self._env()

        # Available outlets include Main Grill (UGX) and Pool Bar (USD), exclude VIP Lounge & Disabled Bar
        outlets = oc.get_available_outlets()
        names = [o["pos_profile"] for o in outlets]
        self.assertEqual(names, ["Main Grill POS", "Pool Bar POS"])
        by_name = {o["pos_profile"]: o for o in outlets}
        self.assertEqual(by_name["Main Grill POS"]["currency"], "UGX")
        self.assertEqual(by_name["Pool Bar POS"]["currency"], "USD")

        # Ambiguous bootstrap does NOT crash with BRANCH-AMBIGUOUS — returns requires_outlet_selection
        bootstrap = api.getPosProfile()
        self.assertTrue(bootstrap.get("requires_outlet_selection"))
        self.assertEqual(len(bootstrap.get("available_outlets") or []), 2)

        # Direct getBranch() still refuses BRANCH-AMBIGUOUS when no session outlet selected
        with self.assertRaises(Refused) as ctx:
            api.getBranch()
        self.assertIn("[POS-BRANCH-AMBIGUOUS]", str(ctx.exception))

        # Switch active outlet in session A to Pool Bar POS
        switched = oc.set_active_outlet("Pool Bar POS")
        self.assertEqual(switched["pos_profile"], "Pool Bar POS")
        self.assertEqual(switched["branch"], "Pool Bar")
        self.assertEqual(switched["currency"], "USD")
        self.assertEqual(store.user_permission_writes, [], "Must NEVER mutate User Permission!")

        # Session A now resolves getBranch, getPosProfile, getRestaurantMenu to Pool Bar
        self.assertEqual(api.getBranch(), "Pool Bar")
        self.assertEqual(api.getPosProfile()["pos_profile"], "Pool Bar POS")
        menu = api.getRestaurantMenu("Pool Bar POS")
        self.assertEqual(menu["name"], "Pool Bar Menu")
        self.assertEqual(menu["items"][0]["item"], "MOJITO")

        # Session B for the SAME user is completely isolated!
        frappe.session.sid = "sid-b"
        frappe.request.cookies = {"sid": "sid-b"}
        self.assertTrue(api.getPosProfile().get("requires_outlet_selection"))

        # Unauthorized 3rd outlet refused with 403 PermissionError
        with self.assertRaises(NotPermitted) as ctx:
            oc.set_active_outlet("VIP Lounge POS")
        self.assertIn("[POS-OUTLET-FORBIDDEN]", str(ctx.exception))
        self.assertNotRegex(str(ctx.exception), VENDOR_RE)

        # Cross-outlet getRestaurantMenu also refused fail-closed
        with self.assertRaises(NotPermitted):
            api.getRestaurantMenu("VIP Lounge POS")

    def test_revoked_permission_invalidates_cached_outlet_fail_closed(self):
        store, _frappe, oc, api, _qr = self._env()
        oc.set_active_outlet("Pool Bar POS")
        self.assertEqual(api.getBranch(), "Pool Bar")

        # Admin revokes Pool Bar User Permission mid-session
        store.user_permissions["staff@example.test"]["Branch"] = [{"doc": "Main Grill", "is_default": 1}]
        # Must fail closed on stale cache and fall back only to permitted branch
        self.assertEqual(api.getBranch(), "Main Grill")
        self.assertEqual(api.getPosProfile()["pos_profile"], "Main Grill POS")

    def test_open_till_cashier_guard_blocks_opening_second_outlet(self):
        store, _frappe, oc, api, _qr = self._env()
        store.open_shifts.append(
            Row(
                name="POS-OPE-GRILL-1",
                pos_profile="Main Grill POS",
                branch="Main Grill",
                company="Maximus Hospitality",
                user="staff@example.test",
                status="Open",
                docstatus=1,
                posting_date="2026-11-24",
                custom_business_date="2026-11-23",
            )
        )
        # Waiter/Manager can still switch context to view/take orders on Pool Bar POS
        switched = oc.set_active_outlet("Pool Bar POS")
        self.assertEqual(switched["pos_profile"], "Pool Bar POS")
        self.assertEqual(switched["open_till_elsewhere"]["pos_profile"], "Main Grill POS")

        # Opening a till on Pool Bar POS while Main Grill till is open refuses [POS-OUTLET-OPEN-TILL-ACTIVE]
        with self.assertRaises(Refused) as ctx:
            api.create_pos_opening_entry("Pool Bar POS", balance_details=[{"mode_of_payment": "USD Cash", "opening_amount": 100}])
        self.assertIn("[POS-OUTLET-OPEN-TILL-ACTIVE]", str(ctx.exception))
        self.assertNotRegex(str(ctx.exception), VENDOR_RE)

        # Direct guard used by make_invoice settlement also refuses
        with self.assertRaises(Refused) as ctx:
            oc.assert_no_conflicting_open_till("Pool Bar POS", user="staff@example.test")
        self.assertIn("[POS-OUTLET-OPEN-TILL-ACTIVE]", str(ctx.exception))

    def test_g44_business_date_and_shift_x_report_present_and_graceful_absent(self):
        # Present case (siteops#8609 installed)
        store, _frappe, oc, api, qr = self._env(has_business_date=True, has_day_close=True)
        store.open_shifts.append(
            Row(
                name="POS-OPE-GRILL-1",
                pos_profile="Main Grill POS",
                branch="Main Grill",
                company="Maximus Hospitality",
                user="staff@example.test",
                status="Open",
                docstatus=1,
                posting_date="2026-11-25",
                custom_business_date="2026-11-24",
            )
        )
        oc.set_active_outlet("Main Grill POS")
        prof = api.getPosProfile()
        self.assertEqual(prof["active_business_date"], "2026-11-24")
        self.assertEqual(prof["active_opening_entry"], "POS-OPE-GRILL-1")
        self.assertTrue(prof["shift_report_available"])

        report = oc.get_shift_x_report("Main Grill POS")
        self.assertTrue(report["available"])
        self.assertEqual(report["report_type"], "X")
        self.assertEqual(report["business_date"], "2026-11-24")
        self.assertEqual(report["rows"][0]["amount"], 150000)
        qr.run.assert_called_once()

        # Absent case (siteops#8609 not installed) -> degrades gracefully!
        store2, _frappe2, oc2, api2, _qr2 = self._env(has_business_date=False, has_day_close=False)
        oc2.set_active_outlet("Main Grill POS")
        prof2 = api2.getPosProfile()
        self.assertIsNone(prof2["active_business_date"])
        self.assertFalse(prof2["shift_report_available"])
        report2 = oc2.get_shift_x_report("Main Grill POS")
        self.assertFalse(report2["available"])


if __name__ == "__main__":
    unittest.main()
