"""Till close controls Slice 2: Over/Short tolerance, blind close, close another till,
reopen till, force close, double-close/concurrent-close/offline-after-close guards;
site-less (in-memory Frappe double, no bench required).

Matches Oracle Simphony (Opt 205/206/267 Over/Short Count, Opt 212 Close Till,
Opt 213 Reopen Till, Opt 25 View Blind Totals, Force Close / Opt 30058-30059)
and SAP POS Store Manager / Customer Checkout parity.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from ury.ury_pos.test_till_cash_controls import (
    APP,
    CASH_MOVEMENT_HOOK,
    DRAWER_EVENT_DIR,
    TILL_CONTROLS_PY,
    FakeDoc,
    FakeOverride,
    PermissionDenied,
    Refused,
    Row,
    _build_env,
    _match_filters,
)

CLOSING_HOOK = APP / "ury" / "hooks" / "ury_pos_closing_entry.py"


class ClosingStore:
    """Extends MemoryStore env with POS Closing Entry, consolidation, and lock tracking."""

    def __init__(self, store, frappe_mod):
        self.store = store
        self.frappe = frappe_mod
        self.closings: dict[str, FakeDoc] = {}
        self.sales_invoices: dict[str, FakeDoc] = {}
        self.lock_calls: list[tuple[str, str]] = []
        self.consolidations: list[dict] = []
        self.closing_hook_mod = None
        self.install_hooks()

    def install_hooks(self):
        orig_get_value = self.frappe.db.get_value.side_effect
        orig_get_all = self.frappe.get_all.side_effect
        orig_get_doc = self.frappe.get_doc.side_effect
        orig_exists = self.frappe.db.exists.side_effect
        orig_new_doc = self.frappe.new_doc.side_effect

        def _all_for(doctype):
            if doctype == "POS Closing Entry":
                return list(self.closings.values())
            if doctype == "Sales Invoice":
                return list(self.sales_invoices.values())
            return None

        def get_value(doctype, filters=None, fieldname=None, as_dict=False, for_update=False, **kwargs):
            if for_update:
                self.lock_calls.append((doctype, str(filters)))
            if doctype == "POS Closing Entry":
                docs = list(self.closings.values())
                match = next((d for d in docs if _match_filters(d, filters)), None)
                if not match:
                    return None
                if isinstance(fieldname, (list, tuple)):
                    data = {f: match.get(f) for f in fieldname}
                    return Row(data) if as_dict else tuple(data[f] for f in fieldname)
                return match.get(fieldname)
            if doctype == "Sales Invoice":
                docs = list(self.sales_invoices.values())
                match = next((d for d in docs if _match_filters(d, filters)), None)
                if not match:
                    return None
                if isinstance(fieldname, (list, tuple)):
                    data = {f: match.get(f) for f in fieldname}
                    return Row(data) if as_dict else tuple(data[f] for f in fieldname)
                return match.get(fieldname)
            return orig_get_value(doctype, filters, fieldname, as_dict=as_dict, for_update=for_update, **kwargs)

        def get_all(doctype, filters=None, fields=None, pluck=None, order_by=None, limit_page_length=None, **kwargs):
            extra = _all_for(doctype)
            if extra is not None:
                docs = [d for d in extra if _match_filters(d, filters)]
                if pluck:
                    return [d.get(pluck) for d in docs]
                if fields:
                    return [Row({f: d.get(f) for f in fields}) for d in docs]
                return docs
            return orig_get_all(doctype, filters, fields=fields, pluck=pluck, order_by=order_by, limit_page_length=limit_page_length, **kwargs)

        def exists(doctype, filters=None):
            extra = _all_for(doctype)
            if extra is not None:
                if isinstance(filters, str):
                    return any(d.get("name") == filters for d in extra)
                return any(_match_filters(d, filters) for d in extra)
            return orig_exists(doctype, filters)

        def make_closing_doc(data):
            doc = ClosingFakeDoc(data, store=self.store, closing_store=self)
            return doc

        def get_doc(arg1, arg2=None, for_update=False, **kwargs):
            if for_update:
                self.lock_calls.append((str(arg1), str(arg2)))
            if isinstance(arg1, dict) and arg1.get("doctype") == "POS Closing Entry":
                return make_closing_doc(arg1)
            if arg1 == "POS Closing Entry":
                if arg2 not in self.closings:
                    raise KeyError(f"POS Closing Entry {arg2} not found")
                return self.closings[arg2]
            if arg1 == "Sales Invoice":
                if arg2 not in self.sales_invoices:
                    raise KeyError(f"Sales Invoice {arg2} not found")
                return self.sales_invoices[arg2]
            return orig_get_doc(arg1, arg2, **kwargs)

        def new_doc(doctype):
            if doctype == "POS Closing Entry":
                return make_closing_doc({"doctype": "POS Closing Entry"})
            return orig_new_doc(doctype)

        def delete_doc(doctype, name, ignore_permissions=False, **kwargs):
            if doctype == "Journal Entry":
                self.store.journal_entries.pop(name, None)

        self.frappe.db.get_value.side_effect = get_value
        self.frappe.get_all.side_effect = get_all
        self.frappe.get_list.side_effect = get_all
        self.frappe.db.exists.side_effect = exists
        self.frappe.get_doc.side_effect = get_doc
        self.frappe.new_doc.side_effect = new_doc
        self.frappe.delete_doc.side_effect = delete_doc
        self.frappe.db.get_single_value.side_effect = lambda dt, f: (
            "POS Invoice" if (dt, f) == ("POS Settings", "invoice_type") else None
        )


class ClosingFakeDoc(FakeDoc):
    def __init__(self, data=None, store=None, closing_store=None):
        super().__init__(data, store=store)
        self._closing_store = closing_store
        self.setdefault("pos_invoices", [])
        self.setdefault("payment_reconciliation", [])
        self.setdefault("taxes", [])

    def set(self, field, value):
        self[field] = [Row(r) for r in (value or [])]

    def reload(self):
        return self

    def insert(self, ignore_permissions=False):
        if not self.get("name"):
            seq = self._store.next_seq("POS-CLO")
            self["name"] = f"POS-CLO-{seq:04d}"
        self.setdefault("docstatus", 0)
        self.setdefault("status", "Draft")
        self.setdefault("owner", self._store.current_user)
        hook = self._closing_store.closing_hook_mod
        if hook:
            hook.before_validate(self, "before_validate")
            hook.validate(self, "validate")
            hook.before_save(self, "before_save")
        self._is_new = False
        self._closing_store.closings[self.name] = self
        return self

    def submit(self):
        hook = self._closing_store.closing_hook_mod
        if hook:
            hook.before_validate(self, "before_validate")
            hook.validate(self, "validate")
            hook.before_submit(self, "before_submit")
        self["docstatus"] = 1
        self["status"] = "Submitted"
        # Native ERPNext on_submit: mark opening closed + consolidate once + post over/short
        opening = self._store.openings.get(self.pos_opening_entry)
        if opening:
            opening["status"] = "Closed"
            opening["pos_closing_entry"] = self.name
        si_name = f"SINV-CONS-{self._store.next_seq('SINV'):04d}"
        consolidated_names = []
        for row in self.get("pos_invoices") or []:
            inv = self._store.pos_invoices.get(row.pos_invoice)
            if inv and not inv.get("consolidated_invoice"):
                inv["consolidated_invoice"] = si_name
                consolidated_names.append(inv.name)
        if consolidated_names:
            self._closing_store.sales_invoices[si_name] = FakeDoc(
                {
                    "doctype": "Sales Invoice",
                    "name": si_name,
                    "docstatus": 1,
                    "is_consolidated": 1,
                    "pos_closing_entry": self.name,
                },
                store=self._store,
            )
            self._closing_store.consolidations.append(
                {"closing": self.name, "sales_invoice": si_name, "pos_invoices": consolidated_names}
            )
        if self._store.je_hook_mod:
            self._store.je_hook_mod.post_cash_over_short(self, "on_submit")
        self._closing_store.closings[self.name] = self
        return self

    def cancel(self):
        hook = self._closing_store.closing_hook_mod
        if hook and hasattr(hook, "before_cancel"):
            hook.before_cancel(self, "before_cancel")
        self["docstatus"] = 2
        self["status"] = "Cancelled"
        # Native ERPNext on_cancel: unconsolidate + reopen POS Opening Entry + cancel Over/Short JE
        for cons in list(self._closing_store.consolidations):
            if cons["closing"] == self.name:
                si = self._closing_store.sales_invoices.get(cons["sales_invoice"])
                if si:
                    si["docstatus"] = 2
                for inv_name in cons["pos_invoices"]:
                    inv = self._store.pos_invoices.get(inv_name)
                    if inv:
                        inv["consolidated_invoice"] = None
        opening = self._store.openings.get(self.pos_opening_entry)
        if opening:
            opening["status"] = "Open"
            opening["pos_closing_entry"] = None
        if self._store.je_hook_mod:
            self._store.je_hook_mod.cancel_cash_over_short(self, "on_cancel")
        self._closing_store.closings[self.name] = self
        return self


class LoadedSlice2(unittest.TestCase):
    def setUp(self):
        self.store, self.override, self.frappe, brand_spec, brand_mod, modules = _build_env()

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

        # Extend api stub with helpers used by ury_pos_closing_entry
        api_stub = modules["ury.ury_pos.api"]
        api_stub._goal_submitted_since = lambda *a, **kw: True
        api_stub._phase_dependent_rows = lambda *a, **kw: []
        api_stub._role_rank = lambda *a, **kw: None

        def _till_invoice_rows(pos_profile, start, end, reader):
            return [
                d
                for d in self.store.pos_invoices.values()
                if d.get("pos_profile") == pos_profile
                and d.get("docstatus") == 1
                and not d.get("consolidated_invoice")
            ]

        api_stub._till_invoice_rows = _till_invoice_rows

        self.enterContext(patch.dict(sys.modules, modules))
        brand_spec.loader.exec_module(brand_mod)

        # Load POS Drawer Event controller
        ctrl_spec = importlib.util.spec_from_file_location(
            "ury.ury.doctype.pos_drawer_event.pos_drawer_event", DRAWER_EVENT_DIR / "pos_drawer_event.py"
        )
        ctrl_mod = importlib.util.module_from_spec(ctrl_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.doctype.pos_drawer_event.pos_drawer_event": ctrl_mod}))
        ctrl_spec.loader.exec_module(ctrl_mod)
        self.store.drawer_event_cls = ctrl_mod.POSDrawerEvent

        # Load ury_cash_movement
        cm_spec = importlib.util.spec_from_file_location("ury.ury.hooks.ury_cash_movement", CASH_MOVEMENT_HOOK)
        self.cash_hook = importlib.util.module_from_spec(cm_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_cash_movement": self.cash_hook}))
        cm_spec.loader.exec_module(self.cash_hook)
        self.store.je_hook_mod = self.cash_hook

        def fake_movements(opening_entry, currency=None):
            totals = {}
            for je in self.store.journal_entries.values():
                if je.get("custom_pos_opening_entry") != opening_entry or je.get("docstatus") != 1:
                    continue
                for line in je.get("accounts") or []:
                    acct = line.get("account")
                    acct_meta = self.store.accounts.get(acct)
                    if currency and acct_meta and acct_meta.get("account_currency") == currency:
                        delta = float(line.get("debit_in_account_currency") or 0) - float(
                            line.get("credit_in_account_currency") or 0
                        )
                    else:
                        delta = float(line.get("debit") or 0) - float(line.get("credit") or 0)
                    totals[acct] = totals.get(acct, 0.0) + delta
            return totals

        self.enterContext(patch.object(self.cash_hook, "till_cash_movements", side_effect=fake_movements))

        # Load till_controls
        tc_spec = importlib.util.spec_from_file_location("ury.ury_pos.till_controls", TILL_CONTROLS_PY)
        self.tc = importlib.util.module_from_spec(tc_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.till_controls": self.tc}))
        tc_spec.loader.exec_module(self.tc)

        # Load ury_pos_closing_entry
        pce_hook_spec = importlib.util.spec_from_file_location("ury.ury.hooks.ury_pos_closing_entry", CLOSING_HOOK)
        self.closing_hook = importlib.util.module_from_spec(pce_hook_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_pos_closing_entry": self.closing_hook}))
        pce_hook_spec.loader.exec_module(self.closing_hook)

        self.cs = ClosingStore(self.store, self.frappe)
        self.cs.closing_hook_mod = self.closing_hook

        # Add a submitted cash bill of 50,000 UGX on Main Till (expected cash = 100,000 float + 50,000 = 150,000)
        self.store.pos_invoices["POS-INV-1"] = FakeDoc(
            {
                "doctype": "POS Invoice",
                "name": "POS-INV-1",
                "docstatus": 1,
                "pos_profile": "Main Till",
                "customer": "Walk-in",
                "consolidated_invoice": None,
                "posting_date": "2026-11-24",
                "posting_time": "09:15:00",
                "grand_total": 50000,
                "net_total": 50000,
                "rounded_total": 50000,
                "total_qty": 2,
                "total_taxes_and_charges": 0,
                "change_amount": 0,
                "account_for_change_amount": "Cash - HC",
                "is_return": 0,
                "return_against": None,
                "payments": [Row(mode_of_payment="Cash", amount=50000, account="Cash - HC")],
            },
            store=self.store,
        )


class TestOverShortToleranceAndBlindClose(LoadedSlice2):
    """Matrix rows 26 & 29: Over/Short tolerance, Manager PIN + reason, Blind Close."""

    def test_variance_within_tolerance_closes_without_pin_and_posts_over_short_je(self):
        # Main Till tolerance = 5,000; expected cash = 150,000; counted = 147,000 (diff = -3,000)
        self.store.current_user = "cashier@example.com"
        res = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 147000}],
        )
        closing = self.cs.closings[res["name"]]
        self.assertEqual(closing.docstatus, 1)
        self.assertEqual(closing.payment_reconciliation[0].difference, -3000)
        self.assertEqual(self.store.openings["OPEN-MAIN-1"].status, "Closed")
        # Over/Short JE posted natively
        over_short = [je for je in self.store.journal_entries.values() if je.get("custom_pos_closing_entry") == closing.name]
        self.assertEqual(len(over_short), 1)

    def test_over_short_approval_prices_persisted_opening_and_payments_without_a_write(self):
        profile = self.frappe.get_doc("POS Profile", "Main Till")
        counts = [{"mode_of_payment": "Cash", "closing_amount": 135000}]
        amount = self.tc.over_short_approval_amount(
            profile, "OPEN-MAIN-1", counts, period_end_date="2026-11-24 10:00:00",
        )
        self.assertEqual(amount, 15000)
        self.assertFalse(self.cs.closings)
        self.assertEqual(self.store.openings["OPEN-MAIN-1"]["status"], "Open")

    def test_variance_above_tolerance_requires_manager_pin_and_reason_and_stamps_audit(self):
        # Expected = 150,000; counted = 135,000 (diff = -15,000 > tolerance 5,000)
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(PermissionDenied, r"POS-CLOSE-VARIANCE-APPROVAL"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 135000}],
            )

        # Even with a PIN token, missing over_short_reason refuses
        claim = {
            "action": "over_short_close",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "counted_payments": [{"mode_of_payment": "Cash", "closing_amount": 135000}],
            "reason": "Wrong change given during lunch rush",
        }
        self.override.mint(
            "tok-os-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="over_short_close",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        with self.assertRaisesRegex(Refused, r"POS-CLOSE-OVER-SHORT-REASON"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 135000}],
                over_short_reason="   ",
                over_short_approval_token="tok-os-1",
            )

        # With Manager PIN + reason: succeeds, keeps cashier session, stamps approver + no_sale_count
        self.tc.trigger_no_sale.__wrapped__ if hasattr(self.tc.trigger_no_sale, "__wrapped__") else None
        self.store.current_user = "manager@example.com"
        self.tc.trigger_no_sale(pos_profile="Main Till", reason="CHANGE", pos_opening_entry="OPEN-MAIN-1")
        self.store.current_user = "cashier@example.com"

        res = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 135000}],
            over_short_reason="Wrong change given during lunch rush",
            over_short_approval_token="tok-os-1",
        )
        self.assertEqual(self.store.current_user, "cashier@example.com")
        closing = self.cs.closings[res["name"]]
        self.assertEqual(closing.docstatus, 1)
        self.assertEqual(closing.custom_over_short_approver, "manager@example.com")
        self.assertEqual(closing.custom_over_short_reason, "Wrong change given during lunch rush")
        self.assertEqual(closing.custom_no_sale_count, 1)

    def test_adversarial_tolerance_zero_and_unset_tolerance_refuse_any_variance(self):
        # Pool Till has custom_over_short_tolerance = 0; float = 50,000, no sales -> expected = 50,000
        self.store.current_user = "cashier2@example.com"
        with self.assertRaisesRegex(PermissionDenied, r"POS-CLOSE-VARIANCE-APPROVAL"):
            self.tc.close_till_with_controls(
                pos_profile="Pool Till",
                pos_opening_entry="OPEN-POOL-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 49999}],
            )
        # Unset (None) tolerance also fails closed as 0
        self.store.profiles["Pool Till"]["custom_over_short_tolerance"] = None
        with self.assertRaisesRegex(PermissionDenied, r"POS-CLOSE-VARIANCE-APPROVAL"):
            self.tc.close_till_with_controls(
                pos_profile="Pool Till",
                pos_opening_entry="OPEN-POOL-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 50001}],
            )
        # Exact count (diff == 0) closes cleanly without PIN
        ok = self.tc.close_till_with_controls(
            pos_profile="Pool Till",
            pos_opening_entry="OPEN-POOL-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 50000}],
        )
        self.assertEqual(self.cs.closings[ok["name"]].docstatus, 1)

    def test_close_period_end_date_preserves_subsecond_microseconds(self):
        # ERPNext v16 POS Invoice.posting_time carries microseconds; truncating
        # period_end_date to %Y-%m-%d %H:%M:%S drops same-second invoices.
        subsec_now = datetime(2026, 11, 24, 18, 0, 5, 654321)
        self.store.current_user = "cashier2@example.com"
        with patch.object(self.tc, "now_datetime", return_value=subsec_now):
            ok = self.tc.close_till_with_controls(
                pos_profile="Pool Till",
                pos_opening_entry="OPEN-POOL-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 50000}],
            )
        closing = self.cs.closings[ok["name"]]
        self.assertGreaterEqual(
            self.tc.get_datetime(closing.period_end_date),
            subsec_now,
            f"period_end_date {closing.period_end_date!r} truncated microseconds before {subsec_now!r}",
        )

    def test_adversarial_blind_close_hides_expected_cash_even_on_variance_refusal(self):
        self.store.profiles["Main Till"]["custom_blind_cash_count"] = 1
        self.store.current_user = "cashier@example.com"

        preview = self.tc.get_till_close_preview("Main Till", "OPEN-MAIN-1")
        self.assertTrue(preview["blind_close"])
        self.assertIsNone(preview["grand_total"])
        for row in preview["payment_rows"]:
            self.assertIsNone(row["expected_amount"])

        # Counted 120,000 vs hidden expected 150,000 -> refusal MUST NOT leak 150000 or 30000!
        with self.assertRaises(PermissionDenied) as ctx:
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 120000}],
            )
        msg = str(ctx.exception)
        self.assertIn("POS-CLOSE-VARIANCE-APPROVAL", msg)
        self.assertNotIn("150000", msg)
        self.assertNotIn("150,000", msg)
        self.assertNotIn("30000", msg)
        self.assertNotIn("30,000", msg)

    def test_adversarial_cashier_cannot_approve_own_variance_and_negative_count_refused(self):
        self.store.current_user = "cashier@example.com"
        claim = {
            "action": "over_short_close",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "counted_payments": [{"mode_of_payment": "Cash", "closing_amount": 100000}],
            "reason": "Self approve attempt",
        }
        self.override.mint(
            "tok-self-var",
            requester="cashier@example.com",
            approver="cashier@example.com",
            privilege="over_short_close",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-SELF"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 100000}],
                over_short_reason="Self approve attempt",
                over_short_approval_token="tok-self-var",
            )

        with self.assertRaisesRegex(Refused, r"POS-CLOSE-NEGATIVE-AMOUNT"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": -500}],
            )

        # Direct Desk/REST submit without server flag is also blocked by before_submit gate
        direct = self.frappe.new_doc("POS Closing Entry")
        direct.pos_opening_entry = "OPEN-MAIN-1"
        direct.pos_profile = "Main Till"
        direct.company = "Hotel Co"
        direct.user = "cashier@example.com"
        direct.period_start_date = "2026-11-24 08:00:00"
        direct.period_end_date = "2026-11-24 10:30:00"
        direct.posting_date = "2026-11-24"
        direct.append("payment_reconciliation", {"mode_of_payment": "Cash", "opening_amount": 100000, "closing_amount": 100000})
        direct.insert()
        with self.assertRaisesRegex(PermissionDenied, r"POS-CLOSE-VARIANCE-APPROVAL"):
            direct.submit()


class TestCloseOtherReopenForceAndConcurrency(LoadedSlice2):
    """Matrix rows 27 & 28 + adversarial double-close, concurrent settle & offline-after-close."""

    def test_close_another_persons_till_requires_pin_keeps_holder_user_and_consolidates_once(self):
        # cashier2 attempts to close cashier's OPEN-MAIN-1
        self.store.current_user = "cashier2@example.com"
        self.store.profiles["Main Till"]["applicable_for_users"].append(Row(user="cashier2@example.com"))

        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
            )

        claim = {
            "action": "close_other_till",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "opening_user": "cashier@example.com",
        }
        self.override.mint(
            "tok-other-1",
            requester="cashier2@example.com",
            approver="manager@example.com",
            privilege="close_other_till",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        res = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
            close_other_approval_token="tok-other-1",
        )
        closing = self.cs.closings[res["name"]]
        self.assertEqual(closing.docstatus, 1)
        self.assertEqual(closing.user, "cashier@example.com")  # Native ERPNext rule preserved!
        self.assertEqual(closing.custom_close_approver, "manager@example.com")
        self.assertEqual(len(self.cs.consolidations), 1)
        self.assertEqual(self.cs.consolidations[0]["pos_invoices"], ["POS-INV-1"])

    def test_reopen_till_requires_pin_unconsolidates_and_reclose_never_duplicates_consolidation(self):
        # First close OPEN-MAIN-1 cleanly
        self.store.current_user = "cashier@example.com"
        first = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
        )
        first_closing = self.cs.closings[first["name"]]
        self.assertEqual(len(self.cs.consolidations), 1)
        self.assertIsNotNone(self.store.pos_invoices["POS-INV-1"].consolidated_invoice)

        # Cashier cannot reopen without PIN
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.reopen_till(
                pos_closing_entry=first_closing.name,
                reason="Late guest invoice adjustment",
            )

        # Direct Desk cancel without PIN also blocked by before_cancel gate
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            first_closing.cancel()

        # Reopen with Manager PIN + reason
        claim = {
            "action": "reopen_till",
            "pos_profile": "Main Till",
            "pos_closing_entry": first_closing.name,
            "pos_opening_entry": "OPEN-MAIN-1",
            "reason": "Late guest invoice adjustment",
        }
        self.override.mint(
            "tok-reopen-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="reopen_till",
            pos_profile="Main Till",
            reference_name=first_closing.name,
            claim=claim,
        )
        reopened = self.tc.reopen_till(
            pos_closing_entry=first_closing.name,
            reason="Late guest invoice adjustment",
            approval_token="tok-reopen-1",
        )
        self.assertEqual(first_closing.docstatus, 2)
        self.assertEqual(self.store.openings["OPEN-MAIN-1"].status, "Open")
        self.assertIsNone(self.store.pos_invoices["POS-INV-1"].consolidated_invoice)
        self.assertEqual(reopened["approver"], "manager@example.com")
        reopen_events = [e for e in self.store.drawer_events.values() if e.event_type == "Till Reopen"]
        self.assertEqual(len(reopen_events), 1)

        # Re-close: POS-INV-1 consolidates cleanly onto a single active Sales Invoice (old one cancelled)
        second = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
        )
        self.assertNotEqual(second["name"], first["name"])
        active_sis = [si for si in self.cs.sales_invoices.values() if si.docstatus == 1]
        self.assertEqual(len(active_sis), 1)
        self.assertEqual(self.store.pos_invoices["POS-INV-1"].consolidated_invoice, active_sis[0].name)

    def test_reopen_till_skips_missing_efris_columns_on_non_uganda_sites(self):
        self.store.current_user = "cashier@example.com"
        first = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
        )
        closing_name = first["name"]
        orig_get_all = self.frappe.get_all

        def strict_get_all(doctype, *args, **kwargs):
            fields = kwargs.get("fields") or []
            if doctype == "Sales Invoice" and any(f in ("custom_efris_fdn", "custom_efris_irn") for f in fields):
                raise RuntimeError("Unknown column 'custom_efris_fdn' in 'SELECT'")
            return orig_get_all(doctype, *args, **kwargs)

        claim = {
            "action": "reopen_till",
            "pos_profile": "Main Till",
            "pos_closing_entry": closing_name,
            "pos_opening_entry": "OPEN-MAIN-1",
            "reason": "Non-EFRIS site reopen",
        }
        self.override.mint(
            "tok-reopen-nonefris",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="reopen_till",
            pos_profile="Main Till",
            reference_name=closing_name,
            claim=claim,
        )
        with (
            patch.object(self.frappe.db, "has_column", return_value=False, create=True),
            patch.object(self.frappe, "get_all", side_effect=strict_get_all),
        ):
            res = self.tc.reopen_till(
                pos_closing_entry=closing_name,
                reason="Non-EFRIS site reopen",
                approval_token="tok-reopen-nonefris",
            )
        self.assertEqual(res["status"], "Open")

    def test_reopen_till_elevates_permissions_during_unconsolidation_and_restores_cashier_session(self):
        # ERPNext unconsolidate_pos_invoices() cancels the linked Sales Invoice without
        # setting si.flags.ignore_permissions, so reopen_till runs closing_doc.cancel()
        # as Administrator after PIN verification and restores frappe.session.user immediately.
        self.store.current_user = "cashier@example.com"
        first = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
        )
        closing_name = first["name"]
        closing_doc = self.cs.closings[closing_name]
        orig_cancel = type(closing_doc).cancel
        seen_users = []

        def checked_cancel(doc_self):
            seen_users.append(self.frappe.session.user)
            return orig_cancel(doc_self)

        claim = {
            "action": "reopen_till",
            "pos_profile": "Main Till",
            "pos_closing_entry": closing_name,
            "pos_opening_entry": "OPEN-MAIN-1",
            "reason": "Cashier session unconsolidation",
        }
        self.override.mint(
            "tok-reopen-perm",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="reopen_till",
            pos_profile="Main Till",
            reference_name=closing_name,
            claim=claim,
        )
        with patch.object(type(closing_doc), "cancel", checked_cancel):
            self.tc.reopen_till(
                pos_closing_entry=closing_name,
                reason="Cashier session unconsolidation",
                approval_token="tok-reopen-perm",
            )
        self.assertEqual(seen_users, ["Administrator"], "cancel() must run elevated for ERPNext unconsolidation")
        self.assertEqual(self.store.current_user, "cashier@example.com", "cashier session must be restored")

    def test_force_close_requires_pin_snapshots_open_checks_and_preserves_consolidation(self):
        # Add an open draft check on Main Till
        self.store.pos_invoices["POS-DRAFT-1"] = FakeDoc(
            {
                "doctype": "POS Invoice",
                "name": "POS-DRAFT-1",
                "docstatus": 0,
                "pos_profile": "Main Till",
                "customer": "Table 4",
                "restaurant_table": "T-4",
                "grand_total": 25000,
            },
            store=self.store,
        )
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.force_close_till(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
                reason="Night audit cutoff",
            )

        claim = {
            "action": "force_close",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "reason": "Night audit cutoff",
            "discard_pending_movements": False,
        }
        self.override.mint(
            "tok-fc-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="reopen_till",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        res = self.tc.force_close_till(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
            reason="Night audit cutoff",
            approval_token="tok-fc-1",
        )
        closing = self.cs.closings[res["name"]]
        self.assertEqual(closing.docstatus, 1)
        self.assertEqual(closing.custom_force_closed, 1)
        self.assertEqual(closing.custom_force_close_reason, "Night audit cutoff")
        self.assertEqual(closing.custom_close_approver, "manager@example.com")
        # Submitted invoice POS-INV-1 was STILL consolidated (never lost!)
        self.assertEqual(len(self.cs.consolidations), 1)
        self.assertEqual(self.cs.consolidations[0]["pos_invoices"], ["POS-INV-1"])
        fc_events = [e for e in self.store.drawer_events.values() if e.event_type == "Force Close"]
        self.assertEqual(len(fc_events), 1)
        self.assertIn("POS-DRAFT-1", fc_events[0].open_checks_snapshot)

    def test_adversarial_closing_twice_refuses_and_locks_opening_entry(self):
        self.store.current_user = "cashier@example.com"
        self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
        )
        # Lock on POS Opening Entry was acquired FOR UPDATE
        self.assertTrue(
            any(dt == "POS Opening Entry" and "OPEN-MAIN-1" in ref for dt, ref in self.cs.lock_calls),
            f"Expected FOR UPDATE lock on POS Opening Entry, got {self.cs.lock_calls}",
        )
        # Second close attempt MUST refuse with POS-CLOSE-ALREADY-CLOSED
        with self.assertRaisesRegex(Refused, r"POS-CLOSE-ALREADY-CLOSED"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
            )
        self.assertEqual(len(self.cs.consolidations), 1)

    def test_adversarial_concurrent_close_and_settle_acquires_opening_lock_and_offline_after_close_refused(self):
        # Settlement guard acquires FOR UPDATE lock on the open POS Opening Entry to serialize against close
        self.cs.lock_calls.clear()
        self.tc.lock_open_shift_for_settlement("Main Till")
        self.assertTrue(
            any(dt == "POS Opening Entry" and "OPEN-MAIN-1" in ref for dt, ref in self.cs.lock_calls),
            "Settlement must acquire FOR UPDATE lock on active POS Opening Entry",
        )

        # Close the shift at 10:30:00
        self.store.current_user = "cashier@example.com"
        self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
        )

        # Offline settlement arriving after shift closed (no open shift) -> REFUSED!
        with self.assertRaisesRegex(Refused, r"POS-OFFLINE-AFTER-CLOSE"):
            self.tc.validate_offline_settlement_shift(
                pos_profile="Main Till",
                offline_captured_at="2026-11-24 09:45:00",
                client_order_id="offline-order-1",
            )

        # Even if a NEW shift opens at 11:00:00, an offline settlement captured at 09:45:00 (before the 10:30 close) MUST refuse!
        self.store.openings["OPEN-MAIN-2"] = FakeDoc(
            {
                "doctype": "POS Opening Entry",
                "name": "OPEN-MAIN-2",
                "pos_profile": "Main Till",
                "company": "Hotel Co",
                "branch": "Main Branch",
                "user": "cashier@example.com",
                "status": "Open",
                "docstatus": 1,
                "period_start_date": "2026-11-24 11:00:00",
                "balance_details": [Row(mode_of_payment="Cash", opening_amount=100000)],
            },
            store=self.store,
        )
        with self.assertRaisesRegex(Refused, r"POS-OFFLINE-AFTER-CLOSE"):
            self.tc.validate_offline_settlement_shift(
                pos_profile="Main Till",
                offline_captured_at="2026-11-24 09:45:00",
                client_order_id="offline-order-1",
            )

        # Offline settlement captured DURING the new shift (11:15:00) is allowed!
        self.tc.validate_offline_settlement_shift(
            pos_profile="Main Till",
            offline_captured_at="2026-11-24 11:15:00",
            client_order_id="offline-order-2",
        )

    def test_close_other_till_forwards_reason_in_claim_for_r2_reason_validation(self):
        # #147 R2 enforces _validate_reason on close_other_till: close_till_with_controls
        # must accept close_other_reason and bind it into the claim verified by override.consume.
        self.store.current_user = "cashier2@example.com"
        reason = "Other: Shift handover after cashier left ill"
        claim = {
            "action": "close_other_till",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "opening_user": "cashier@example.com",
            "reason": reason,
        }
        self.override.mint(
            "tok-other-reason-1",
            requester="cashier2@example.com",
            approver="manager@example.com",
            privilege="close_other_till",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        res = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
            close_other_approval_token="tok-other-reason-1",
            close_other_reason=reason,
        )
        self.assertEqual(res["docstatus"], 1)
        self.assertEqual(res["close_approver"], "manager@example.com")
        self.assertIn("tok-other-reason-1", self.override.consumed)

    def test_blind_close_other_till_preserves_close_other_token_on_variance_refusal(self):
        # Defect 1 (R1 live probe): when closing another cashier's blind till and counted cash
        # exceeds tolerance without over_short_approval_token, close_till_with_controls must
        # refuse POS-CLOSE-VARIANCE-APPROVAL WITHOUT burning close_other_approval_token so
        # retrying with both tokens succeeds (never POS-AUTH-EXPIRED).
        self.store.profiles["Main Till"]["custom_blind_cash_count"] = 1
        self.store.profiles["Main Till"]["custom_over_short_tolerance"] = 5000
        self.store.current_user = "cashier2@example.com"

        other_reason = "Other: Shift handover close"
        other_claim = {
            "action": "close_other_till",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "opening_user": "cashier@example.com",
            "reason": other_reason,
        }
        self.override.mint(
            "tok-other-preserve-1",
            requester="cashier2@example.com",
            approver="manager@example.com",
            privilege="close_other_till",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=other_claim,
        )

        # Step 1: variance is -30,000 (> 5,000 tolerance), no over_short_approval_token yet
        with self.assertRaisesRegex(PermissionDenied, r"POS-CLOSE-VARIANCE-APPROVAL") as ctx:
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 120000}],
                close_other_approval_token="tok-other-preserve-1",
                close_other_reason=other_reason,
            )
        # Blind zero-leak: expected 150,000 must not appear in refusal message
        self.assertNotIn("150000", str(ctx.exception))
        self.assertNotIn("150,000", str(ctx.exception))
        # CRITICAL: close_other_till token must STILL be unconsumed!
        self.assertNotIn("tok-other-preserve-1", self.override.consumed)

        # Step 2: retry with BOTH the preserved close_other token and the new over_short token
        os_reason = "Other: Confirmed blind recount short 30000"
        os_claim = {
            "action": "over_short_close",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "counted_payments": [{"mode_of_payment": "Cash", "closing_amount": 120000}],
            "reason": os_reason,
        }
        self.override.mint(
            "tok-os-preserve-2",
            requester="cashier2@example.com",
            approver="manager@example.com",
            privilege="over_short_close",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=os_claim,
        )
        res = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 120000}],
            over_short_reason=os_reason,
            over_short_approval_token="tok-os-preserve-2",
            close_other_approval_token="tok-other-preserve-1",
            close_other_reason=other_reason,
        )
        self.assertEqual(res["docstatus"], 1)
        self.assertEqual(res["close_approver"], "manager@example.com")
        self.assertEqual(res["over_short_approver"], "manager@example.com")
        self.assertIn("tok-other-preserve-1", self.override.consumed)
        self.assertIn("tok-os-preserve-2", self.override.consumed)

    def test_force_close_enforces_over_short_close_privilege_and_max_amount_cap(self):
        # Defect 3 (R1 review): force_close_till must NOT stamp closing.flags.over_short_approval
        # unchecked from the force-close PIN approver when variance exceeds tolerance.
        self.store.profiles["Main Till"]["custom_over_short_tolerance"] = 5000
        self.store.user_roles["admin_mgr@example.com"] = ["URY Admin"]
        self.store.user_branches["admin_mgr@example.com"] = "Main Branch"
        self.store.privilege_rules["Hotel Co"] = [
            {"privilege": "reopen_till", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_amount": 0},
            {"privilege": "over_short_close", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_amount": 10000},
            {"privilege": "over_short_close", "role": "URY Admin", "may_perform": 1, "may_authorise": 1, "max_amount": 0},
        ]
        self.store.current_user = "cashier@example.com"

        fc_reason = "Other: Night audit force close"
        fc_claim = {
            "action": "force_close",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "reason": fc_reason,
            "discard_pending_movements": False,
        }
        self.override.mint(
            "tok-fc-cap-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="reopen_till",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=fc_claim,
        )

        # Variance is -90,000 (exceeds tolerance 5,000 AND manager's over_short_close max_amount 10,000).
        # Omitting over_short_approval_token MUST be refused with POS-CLOSE-VARIANCE-APPROVAL
        # and MUST NOT consume tok-fc-cap-1 prematurely.
        with self.assertRaisesRegex(PermissionDenied, r"POS-CLOSE-VARIANCE-APPROVAL"):
            self.tc.force_close_till(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 60000}],
                reason=fc_reason,
                approval_token="tok-fc-cap-1",
            )
        self.assertNotIn("tok-fc-cap-1", self.override.consumed)
        self.assertEqual(len(self.cs.consolidations), 0)

        # Retrying with an explicit over_short_close PIN from URY Admin (unlimited) succeeds!
        os_reason = "Other: Approved 90k force close shortage"
        os_claim = {
            "action": "over_short_close",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "counted_payments": [{"mode_of_payment": "Cash", "closing_amount": 60000}],
            "reason": os_reason,
        }
        self.override.mint(
            "tok-fc-os-admin",
            requester="cashier@example.com",
            approver="admin_mgr@example.com",
            privilege="over_short_close",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=os_claim,
        )
        res = self.tc.force_close_till(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 60000}],
            reason=fc_reason,
            approval_token="tok-fc-cap-1",
            over_short_reason=os_reason,
            over_short_approval_token="tok-fc-os-admin",
        )
        closing = self.cs.closings[res["name"]]
        self.assertEqual(closing.docstatus, 1)
        self.assertEqual(closing.custom_force_closed, 1)
        self.assertEqual(closing.custom_over_short_approver, "admin_mgr@example.com")

    def test_concurrent_double_close_query_deadlock_1020_returns_clean_already_closed_refusal(self):
        # Defect 4 (R1 live Barrier(2) probe): MariaDB REPEATABLE-READ FOR UPDATE raises
        # QueryDeadlockError(1020, "Record has changed since last read in table 'tabPOS Opening Entry'")
        # on concurrent double-close. close_till_with_controls must catch it and raise a clean
        # POS-CLOSE-ALREADY-CLOSED ValidationError (never HTTP 500).
        class QueryDeadlockError(Exception):
            pass

        self.frappe.QueryDeadlockError = QueryDeadlockError
        self.frappe.exceptions = SimpleNamespace(QueryDeadlockError=QueryDeadlockError)
        self.frappe.db.rollback = MagicMock()
        self.store.current_user = "cashier@example.com"

        orig_get_value = self.frappe.db.get_value.side_effect

        def deadlock_on_for_update(doctype, filters=None, fieldname=None, as_dict=False, for_update=False, **kwargs):
            if doctype == "POS Opening Entry" and for_update:
                raise QueryDeadlockError(
                    1020,
                    "Record has changed since last read in table 'tabPOS Opening Entry'; try restarting transaction",
                )
            return orig_get_value(doctype, filters, fieldname, as_dict=as_dict, for_update=for_update, **kwargs)

        with patch.object(self.frappe.db, "get_value", side_effect=deadlock_on_for_update):
            with self.assertRaisesRegex(Refused, r"POS-CLOSE-ALREADY-CLOSED") as ctx:
                self.tc.close_till_with_controls(
                    pos_profile="Main Till",
                    pos_opening_entry="OPEN-MAIN-1",
                    counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 150000}],
                )
        self.assertIsNone(ctx.exception.__context__)

    def test_pending_movement_refused_before_variance_approval_for_cashier(self):
        # A pending draft cash movement makes counted cash look short vs expected;
        # close_till_with_controls must refuse with POS-CLOSE-PENDING-MOVEMENT before
        # raising a misleading POS-CLOSE-VARIANCE-APPROVAL.
        self.store.journal_entries["JE-PEND-1"] = FakeDoc(
            {
                "doctype": "Journal Entry",
                "name": "JE-PEND-1",
                "docstatus": 0,
                "custom_pos_opening_entry": "OPEN-MAIN-1",
                "custom_cash_movement_type": "Paid Out",
                "total_debit": 60000,
            },
            store=self.store,
        )
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(Refused, r"POS-CLOSE-PENDING-MOVEMENT"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 90000}],
            )

    def test_manager_with_over_short_close_may_perform_within_cap_closes_own_till_and_admin_closes_natively(self):
        # Under #147 R2 Simphony rules, may_perform=1 authorises solo execution up to max_amount
        # (allow_self_approval=0 governs PIN overrides, not solo may_perform), while variances
        # above max_amount still refuse with POS-CLOSE-VARIANCE-APPROVAL.
        self.store.profiles["Main Till"]["custom_over_short_tolerance"] = 5000
        self.store.privilege_rules["Hotel Co"] = [
            {
                "company": "Hotel Co",
                "privilege": "over_short_close",
                "role": "URY Manager",
                "may_perform": 1,
                "may_authorise": 1,
                "allow_self_approval": 0,
                "max_amount": 10000,
            },
            {
                "company": "Hotel Co",
                "privilege": "close_other_till",
                "role": "URY Manager",
                "may_perform": 1,
                "may_authorise": 1,
                "allow_self_approval": 0,
                "max_amount": 0,
            },
        ]
        self.store.openings["OPEN-MGR-1"] = FakeDoc(
            {
                "doctype": "POS Opening Entry",
                "name": "OPEN-MGR-1",
                "pos_profile": "Main Till",
                "company": "Hotel Co",
                "user": "manager@example.com",
                "status": "Open",
                "docstatus": 1,
                "period_start_date": "2026-11-24 08:00:00",
                "balance_details": [Row({"mode_of_payment": "Cash", "opening_amount": 100000})],
            },
            store=self.store,
        )
        # Expected cash = 100,000 open + 50,000 invoice = 150,000
        self.store.current_user = "manager@example.com"

        # Variance -20,000 exceeds manager's max_amount=10,000 -> MUST still refuse
        with self.assertRaisesRegex(PermissionDenied, r"POS-CLOSE-VARIANCE-APPROVAL"):
            self.tc.close_till_with_controls(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MGR-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 130000}],
            )

        # Variance -8,000 exceeds tolerance 5,000 but is within manager's may_perform cap (10,000) -> succeeds
        res = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MGR-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 142000}],
        )
        self.assertEqual(res["docstatus"], 1)

        # Native Administrator submit on another cashier's shift also succeeds when rules are active
        self.store.current_user = "Administrator"
        closing = self.frappe.new_doc("POS Closing Entry")
        closing.pos_opening_entry = "OPEN-MAIN-1"
        closing.pos_profile = "Main Till"
        closing.company = "Hotel Co"
        closing.user = "cashier@example.com"
        closing.period_start_date = "2026-11-24 08:00:00"
        closing.period_end_date = "2026-11-24 18:00:00"
        closing.append("payment_reconciliation", {"mode_of_payment": "Cash", "closing_amount": 140000})
        closing.insert(ignore_permissions=True)
        closing.submit()
        self.assertEqual(closing.docstatus, 1)

    def test_capped_manager_over_short_pin_without_amount_in_claim_refused_blind_safely(self):
        # Even when an over_short_close token's claim omits "amount" (blind-close UX),
        # _require_till_privilege must enforce the approver's over_short_close max_amount
        # against the server-computed variance, without leaking amounts or burning fc_token.
        self.store.profiles["Main Till"]["custom_blind_cash_count"] = 1
        self.store.profiles["Main Till"]["custom_over_short_tolerance"] = 5000
        self.store.user_roles["admin_mgr@example.com"] = ["URY Admin"]
        self.store.user_branches["admin_mgr@example.com"] = "Main Branch"
        self.store.privilege_rules["Hotel Co"] = [
            {"company": "Hotel Co", "privilege": "reopen_till", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_amount": 0},
            {"company": "Hotel Co", "privilege": "over_short_close", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_amount": 10000},
            {"company": "Hotel Co", "privilege": "over_short_close", "role": "URY Admin", "may_perform": 1, "may_authorise": 1, "max_amount": 0},
        ]
        self.store.current_user = "cashier@example.com"

        fc_reason = "Other: Night audit blind force close"
        fc_claim = {
            "action": "force_close",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "reason": fc_reason,
            "discard_pending_movements": False,
        }
        self.override.mint(
            "tok-fc-blind-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="reopen_till",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=fc_claim,
        )

        # Capped manager (max_amount=10,000) mints an over_short_close token WITHOUT amount in claim.
        # Actual variance = -90,000 (150,000 expected vs 60,000 counted).
        os_reason = "Other: Blind count shortage"
        os_claim = {
            "action": "over_short_close",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "counted_payments": [{"mode_of_payment": "Cash", "closing_amount": 60000}],
            "reason": os_reason,
        }
        self.override.mint(
            "tok-os-capped-mgr",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="over_short_close",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=os_claim,
        )

        with self.assertRaises(PermissionDenied) as ctx:
            self.tc.force_close_till(
                pos_profile="Main Till",
                pos_opening_entry="OPEN-MAIN-1",
                counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 60000}],
                reason=fc_reason,
                approval_token="tok-fc-blind-1",
                over_short_reason=os_reason,
                over_short_approval_token="tok-os-capped-mgr",
            )
        msg = str(ctx.exception)
        self.assertIn("POS-AUTH-NOT-ELIGIBLE", msg)
        self.assertNotIn("150000", msg.replace(",", ""))
        self.assertNotIn("90000", msg.replace(",", ""))
        # Force-close token MUST still be preserved for retry with URY Admin!
        self.assertNotIn("tok-fc-blind-1", self.override.consumed)
        self.assertNotIn("tok-os-capped-mgr", self.override.consumed)

        # Retry with URY Admin (unlimited) over_short_close token + SAME fc token succeeds!
        self.override.mint(
            "tok-os-admin-ok",
            requester="cashier@example.com",
            approver="admin_mgr@example.com",
            privilege="over_short_close",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=os_claim,
        )
        res = self.tc.force_close_till(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=[{"mode_of_payment": "Cash", "closing_amount": 60000}],
            reason=fc_reason,
            approval_token="tok-fc-blind-1",
            over_short_reason=os_reason,
            over_short_approval_token="tok-os-admin-ok",
        )
        self.assertEqual(res["docstatus"], 1)
        self.assertEqual(self.cs.closings[res["name"]].custom_over_short_approver, "admin_mgr@example.com")


class TestTipDeclarationJsonStringParsing(LoadedSlice2):
    """[R3 P1] Form-encoded / JSON-string `tip_declarations` must persist on controlled & force close."""

    def setUp(self):
        super().setUp()
        self.frappe.parse_json = json.loads

    def test_parse_tip_declarations_parses_json_string_and_does_not_swallow_unexpected_errors(self):
        payload = json.dumps(
            [
                {
                    "user": "waiter@example.com",
                    "employee": "HR-EMP-0001",
                    "currency": "UGX",
                    "charged_tips_collected": 5000,
                    "tips_paid_from_till": 0,
                    "net_tips_payable": 5000,
                    "declared_cash_tips": 15000,
                    "remarks": "Form-encoded shift tips",
                }
            ]
        )
        rows = self.tc._parse_tip_declarations(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user"], "waiter@example.com")
        self.assertEqual(rows[0]["employee"], "HR-EMP-0001")
        self.assertEqual(rows[0]["declared_cash_tips"], 15000)
        self.assertEqual(rows[0]["remarks"], "Form-encoded shift tips")

        # Malformed JSON returns [] safely via (ValueError, TypeError)
        self.assertEqual(self.tc._parse_tip_declarations("{not-valid-json"), [])

        # Unexpected runtime/name errors MUST NOT be silently swallowed into []
        def _boom(_val):
            raise NameError("simulated unexpected error")

        self.frappe.parse_json = _boom
        with self.assertRaises(NameError):
            self.tc._parse_tip_declarations("[]")

    def test_close_till_with_controls_persists_json_string_tip_declarations(self):
        self.store.current_user = "cashier@example.com"
        res = self.tc.close_till_with_controls(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=json.dumps([{"mode_of_payment": "Cash", "closing_amount": 150000}]),
            tip_declarations=json.dumps(
                [
                    {
                        "user": "waiter@example.com",
                        "currency": "UGX",
                        "charged_tips_collected": 0,
                        "tips_paid_from_till": 0,
                        "net_tips_payable": 0,
                        "declared_cash_tips": 15000,
                        "remarks": "Form-encoded close tips",
                    }
                ]
            ),
        )
        self.assertEqual(res["docstatus"], 1)
        closing = self.cs.closings[res["name"]]
        tip_rows = closing.get("custom_tip_declarations") or []
        self.assertEqual(len(tip_rows), 1)
        self.assertEqual(tip_rows[0]["user"], "waiter@example.com")
        self.assertEqual(float(tip_rows[0]["declared_cash_tips"]), 15000.0)
        self.assertEqual(tip_rows[0]["remarks"], "Form-encoded close tips")

    def test_force_close_till_persists_json_string_tip_declarations(self):
        self.store.current_user = "manager@example.com"
        res = self.tc.force_close_till(
            pos_profile="Main Till",
            pos_opening_entry="OPEN-MAIN-1",
            counted_payments=json.dumps([{"mode_of_payment": "Cash", "closing_amount": 150000}]),
            reason="Manager force close with JSON-string tips",
            tip_declarations=json.dumps(
                [
                    {
                        "user": "waiter@example.com",
                        "currency": "UGX",
                        "charged_tips_collected": 0,
                        "tips_paid_from_till": 0,
                        "net_tips_payable": 0,
                        "declared_cash_tips": 22000,
                        "remarks": "Force close tips",
                    }
                ]
            ),
        )
        self.assertEqual(res["docstatus"], 1)
        closing = self.cs.closings[res["name"]]
        tip_rows = closing.get("custom_tip_declarations") or []
        self.assertEqual(len(tip_rows), 1)
        self.assertEqual(tip_rows[0]["user"], "waiter@example.com")
        self.assertEqual(float(tip_rows[0]["declared_cash_tips"]), 22000.0)
        self.assertEqual(tip_rows[0]["remarks"], "Force close tips")


if __name__ == "__main__":
    unittest.main()
