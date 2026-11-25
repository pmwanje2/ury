"""RED -> GREEN regression tests for Hydrogen R3 P1 finding on pmwanje2/ury#154 (R4):

Cross-waiter `Merge Rounds` (`collision_merge=True`) under ERPNext `Employee` User Permissions:
- When Captain #1 owns `POS Invoice` (`custom_waiter_employee = HR-EMP-00003`) and Captain #2
  (`HR-EMP-00004`) collides offline under `Merge Rounds`, `ury_order.sync_order` saves with
  `ignore_permissions=collision_merge` and must also invoke internal `_execute_kot(...,
  ignore_permissions=True)` (NOT exposing `ignore_permissions` on whitelisted `kot_execute`).
- Direct calls to `@frappe.whitelist() kot_execute` and `_execute_kot(..., ignore_permissions=False)`
  must continue enforcing `frappe.has_permission("POS Invoice", "write", doc=pos_invoice)`.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

APP = Path(__file__).resolve().parents[1]
KOT_GEN_PY = APP / "ury" / "api" / "ury_kot_generate.py"
URY_ORDER_PY = APP / "ury" / "doctype" / "ury_order" / "ury_order.py"


class _PermError(Exception):
    pass


class _ValError(Exception):
    pass


def _load_kot_generate():
    saved = {
        k: sys.modules.get(k)
        for k in (
            "frappe",
            "frappe.utils",
            "ury.ury.api.ury_kot_routing",
            "ury.ury.api.ury_production_context",
            "ury.ury.api.ury_order_reservation_service",
            "ury.ury.api.ury_waiter_print",
            "ury.ury.api.ury_mto_work_order_service",
            "ury_kot_generate_r4_test",
        )
    }

    frappe = ModuleType("frappe")
    frappe.PermissionError = _PermError
    frappe.ValidationError = _ValError
    frappe.whitelist = lambda *a, **kw: (lambda fn: fn) if not a or not callable(a[0]) else a[0]
    frappe._ = lambda s: s
    frappe.flags = SimpleNamespace()
    frappe.msgprint = lambda *a, **kw: None
    frappe.log_error = lambda *a, **kw: None
    frappe.get_traceback = lambda: ""
    frappe.get_precision = lambda *a, **kw: 2

    created_kots = []

    class FakeKotDoc:
        def __init__(self, data):
            self.data = dict(data)
            self.name = f"KOT-TEST-{len(created_kots) + 1}"
            self.kot_items = []
            self.flags = SimpleNamespace()
            self.insert_kwargs = None
            self.submitted = False
            self.creation = "2026-11-24 12:00:00"

        def append(self, field, row):
            self.kot_items.append(row)

        def insert(self, **kwargs):
            self.insert_kwargs = kwargs
            created_kots.append(self)
            return self

        def submit(self):
            self.submitted = True
            return self

    pos_invoice = SimpleNamespace(
        name="KR3-00099",
        pos_profile="Main Profile",
        company="Hotel Co",
        branch="Main Branch",
        order_type="Dine In",
        restaurant_table="T-1",
        custom_ury_order_number="1",
        custom_aggregator_id=None,
        get=lambda k, d=None: {
            "order_type": "Dine In",
            "restaurant_table": "T-1",
            "items": [],
        }.get(k, d),
    )
    pos_profile = SimpleNamespace(
        name="Main Profile",
        branch="Main Branch",
        custom_kot_naming_series="KOT-.YYYY.-.#####",
    )

    def get_doc(arg, name=None):
        if isinstance(arg, dict):
            return FakeKotDoc(arg)
        if arg == "POS Invoice":
            return pos_invoice
        if arg == "POS Profile":
            return pos_profile
        raise KeyError((arg, name))

    frappe.get_doc = get_doc
    frappe.has_permission = MagicMock(return_value=False)
    frappe.db = SimpleNamespace(
        get_all=lambda doctype, **kw: [SimpleNamespace(name="Hot Kitchen")] if doctype == "URY Production Unit" else [],
        get_value=lambda *a, **kw: None,
        exists=lambda *a, **kw: None,
    )

    utils = ModuleType("frappe.utils")
    utils.cint = lambda v: int(v or 0)
    utils.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
    utils.get_datetime = lambda v: v

    def throw(msg, exc=_ValError):
        raise exc(msg)

    frappe.throw = throw

    routing = ModuleType("ury.ury.api.ury_kot_routing")
    routing.resolve_production_units = lambda **kw: ["Hot Kitchen"]
    prod_ctx = ModuleType("ury.ury.api.ury_production_context")
    prod_ctx.resolve_production_context = lambda *a, **kw: None
    res_svc = ModuleType("ury.ury.api.ury_order_reservation_service")
    res_svc._line_ref = lambda row: row.get("reservation_line_key")
    res_svc._line_context = lambda row: {"comments": row.get("comments", "")}
    res_svc._line_key = lambda code, row, idx: f"{code}:{idx}"
    waiter_print = ModuleType("ury.ury.api.ury_waiter_print")
    waiter_print.print_combined_waiter_order_slip = lambda *a, **kw: None
    mto_svc = ModuleType("ury.ury.api.ury_mto_work_order_service")
    mto_svc.create_work_orders_for_kot = lambda *a, **kw: None

    sys.modules["frappe"] = frappe
    sys.modules["frappe.utils"] = utils
    sys.modules["ury.ury.api.ury_kot_routing"] = routing
    sys.modules["ury.ury.api.ury_production_context"] = prod_ctx
    sys.modules["ury.ury.api.ury_order_reservation_service"] = res_svc
    sys.modules["ury.ury.api.ury_waiter_print"] = waiter_print
    sys.modules["ury.ury.api.ury_mto_work_order_service"] = mto_svc

    spec = importlib.util.spec_from_file_location("ury_kot_generate_r4_test", KOT_GEN_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ury_kot_generate_r4_test"] = mod
    spec.loader.exec_module(mod)
    return mod, frappe, created_kots, saved


def _restore_modules(saved):
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


class TestR4CollisionMergeKotPermissions(unittest.TestCase):
    def test_whitelisted_kot_execute_never_accepts_ignore_permissions(self):
        mod, frappe, _created, saved = _load_kot_generate()
        try:
            pub_params = inspect.signature(mod.kot_execute).parameters
            self.assertNotIn(
                "ignore_permissions",
                pub_params,
                "Whitelisted kot_execute must never expose ignore_permissions to HTTP callers",
            )
            with self.assertRaises(_PermError):
                mod.kot_execute(
                    "KR3-00099",
                    "Walk-in",
                    "T-1",
                    [{"item": "TEA", "item_name": "Tea", "qty": 1}],
                    [],
                )
        finally:
            _restore_modules(saved)

    def test_internal_execute_kot_supports_ignore_permissions_for_collision_merge(self):
        mod, frappe, created_kots, saved = _load_kot_generate()
        try:
            priv_params = inspect.signature(mod._execute_kot).parameters
            self.assertIn("ignore_permissions", priv_params)
            self.assertEqual(priv_params["ignore_permissions"].default, False)

            # Default ignore_permissions=False still blocks when write permission is denied
            with self.assertRaises(_PermError):
                mod._execute_kot(
                    "KR3-00099",
                    "Walk-in",
                    "T-1",
                    [{"item": "TEA", "item_name": "Tea", "qty": 1}],
                    [],
                    desk=True,
                )

            # Internal collision_merge path with ignore_permissions=True creates & submits KOT
            mod._execute_kot(
                "KR3-00099",
                "Walk-in",
                "T-1",
                [{"item": "TEA", "item_name": "Tea", "qty": 1}],
                [],
                desk=True,
                ignore_permissions=True,
            )
            self.assertEqual(len(created_kots), 1)
            self.assertEqual(created_kots[0].insert_kwargs, {"ignore_permissions": True})
            self.assertTrue(created_kots[0].submitted)
            self.assertTrue(getattr(created_kots[0].flags, "ignore_permissions", False))
        finally:
            _restore_modules(saved)

    def test_sync_order_source_passes_ignore_permissions_on_collision_merge(self):
        src = URY_ORDER_PY.read_text()
        self.assertIn(
            'kot_kwargs["ignore_permissions"] = True',
            src,
            "sync_order must pass ignore_permissions=True to internal kot_execute when collision_merge is active",
        )


if __name__ == "__main__":
    unittest.main()
