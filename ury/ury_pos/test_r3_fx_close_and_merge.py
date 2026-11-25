"""RED -> GREEN regression tests for Hydrogen R2 P1 defects on pmwanje2/ury#154 (R3 Neon).

Covers:
1. [P1 #1] UI Controlled Close (`POSClosingDialog` -> `close_till_with_controls` & `force_close_till`)
   wipes ALL FX counted amounts to `0`:
   - `_parse_counted_payments` must preserve `custom_closing_foreign_amount` for FX modes and
     validate currency decimal precision across UGX (0dp), USD (2dp), and KWD (3dp).
   - `close_till_with_controls` and `force_close_till` must pass `custom_closing_foreign_amount`
     onto `POS Closing Entry Detail` rows BEFORE `closing.insert()` runs `_set_expected_amounts`.
   - Exact zero-variance FX counts close cleanly (`docstatus == 1`, zero `foreign_difference`,
     zero `difference`), while real FX variances still enforce `[POS-CLOSE-OVER-SHORT-REASON]`
     / `[POS-CLOSE-VARIANCE-APPROVAL]`.
2. [P1 #2] Merged-bill settlement (`custom_merged_pos_invoice`) fails `[POS-FX-AMOUNT-MISMATCH]`
   on FX over-tender:
   - `existing` branch (`make_invoice`, leftover over-tender added onto primary invoice's existing
     FX payment row): preserves `custom_tender_amount`, `custom_tender_currency`,
     `custom_exchange_rate` so `validate_invoice_fx_payments` passes on BOTH primary and partner,
     total FX sums to exact tendered amount, and local change (`120,000 UGX`) computes accurately.
   - `else:` branch (`doc_req == 0` before FX tender, e.g. local Cash paid primary and FX over-tender
     paid partner with leftover landing on primary): carries `custom_tender_currency`,
     `custom_tender_amount`, `custom_exchange_rate`, `custom_exchange_rate_date` so BOTH invoices
     pass `validate_invoice_fx_payments` and local change computes accurately.
   - High-rate 3dp KWD merged-bill over-tender (`100,000 + 150,006 UGX` paid `25.000 KWD @ 12345`)
     passes validation without 4dp quantization false `[POS-FX-AMOUNT-MISMATCH]`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from ury.ury_pos.test_till_cash_controls import (
    APP,
    CASH_MOVEMENT_HOOK,
    DRAWER_EVENT_DIR,
    TILL_CONTROLS_PY,
    FakeDoc,
    PermissionDenied,
    Refused,
    Row,
    _build_env,
)
from ury.ury_pos.test_till_close_controls import CLOSING_HOOK, ClosingStore

FX_PY = APP / "ury_pos" / "fx.py"
URY_ORDER_PY = APP / "ury" / "doctype" / "ury_order" / "ury_order.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestR3ControlledCloseFxPreservation(unittest.TestCase):
    """[P1 #1] close_till_with_controls & force_close_till preserve FX counted amounts."""

    def setUp(self):
        self.store, self.override, self.frappe, brand_spec, brand_mod, modules = _build_env()

        # Configure UGX (0dp), USD (2dp), KWD (3dp) currencies and FX modes in store
        self.store.currencies["UGX"] = Row(name="UGX", fraction_units=1, smallest_currency_fraction_value=1)
        self.store.currencies["USD"] = Row(name="USD", fraction_units=100, smallest_currency_fraction_value=0.01)
        self.store.currencies["KWD"] = Row(name="KWD", fraction_units=1000, smallest_currency_fraction_value=0.001)
        self.store.accounts["FX Clearing USD - HC"] = Row(
            name="FX Clearing USD - HC",
            company="Hotel Co",
            account_currency="UGX",
            root_type="Asset",
            is_group=0,
            disabled=0,
        )
        self.store.accounts["FX Clearing KWD - HC"] = Row(
            name="FX Clearing KWD - HC",
            company="Hotel Co",
            account_currency="UGX",
            root_type="Asset",
            is_group=0,
            disabled=0,
        )
        self.store.accounts["USD Vault - HC"] = Row(
            name="USD Vault - HC",
            company="Hotel Co",
            account_currency="USD",
            root_type="Asset",
            is_group=0,
            disabled=0,
        )
        self.store.accounts["KWD Vault - HC"] = Row(
            name="KWD Vault - HC",
            company="Hotel Co",
            account_currency="KWD",
            root_type="Asset",
            is_group=0,
            disabled=0,
        )
        self.store.modes["USD Cash"] = Row(
            name="USD Cash",
            type="Cash",
            custom_overtender_rule="Refuse",
            custom_halo_max_amount=500000,
            custom_tender_currency="USD",
            custom_fx_rate_type="Buying",
        )
        self.store.modes["KWD Cash"] = Row(
            name="KWD Cash",
            type="Cash",
            custom_overtender_rule="Refuse",
            custom_halo_max_amount=500000,
            custom_tender_currency="KWD",
            custom_fx_rate_type="Buying",
        )
        self.store.mode_accounts[("USD Cash", "Hotel Co")] = "FX Clearing USD - HC"
        self.store.mode_accounts[("KWD Cash", "Hotel Co")] = "FX Clearing KWD - HC"

        profile = self.store.profiles["Main Till"]
        profile["currency"] = "UGX"
        profile["custom_over_short_tolerance"] = 0
        profile["custom_require_over_short_reason"] = 1
        profile["custom_over_short_reason_tolerance"] = 0
        profile["payments"] = [
            Row({"mode_of_payment": "Cash"}),
            Row({"mode_of_payment": "USD Cash"}),
            Row({"mode_of_payment": "KWD Cash"}),
        ]

        opening = self.store.openings["OPEN-MAIN-1"]
        opening["balance_details"] = [
            Row({"mode_of_payment": "Cash", "opening_amount": 0}),
            Row({"mode_of_payment": "USD Cash", "opening_amount": 0}),
            Row({"mode_of_payment": "KWD Cash", "opening_amount": 0}),
        ]

        # Wire Mode of Payment custom_tender_currency lookup in db.get_value + db.sql
        orig_get_value = self.frappe.db.get_value.side_effect

        def _get_value_with_fx(doctype, filters=None, fieldname=None, as_dict=False, for_update=False, **kwargs):
            if doctype == "Mode of Payment" and isinstance(filters, str):
                mop = self.store.modes.get(filters)
                if mop:
                    if isinstance(fieldname, (list, tuple)):
                        data = {f: mop.get(f) for f in fieldname}
                        return Row(data) if as_dict else tuple(data[f] for f in fieldname)
                    if fieldname in mop:
                        return mop.get(fieldname)
            if doctype == "POS Profile" and isinstance(filters, str):
                prof = self.store.profiles.get(filters)
                if prof and isinstance(fieldname, (list, tuple)):
                    data = {f: prof.get(f) for f in fieldname}
                    return Row(data) if as_dict else tuple(data[f] for f in fieldname)
            return orig_get_value(doctype, filters, fieldname, as_dict=as_dict, for_update=for_update, **kwargs)

        self.frappe.db.get_value.side_effect = _get_value_with_fx

        def _sql_with_fx(query, params=None, as_dict=False, **kwargs):
            q = str(query)
            if "FROM `tabMode of Payment`" in q and "custom_tender_currency" in q:
                modes = params[0] if params else ()
                out = []
                for m in modes:
                    curr = (self.store.modes.get(m) or {}).get("custom_tender_currency")
                    if curr:
                        out.append(Row({"name": m, "custom_tender_currency": curr}))
                return out
            if "FROM `tabSales Invoice Payment`" in q and "custom_tender_amount" in q:
                inv_names, fx_modes = params
                totals: dict[str, float] = {}
                for inv_name in inv_names:
                    inv = self.store.pos_invoices.get(inv_name)
                    if not inv:
                        continue
                    for p in inv.get("payments") or []:
                        m = p.get("mode_of_payment")
                        if m in fx_modes:
                            totals[m] = totals.get(m, 0.0) + float(p.get("custom_tender_amount") or 0)
                return [Row({"mode_of_payment": m, "total_fx": val}) for m, val in totals.items()]
            return []

        self.frappe.db.sql = MagicMock(side_effect=_sql_with_fx)
        self.frappe.ValidationError = Refused
        self.frappe.PermissionError = PermissionDenied
        self.frappe.utils.today = lambda: "2026-11-24"

        # ERPNext closing helpers stub
        pce_mod = ModuleType("erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry")

        def get_payment_rows_by_invoice(invoices):
            out = {}
            for inv in invoices:
                full = self.store.pos_invoices.get(inv.name)
                if full:
                    out[("POS Invoice", inv.name)] = [Row(p) for p in (full.get("payments") or [])]
            return out

        def get_change_payment_row(rows, change_account):
            for r in rows:
                if r.get("account") == change_account:
                    return r
            return None

        pce_mod.get_payment_rows_by_invoice = get_payment_rows_by_invoice
        pce_mod.get_change_payment_row = get_change_payment_row
        pce_mod.get_taxes = lambda invoices: []

        modules["erpnext.accounts"] = ModuleType("erpnext.accounts")
        modules["erpnext.accounts.doctype"] = ModuleType("erpnext.accounts.doctype")
        modules["erpnext.accounts.doctype.pos_closing_entry"] = ModuleType("erpnext.accounts.doctype.pos_closing_entry")
        modules["erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry"] = pce_mod

        api_stub = modules["ury.ury_pos.api"]
        api_stub._goal_submitted_since = lambda *a, **kw: True
        api_stub._phase_dependent_rows = lambda *a, **kw: []
        api_stub._role_rank = lambda *a, **kw: None
        api_stub._till_invoice_rows = lambda pos_profile, start, end, reader: [
            d
            for d in self.store.pos_invoices.values()
            if d.get("pos_profile") == pos_profile
            and d.get("docstatus") == 1
            and not d.get("consolidated_invoice")
        ]

        self.enterContext(patch.dict(sys.modules, modules))
        brand_spec.loader.exec_module(brand_mod)

        ctrl_mod = _load_module(
            "ury.ury.doctype.pos_drawer_event.pos_drawer_event",
            DRAWER_EVENT_DIR / "pos_drawer_event.py",
        )
        self.enterContext(patch.dict(sys.modules, {"ury.ury.doctype.pos_drawer_event.pos_drawer_event": ctrl_mod}))
        self.store.drawer_event_cls = ctrl_mod.POSDrawerEvent

        self.fx = _load_module("ury.ury_pos.fx", FX_PY)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.fx": self.fx}))

        self.cash_hook = _load_module("ury.ury.hooks.ury_cash_movement", CASH_MOVEMENT_HOOK)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_cash_movement": self.cash_hook}))
        self.store.je_hook_mod = self.cash_hook
        self.enterContext(patch.object(self.cash_hook, "till_cash_movements", return_value={}))

        self.tc = _load_module("ury.ury_pos.till_controls", TILL_CONTROLS_PY)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.till_controls": self.tc}))

        self.closing_hook = _load_module("ury.ury.hooks.ury_pos_closing_entry", CLOSING_HOOK)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_pos_closing_entry": self.closing_hook}))

        self.cs = ClosingStore(self.store, self.frappe)
        self.cs.closing_hook_mod = self.closing_hook

        # Re-hook db.get_value on top of ClosingStore so Mode of Payment FX fields stay visible
        cs_get_value = self.frappe.db.get_value.side_effect

        def _combined_get_value(doctype, filters=None, fieldname=None, as_dict=False, for_update=False, **kwargs):
            if doctype == "Mode of Payment" and isinstance(filters, str):
                mop = self.store.modes.get(filters)
                if mop:
                    if isinstance(fieldname, (list, tuple)):
                        data = {f: mop.get(f) for f in fieldname}
                        return Row(data) if as_dict else tuple(data[f] for f in fieldname)
                    if fieldname in mop:
                        return mop.get(fieldname)
            if doctype == "POS Profile" and isinstance(filters, str):
                prof = self.store.profiles.get(filters)
                if prof and isinstance(fieldname, (list, tuple)):
                    data = {f: prof.get(f) for f in fieldname}
                    return Row(data) if as_dict else tuple(data[f] for f in fieldname)
            return cs_get_value(doctype, filters, fieldname, as_dict=as_dict, for_update=for_update, **kwargs)

        self.frappe.db.get_value.side_effect = _combined_get_value

        # Shift sales matching Hydrogen R2 Probe #15a:
        # Cash = 36,075 UGX, USD Cash = 12,037 UGX (3.24 USD), KWD Cash = 13,888 UGX (1.125 KWD)
        self.store.pos_invoices["KR2B00049"] = FakeDoc(
            {
                "doctype": "POS Invoice",
                "name": "KR2B00049",
                "docstatus": 1,
                "pos_profile": "Main Till",
                "customer": "Walk-in",
                "consolidated_invoice": None,
                "posting_date": "2026-11-24",
                "posting_time": "09:15:00",
                "grand_total": 62000,
                "net_total": 62000,
                "rounded_total": 62000,
                "total_qty": 3,
                "total_taxes_and_charges": 0,
                "change_amount": 0,
                "account_for_change_amount": "Cash - HC",
                "is_return": 0,
                "return_against": None,
                "payments": [
                    {
                        "mode_of_payment": "Cash",
                        "account": "Cash - HC",
                        "amount": 36075,
                    },
                    {
                        "mode_of_payment": "USD Cash",
                        "account": "FX Clearing USD - HC",
                        "amount": 12037,
                        "custom_tender_currency": "USD",
                        "custom_tender_amount": 3.24,
                        "custom_exchange_rate": 3715.12345679,
                    },
                    {
                        "mode_of_payment": "KWD Cash",
                        "account": "FX Clearing KWD - HC",
                        "amount": 13888,
                        "custom_tender_currency": "KWD",
                        "custom_tender_amount": 1.125,
                        "custom_exchange_rate": 12344.888888889,
                    },
                ],
            },
            store=self.store,
        )

    def test_parse_counted_payments_preserves_fx_amounts_and_enforces_precision(self):
        parsed = self.tc._parse_counted_payments(
            [
                {"mode_of_payment": "Cash", "closing_amount": 36075},
                {"mode_of_payment": "USD Cash", "closing_amount": 12037, "custom_closing_foreign_amount": 3.24},
                {"mode_of_payment": "KWD Cash", "closing_amount": 13888, "custom_closing_foreign_amount": 1.125},
            ],
            "UGX",
        )
        by_mode = {row["mode_of_payment"]: row for row in parsed}
        self.assertNotIn("custom_closing_foreign_amount", by_mode["Cash"])
        self.assertEqual(by_mode["USD Cash"]["custom_closing_foreign_amount"], 3.24)
        self.assertEqual(by_mode["KWD Cash"]["custom_closing_foreign_amount"], 1.125)

        # Reject >2dp on USD and >3dp on KWD with [POS-CASH-PRECISION]
        with self.assertRaisesRegex(Refused, r"\[POS-CASH-PRECISION\]"):
            self.tc._parse_counted_payments(
                [{"mode_of_payment": "USD Cash", "closing_amount": 12037, "custom_closing_foreign_amount": 3.245}],
                "UGX",
            )
        with self.assertRaisesRegex(Refused, r"\[POS-CASH-PRECISION\]"):
            self.tc._parse_counted_payments(
                [{"mode_of_payment": "KWD Cash", "closing_amount": 13888, "custom_closing_foreign_amount": 1.1254}],
                "UGX",
            )
        with self.assertRaisesRegex(Refused, r"\[POS-CLOSE-NEGATIVE-AMOUNT\]"):
            self.tc._parse_counted_payments(
                [{"mode_of_payment": "USD Cash", "closing_amount": 0, "custom_closing_foreign_amount": -1.0}],
                "UGX",
            )

    def test_close_till_with_controls_exact_zero_variance_fx_closes_cleanly(self):
        # Hydrogen R2 live repro Probe #15a: exact zero-variance counts across UGX/USD/KWD
        # MUST close cleanly without requiring an Over/Short reason or PIN!
        self.store.current_user = "cashier@example.com"
        self.frappe.session.user = "cashier@example.com"
        result = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[
                {"mode_of_payment": "Cash", "closing_amount": 36075},
                {"mode_of_payment": "USD Cash", "closing_amount": 12037, "custom_closing_foreign_amount": 3.24},
                {"mode_of_payment": "KWD Cash", "closing_amount": 13888, "custom_closing_foreign_amount": 1.125},
            ],
            period_end_date="2026-11-24 23:00:00",
        )
        self.assertEqual(result["docstatus"], 1)
        closing = self.cs.closings[result["name"]]
        rows = {r.mode_of_payment: r for r in closing.payment_reconciliation}
        self.assertEqual(rows["USD Cash"].custom_closing_foreign_amount, 3.24)
        self.assertEqual(rows["USD Cash"].custom_foreign_difference, 0.0)
        self.assertEqual(rows["USD Cash"].difference, 0.0)
        self.assertEqual(rows["KWD Cash"].custom_closing_foreign_amount, 1.125)
        self.assertEqual(rows["KWD Cash"].custom_foreign_difference, 0.0)
        self.assertEqual(rows["KWD Cash"].difference, 0.0)

    def test_close_till_with_controls_real_fx_variance_still_requires_reason(self):
        # Manager closing with a real 0.04 USD shortage and NO reason MUST still raise
        # [POS-CLOSE-OVER-SHORT-REASON].
        self.store.current_user = "manager@example.com"
        self.frappe.session.user = "manager@example.com"
        self.store.openings["OPEN-MAIN-1"]["user"] = "manager@example.com"
        with self.assertRaisesRegex(Refused, r"\[POS-CLOSE-OVER-SHORT-REASON\]"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[
                    {"mode_of_payment": "Cash", "closing_amount": 36075},
                    {"mode_of_payment": "USD Cash", "closing_amount": 11888, "custom_closing_foreign_amount": 3.20},
                    {"mode_of_payment": "KWD Cash", "closing_amount": 13888, "custom_closing_foreign_amount": 1.125},
                ],
                period_end_date="2026-11-24 23:00:00",
            )

    def test_force_close_till_preserves_fx_counted_amounts(self):
        self.store.current_user = "manager@example.com"
        self.frappe.session.user = "manager@example.com"
        result = self.tc.force_close_till(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[
                {"mode_of_payment": "Cash", "closing_amount": 36075},
                {"mode_of_payment": "USD Cash", "closing_amount": 12037, "custom_closing_foreign_amount": 3.24},
                {"mode_of_payment": "KWD Cash", "closing_amount": 13888, "custom_closing_foreign_amount": 1.125},
            ],
            reason="End of day force close",
            period_end_date="2026-11-24 23:00:00",
        )
        self.assertEqual(result["docstatus"], 1)
        closing = self.cs.closings[result["name"]]
        rows = {r.mode_of_payment: r for r in closing.payment_reconciliation}
        self.assertEqual(rows["USD Cash"].custom_closing_foreign_amount, 3.24)
        self.assertEqual(rows["USD Cash"].custom_foreign_difference, 0.0)
        self.assertEqual(rows["KWD Cash"].custom_closing_foreign_amount, 1.125)
        self.assertEqual(rows["KWD Cash"].custom_foreign_difference, 0.0)


class _Dict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key, value):
        self[key] = value


class _FakeInvoice(_Dict):
    def __init__(self, data, *, validate_fn=None):
        super().__init__(data)
        self["payments"] = [_Dict(p) for p in data.get("payments", [])]
        self["items"] = [_Dict(i) for i in data.get("items", [])]
        object.__setattr__(self, "flags", _Dict())
        object.__setattr__(self, "_validate_fn", validate_fn)

    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        if key == "payments":
            self[key] = [_Dict(r) for r in (value or [])]
        else:
            self[key] = value

    def append(self, key, row):
        arr = self.setdefault(key, [])
        d = _Dict(row)
        arr.append(d)
        return d

    def precision(self, field=None):
        curr = (self.get("currency") or "UGX").upper()
        if curr == "UGX":
            return 0
        if curr == "KWD":
            return 3
        return 2

    def calculate_taxes_and_totals(self):
        return None

    def set_paid_amount(self):
        self["paid_amount"] = round(sum(float(r.get("amount") or 0) for r in self["payments"]), self.precision())

    def set_outstanding_amount(self):
        due = float(self.get("rounded_total") or self.get("grand_total") or 0)
        paid = float(self.get("paid_amount") or 0)
        self["outstanding_amount"] = max(0.0, round(due - paid, self.precision()))

    def calculate_change_amount(self):
        due = float(self.get("rounded_total") or self.get("grand_total") or 0)
        paid = float(self.get("paid_amount") or 0)
        self["change_amount"] = max(0.0, round(paid - due, self.precision()))

    def save(self, ignore_permissions=False):
        self.set_paid_amount()
        self.set_outstanding_amount()
        self.calculate_change_amount()
        validate_fn = object.__getattribute__(self, "_validate_fn")
        if callable(validate_fn):
            validate_fn(self)
        return self


class TestR3MergedBillFxOvertender(unittest.TestCase):
    """[P1 #2] Merged-bill settlement allocator preserves FX fields & conversion math."""

    def setUp(self):
        self.ValidationError = type("ValidationError", (Exception,), {})
        self.PermissionError = type("PermissionError", (Exception,), {})

        self.frappe = ModuleType("frappe")
        self.frappe._ = lambda msg: msg
        self.frappe._dict = _Dict
        self.frappe.ValidationError = self.ValidationError
        self.frappe.PermissionError = self.PermissionError
        self.frappe.UniqueValidationError = type("UniqueValidationError", (Exception,), {})
        self.frappe.flags = _Dict()
        self.frappe.session = _Dict(user="cashier@hotel.test")
        self.frappe.cache = MagicMock()
        self.frappe.whitelist = lambda *a, **kw: (lambda fn: fn) if not a or not callable(a[0]) else a[0]

        def _throw(msg, exc=None):
            raise (exc or self.ValidationError)(msg)

        self.frappe.throw = _throw
        self.frappe.parse_json = lambda v: v
        self.frappe.get_meta = MagicMock(return_value=SimpleNamespace(has_field=lambda _f: False))
        self.frappe.get_precision = MagicMock(return_value=0)
        self.frappe.get_value = MagicMock(return_value="Dine In")
        self.frappe.get_roles = MagicMock(return_value=["URY Cashier"])

        self.profile = _Dict(
            name="KH154 POS",
            company="Grand Hotel",
            currency="UGX",
            account_for_change_amount="Cash - GH",
            payments=[
                _Dict(mode_of_payment="Cash"),
                _Dict(mode_of_payment="USD Cash"),
                _Dict(mode_of_payment="KWD Cash"),
            ],
        )
        self.frappe.get_cached_doc = MagicMock(return_value=self.profile)
        self.frappe.get_cached_value = MagicMock(return_value="Cash")

        self.mops = {
            "Cash": {
                "name": "Cash",
                "type": "Cash",
                "custom_reference_mode": "Hidden",
                "custom_tender_currency": "",
                "default_account": "Cash - GH",
            },
            "USD Cash": {
                "name": "USD Cash",
                "type": "Cash",
                "custom_reference_mode": "Hidden",
                "custom_tender_currency": "USD",
                "custom_fx_rate_type": "Buying",
                "default_account": "FX Clearing USD - GH",
                "custom_fx_vault_account": "USD Vault - GH",
            },
            "KWD Cash": {
                "name": "KWD Cash",
                "type": "Cash",
                "custom_reference_mode": "Hidden",
                "custom_tender_currency": "KWD",
                "custom_fx_rate_type": "Buying",
                "default_account": "FX Clearing KWD - GH",
                "custom_fx_vault_account": "KWD Vault - GH",
            },
        }
        self.rates = {
            ("USD", "UGX"): 3700.0,
            ("KWD", "UGX"): 12345.0,
        }

        def _db_get_value(doctype, filters=None, fieldname=None, as_dict=False, for_update=False, **kwargs):
            if doctype == "POS Invoice" and fieldname == "docstatus":
                return 0
            if doctype == "Mode of Payment" and isinstance(filters, str):
                mop = self.mops.get(filters) or {}
                if isinstance(fieldname, (list, tuple)):
                    data = {f: mop.get(f) for f in fieldname}
                    return _Dict(data) if as_dict else tuple(data[f] for f in fieldname)
                return mop.get(fieldname)
            if doctype == "Mode of Payment Account" and isinstance(filters, dict):
                parent = filters.get("parent")
                mop = self.mops.get(parent) or {}
                if isinstance(fieldname, (list, tuple)):
                    data = {f: mop.get(f) for f in fieldname}
                    return _Dict(data) if as_dict else tuple(data[f] for f in fieldname)
                return mop.get(fieldname)
            if doctype == "Workflow":
                return None
            return None

        def _get_all(doctype, filters=None, fields=None, **kwargs):
            if doctype == "Currency Exchange" and isinstance(filters, dict):
                pair = (filters.get("from_currency"), filters.get("to_currency"))
                rate = self.rates.get(pair)
                if rate:
                    return [_Dict(name=f"CE-{pair[0]}", date="2026-11-24", exchange_rate=rate)]
            return []

        self.frappe.db = SimpleNamespace(
            get_value=MagicMock(side_effect=_db_get_value),
            get_single_value=MagicMock(return_value=1),
            exists=MagicMock(return_value=False),
            table_exists=MagicMock(return_value=False),
            sql=MagicMock(return_value=[]),
        )
        self.frappe.get_all = MagicMock(side_effect=_get_all)
        self.frappe.get_doc = MagicMock()

        def _flt(val, precision=None):
            num = float(val or 0)
            if precision is None:
                return num
            return round(num, int(precision))

        frappe_utils = ModuleType("frappe.utils")
        frappe_utils.cint = lambda v: int(float(v or 0))
        frappe_utils.flt = _flt
        frappe_utils.today = lambda: "2026-11-24"
        frappe_utils.get_datetime = lambda v=None: v
        frappe_utils.get_system_timezone = lambda: "UTC"
        frappe_utils.now_datetime = lambda: None
        self.frappe.utils = frappe_utils

        frappe_model = ModuleType("frappe.model")
        frappe_model_doc = ModuleType("frappe.model.document")
        frappe_model_doc.Document = object

        erp_pkg = ModuleType("erpnext")
        erp_ctrl = ModuleType("erpnext.controllers")
        erp_queries = ModuleType("erpnext.controllers.queries")
        erp_queries.item_query = lambda *a, **kw: []

        # Minimal stub modules for non-FX dependencies imported at top of ury_order.py
        api_stub = ModuleType("ury.ury_pos.api")
        api_stub.getBranch = lambda: "Kampala"
        api_stub.getBranchRoom = lambda: []
        api_stub.getRoom = lambda: []
        api_stub.posOpening = lambda: {}

        dc_stub = ModuleType("ury.ury_pos.default_customer")
        dc_stub.get_default_customer_config = lambda *a, **kw: {}
        dc_stub.resolve_default_customer = lambda *a, **kw: None

        void_stub = ModuleType("ury.ury_pos.void_return_reopen")

        disc_stub = ModuleType("ury.ury_pos.discount")
        disc_stub.parse_discount_request = lambda **kw: {"has_any_arg": False}
        disc_stub.apply_discount_to_invoice = lambda *a, **kw: None
        disc_stub.with_currency_precision = lambda _inv: contextlib.nullcontext()
        for attr in (
            "canonical_split_discount_claim",
            "currency_precision",
            "enforce_remove_discount_auth",
            "get_check_discounts",
            "list_discount_options",
            "user_discount_limit",
            "_above_limit",
            "_approver_discount_ceiling",
            "_compute_manual_discount_summary",
            "_exceeds_approved_percent",
            "_get_override_module",
            "_is_auto_pricing_rule_row",
            "_read_meta",
            "_resolve_reason",
            "_restore_doc_discount_state",
            "_set_row_field",
            "_snapshot_doc_discount_state",
            "_write_discount_audit_fields",
        ):
            setattr(disc_stub, attr, MagicMock())

        kot_stub = ModuleType("ury.ury.api.ury_kot_generate")
        kot_stub._execute_kot = MagicMock()
        kot_stub.process_items_for_cancel_kot = MagicMock()

        res_stub = ModuleType("ury.ury.api.ury_order_reservation_service")
        for attr in (
            "_line_key",
            "_line_ref",
            "reconcile_order_reservations",
            "release_order_reservations",
            "resolve_production_context",
            "_warehouse_for_context",
        ):
            setattr(res_stub, attr, MagicMock())

        sp_stub = ModuleType("ury.ury.api.ury_stock_policy")
        sp_stub.get_branch_stock_policy = MagicMock()

        alert_stub = ModuleType("ury.ury.doctype.alert_settings.alert_settings")
        alert_stub.get_alert_rule = MagicMock()

        notif_stub = ModuleType("ury.ury.api.ury_kot_notification")
        notif_stub.create_system_notification = MagicMock()
        notif_stub.get_users_with_role = MagicMock()

        ip_stub = ModuleType("ury.ury.api.item_price")
        ip_stub.item_price_release_states = MagicMock()
        ip_stub.price_valid_on = MagicMock()

        tender_stub = ModuleType("ury.ury.hooks.ury_pos_tender")
        tender_stub.sync_charged_tip = lambda *a, **kw: None

        offline_tenders_stub = ModuleType("ury.ury_pos.offline_tenders")
        offline_tenders_stub.enforce_offline_tender_replay = lambda *a, **kw: None

        room_charge_stub = ModuleType("ury.ury_pos.room_charge")
        room_charge_stub.enforce_room_charge_settlement = lambda *a, **kw: None

        ury_pos_pkg = ModuleType("ury.ury_pos")
        ury_pos_pkg.offline_tenders = offline_tenders_stub
        ury_pos_pkg.room_charge = room_charge_stub

        brand_mod = ModuleType("ury.brand")
        brand_mod.coded_message = lambda code, msg: f"[POS-{code}] {msg}"

        modules = {
            "frappe": self.frappe,
            "frappe.utils": frappe_utils,
            "frappe.model": frappe_model,
            "frappe.model.document": frappe_model_doc,
            "erpnext": erp_pkg,
            "erpnext.controllers": erp_ctrl,
            "erpnext.controllers.queries": erp_queries,
            "ury.brand": brand_mod,
            "ury.ury_pos": ury_pos_pkg,
            "ury.ury_pos.api": api_stub,
            "ury.ury_pos.default_customer": dc_stub,
            "ury.ury_pos.void_return_reopen": void_stub,
            "ury.ury_pos.discount": disc_stub,
            "ury.ury_pos.offline_tenders": offline_tenders_stub,
            "ury.ury_pos.room_charge": room_charge_stub,
            "ury.ury.api.ury_kot_generate": kot_stub,
            "ury.ury.api.ury_order_reservation_service": res_stub,
            "ury.ury.api.ury_stock_policy": sp_stub,
            "ury.ury.doctype.alert_settings.alert_settings": alert_stub,
            "ury.ury.api.ury_kot_notification": notif_stub,
            "ury.ury.api.item_price": ip_stub,
            "ury.ury.hooks.ury_pos_tender": tender_stub,
        }
        self.enterContext(patch.dict(sys.modules, modules))
        self.fx = _load_module("ury.ury_pos.fx", FX_PY)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.fx": self.fx}))
        self.order = _load_module("ury.ury.doctype.ury_order.ury_order", URY_ORDER_PY)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.doctype.ury_order.ury_order": self.order}))

    def _run_merged_settle(self, primary_total: float, partner_total: float, payments: list[dict]):
        def _validate_fx(doc):
            self.fx.validate_invoice_fx_payments(doc, pos_profile=self.profile)

        invoice = _FakeInvoice(
            {
                "name": "KR2B00052",
                "docstatus": 0,
                "pos_profile": "KH154 POS",
                "company": "Grand Hotel",
                "currency": "UGX",
                "posting_date": "2026-11-24",
                "grand_total": primary_total,
                "rounded_total": primary_total,
                "account_for_change_amount": "Cash - GH",
                "custom_merged_pos_invoice": "KR2B00053",
                "waiter": "waiter@hotel.test",
                "is_return": 0,
                "payments": [],
            },
            validate_fn=_validate_fx,
        )
        target = _FakeInvoice(
            {
                "name": "KR2B00053",
                "docstatus": 0,
                "pos_profile": "KH154 POS",
                "company": "Grand Hotel",
                "currency": "UGX",
                "posting_date": "2026-11-24",
                "grand_total": partner_total,
                "rounded_total": partner_total,
                "account_for_change_amount": "Cash - GH",
                "waiter": "waiter@hotel.test",
                "is_return": 0,
                "payments": [],
            },
            validate_fn=_validate_fx,
        )
        self.frappe.get_doc.side_effect = lambda dt, name, **kw: target if name == "KR2B00053" else invoice

        with (
            patch.object(self.order, "_validate_client_ids", return_value=None),
            patch.object(self.order, "_lock_settlement_customer", return_value=0),
            patch.object(self.order, "_client_order_invoice", return_value=None),
            patch.object(self.order, "get_order_invoice", return_value=invoice),
            patch.object(self.order, "_check_held_before_settlement", return_value=None),
            patch.object(self.order, "_refuse_client_order_rebind", return_value=None),
            patch.object(self.order, "_set_settlement_customer", return_value=None),
            patch.object(self.order, "_sync_service_charge", return_value=None),
            patch.object(self.order, "_validate_confirmed_phone_payments", return_value=None),
            patch.object(self.order, "_enforce_on_account", return_value=False),
        ):
            self.order.make_invoice(
                customer="Walk-in Guest",
                payments=payments,
                cashier="cashier@hotel.test",
                pos_profile="KH154 POS",
                owner="cashier@hotel.test",
                invoice="KR2B00052",
                submit_invoice=0,
            )

        # Also validate both docs after in_bill_merge_sync clears (mirrors real before_submit!)
        self.fx.validate_invoice_fx_payments(invoice, pos_profile=self.profile)
        self.fx.validate_invoice_fx_payments(target, pos_profile=self.profile)
        return invoice, target

    def test_merged_bill_fx_overtender_existing_branch_keeps_fx_math_and_local_change(self):
        # Hydrogen R2 Probe #12 exact repro: 100,000 + 150,000 UGX paid 100.00 USD Cash @ 3700
        # -> MUST pass [POS-FX-AMOUNT-MISMATCH], sum to 100.00 USD / 370,000 UGX, and give 120,000 UGX change!
        invoice, target = self._run_merged_settle(
            100000,
            150000,
            [{"mode_of_payment": "USD Cash", "custom_tender_amount": 100.0, "amount": 370000}],
        )

        usd_rows = [
            r
            for r in (invoice.payments + target.payments)
            if r.get("mode_of_payment") == "USD Cash"
        ]
        self.assertEqual(len(usd_rows), 2)
        self.assertAlmostEqual(sum(float(r.custom_tender_amount) for r in usd_rows), 100.0, places=4)
        self.assertAlmostEqual(sum(float(r.amount) for r in usd_rows), 370000.0, places=2)
        self.assertEqual(invoice.change_amount, 120000.0)
        self.assertEqual(target.change_amount, 0.0)
        # Local 0-amount Cash row guaranteed on primary for local change deduction
        self.assertTrue(any(r.mode_of_payment == "Cash" and float(r.amount) == 0.0 for r in invoice.payments))

    def test_merged_bill_fx_overtender_else_branch_preserves_fx_fields_and_math(self):
        # Primary 100,000 paid fully by local Cash (doc_req becomes 0 before USD Cash row),
        # partner 150,000 paid by 50.00 USD Cash @ 3700 = 185,000 UGX -> leftover 35,000 UGX
        # lands in the `else:` branch on primary invoice!
        invoice, target = self._run_merged_settle(
            100000,
            150000,
            [
                {"mode_of_payment": "Cash", "amount": 100000},
                {"mode_of_payment": "USD Cash", "custom_tender_amount": 50.0, "amount": 185000},
            ],
        )

        primary_usd = [r for r in invoice.payments if r.get("mode_of_payment") == "USD Cash"]
        partner_usd = [r for r in target.payments if r.get("mode_of_payment") == "USD Cash"]
        self.assertEqual(len(primary_usd), 1)
        self.assertEqual(len(partner_usd), 1)
        self.assertEqual(primary_usd[0].get("custom_tender_currency"), "USD")
        self.assertEqual(float(primary_usd[0].get("custom_exchange_rate")), 3700.0)
        self.assertGreater(float(primary_usd[0].get("custom_tender_amount") or 0), 0.0)
        self.assertAlmostEqual(
            float(primary_usd[0].custom_tender_amount) + float(partner_usd[0].custom_tender_amount),
            50.0,
            places=4,
        )
        self.assertEqual(invoice.change_amount, 35000.0)
        self.assertEqual(target.change_amount, 0.0)

    def test_merged_bill_high_rate_kwd_3dp_overtender_passes_validation(self):
        # High rate 12,345 UGX/KWD where 4dp quantization step = 1.2345 UGX (> 0.5 UGX base tol):
        # 100,000 + 150,006 UGX paid 25.000 KWD Cash @ 12345 = 308,625 UGX -> 58,619 UGX change.
        invoice, target = self._run_merged_settle(
            100000,
            150006,
            [{"mode_of_payment": "KWD Cash", "custom_tender_amount": 25.0, "amount": 308625}],
        )

        kwd_rows = [
            r
            for r in (invoice.payments + target.payments)
            if r.get("mode_of_payment") == "KWD Cash"
        ]
        self.assertEqual(len(kwd_rows), 2)
        self.assertAlmostEqual(sum(float(r.custom_tender_amount) for r in kwd_rows), 25.0, places=4)
        self.assertAlmostEqual(sum(float(r.amount) for r in kwd_rows), 308625.0, places=2)
        self.assertEqual(invoice.change_amount, 58619.0)


if __name__ == "__main__":
    unittest.main()
