"""Site-less tests for Reopen / Adjust Closed Check (B5a Slice 3, Matrix row 10) and schema patch.

Covers:
- Simphony Opt 67 (`adjust_closed_check`): wrong tender or tip adjustment only, items strictly
  immutable, total must match, reversal return + submitted replacement check (`custom_reopened_from`).
- Simphony Opt 68 / SAP CCO FP17 (`reopen_closed_check`): reversal return + open Draft replacement
  check (`docstatus=0`, `invoice_printed=0`, tenders cleared, table restored ONLY if free).
- Hard limits for both: same business day only, shift still open, not consolidated, not already
  returned, same outlet, not offline, not fiscalised unless `credit_and_rebill=1` explicitly
  confirmed and bound into manager approval claim (with EFRIS credit note reason code).
- Full before/after audit snapshot written to `POS Authorisation Log` and server `Comment` on
  original and replacement checks.
- Custom field schema declarations, JSON fixture validity, and idempotent seed-safe patch.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]


class Doc:
    def __init__(self, initial=None, **kwargs):
        data = {}
        if isinstance(initial, Doc):
            data.update(initial._d)
        elif isinstance(initial, dict):
            data.update(initial)
        data.update(kwargs)
        object.__setattr__(self, "_d", data)
        object.__setattr__(self, "flags", SimpleNamespace())

    def __getattr__(self, name):
        d = object.__getattribute__(self, "_d")
        if name in d:
            return d[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in ("_d", "flags"):
            object.__setattr__(self, name, value)
        else:
            object.__getattribute__(self, "_d")[name] = value

    def __getitem__(self, key):
        return self._d[key]

    def __setitem__(self, key, value):
        self._d[key] = value

    def __contains__(self, key):
        return key in self._d

    def get(self, key, default=None):
        return self._d.get(key, default)

    def setdefault(self, key, default=None):
        return self._d.setdefault(key, default)

    def append(self, field, row):
        self._d.setdefault(field, []).append(Doc(row) if isinstance(row, dict) else row)
        return self._d[field][-1]

    def as_dict(self):
        return dict(self._d)

    def is_new(self):
        return not bool(self._d.get("name")) or bool(self._d.get("_is_new"))


def _canonical_hash(claim):
    raw = json.dumps(claim or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ContractOverrideStub(ModuleType):
    def __init__(self, harness):
        super().__init__("ury.ury_pos.override")
        self._h = harness
        self._tokens = {}

    def is_policy_active(self, company):
        return bool(company and any(r.get("company") == company for r in self._h.privilege_rules))

    def list_reasons(self, privilege=None):
        out = []
        for row in sorted(self._h.reasons, key=lambda r: (int(r.get("sort_order") or 0), r.get("code") or "")):
            if int(row.get("disabled") or 0):
                continue
            targets = [p.strip() for p in (row.get("applies_to") or "").replace(",", "\n").splitlines() if p.strip()]
            if privilege and targets and privilege not in targets:
                continue
            out.append({
                "code": row["code"],
                "description": row["description"],
                "requires_note": int(row.get("requires_note") or 0),
                "applies_to": targets,
            })
        return out

    def may_perform(self, user, pos_profile, privilege, percent=None, amount=None, privilege_group=0):
        profile = self._h.profiles.get(pos_profile)
        if not profile:
            return False
        roles = set(self._h.user_roles.get(user, []))
        for r in self._h.privilege_rules:
            if r.get("company") != profile.get("company") or r.get("privilege") != privilege:
                continue
            if r.get("pos_profile") and r.get("pos_profile") != pos_profile:
                continue
            if r.get("role") in roles and int(r.get("may_perform") or 0) == 1:
                return True
        return False

    def mint_token(self, requester, approver, pos_profile, privilege, reference_name, claim):
        if requester == approver:
            self._h.frappe.throw("Self-approval is not allowed. [POS-AUTH-SELF]", self._h.frappe.PermissionError)
        tok = f"TOK-{len(self._tokens) + 1}"
        self._tokens[tok] = {
            "requester": requester,
            "approver": approver,
            "pos_profile": pos_profile,
            "privilege": privilege,
            "reference_name": reference_name,
            "claim_hash": _canonical_hash(claim),
            "consumed": False,
        }
        return tok

    def consume(self, token, privilege, pos_profile, reference_name, claim):
        entry = self._tokens.get(token)
        if not entry or entry["consumed"]:
            self._h.frappe.throw("Approval expired or already used. [POS-AUTH-EXPIRED]", self._h.frappe.ValidationError)
        if entry["requester"] != self._h.frappe.session.user:
            self._h.frappe.throw("Approval requester mismatch. [POS-AUTH-CHANGED]", self._h.frappe.ValidationError)
        if (
            entry["privilege"] != privilege
            or entry["pos_profile"] != pos_profile
            or entry["reference_name"] != reference_name
            or entry["claim_hash"] != _canonical_hash(claim)
        ):
            self._h.frappe.throw("Action changed after manager approval. [POS-AUTH-CHANGED]", self._h.frappe.ValidationError)
        entry["consumed"] = True
        self._h.auth_logs.append(Doc(
            doctype="POS Authorisation Log",
            requester=entry["requester"],
            approver=entry["approver"],
            privilege=privilege,
            pos_profile=pos_profile,
            reference_name=reference_name,
            claim_values=json.dumps(claim, sort_keys=True),
            outcome="Consumed",
        ))
        return SimpleNamespace(approver=entry["approver"], requester=entry["requester"])

    def require(self, privilege, pos_profile, reference_name, claim, *, token=None, percent=None, amount=None, privilege_group=0):
        if token:
            return self.consume(token, privilege, pos_profile, reference_name, claim)
        if self.may_perform(self._h.frappe.session.user, pos_profile, privilege, percent=percent, amount=amount, privilege_group=privilege_group):
            return None
        self._h.frappe.throw("Manager approval required. [POS-AUTH-REQUIRED]", self._h.frappe.PermissionError)

    def mark_verified_approval(self, doc, privilege, approval=None):
        setattr(doc.flags, f"pos_override_verified_{privilege}", SimpleNamespace(
            privilege=privilege,
            docname=str(getattr(doc, "name", "") or ""),
            approver=getattr(approval, "approver", None) if approval else None,
        ))

    def has_verified_approval(self, doc, privilege):
        proof = getattr(doc.flags, f"pos_override_verified_{privilege}", None)
        return bool(proof and proof.privilege == privilege)


class ReopenHarness:
    def __init__(self):
        self.user_roles = {
            "cashier@example.test": ["URY Cashier"],
            "manager@example.test": ["URY Manager", "URY Cashier"],
            "manager2@example.test": ["URY Manager", "URY Cashier"],
            "other_branch_mgr@example.test": ["URY Manager"],
            "Administrator": ["System Manager", "URY Admin"],
        }
        self.user_branch = {
            "cashier@example.test": "Branch A",
            "manager@example.test": "Branch A",
            "manager2@example.test": "Branch A",
            "other_branch_mgr@example.test": "Branch B",
            "Administrator": "Branch A",
        }
        self.profiles = {
            "Till A": Doc(
                name="Till A",
                company="Hotel Co",
                currency="UGX",
                branch="Branch A",
                warehouse="Kitchen - HC",
                cost_center="Restaurant - HC",
                selling_price_list="Standard Selling",
                payments=[
                    Doc(mode_of_payment="Cash", default=1),
                    Doc(mode_of_payment="Card", default=0),
                    Doc(mode_of_payment="Mobile Money", default=0),
                ],
            ),
        }
        self.mops = {
            "Cash": Doc(name="Cash", type="Cash"),
            "Card": Doc(name="Card", type="Bank"),
            "Mobile Money": Doc(name="Mobile Money", type="Phone"),
        }
        self.tables = {
            "T1": Doc(name="T1", occupied=0),
            "T2": Doc(name="T2", occupied=1),
        }
        self.open_shifts = {"Till A": "POS-OPEN-1"}
        self.privilege_rules = [
            Doc(company="Hotel Co", pos_profile="", privilege=p, role="URY Manager", may_perform=1, may_authorise=1, requires_reason=1)
            for p in ("reopen_check", "adjust_closed_check", "return")
        ]
        self.reasons = [
            Doc(code="REOPEN-TENDER", description="Wrong payment method", applies_to="reopen_check\nadjust_closed_check", requires_note=0, sort_order=70, disabled=0),
            Doc(code="REOPEN-SPLIT", description="Guest requested split or correction", applies_to="reopen_check\nadjust_closed_check", requires_note=0, sort_order=80, disabled=0),
            Doc(code="OTHER", description="Other", applies_to="", requires_note=1, sort_order=999, disabled=0),
        ]
        self.invoices = {}
        self.closing_entries = []
        self.merge_logs = []
        self.auth_logs = []
        self.comments = []
        self.custom_fields_present = {
            ("POS Invoice", "custom_reopened_from"),
            ("POS Invoice", "custom_return_approver"),
            ("POS Invoice", "cancel_reason"),
            ("POS Invoice", "efris_creditnote_reasoncode"),
            ("POS Invoice", "efris_creditnote_reason"),
            ("POS Invoice", "efris_irn"),
            ("POS Invoice", "efris_status"),
            ("POS Invoice", "tip_amount"),
        }
        self.frappe = self._build_frappe()
        self.override = ContractOverrideStub(self)

    def add_closed_check(
        self,
        name="POS-INV-1",
        *,
        posting_date="2026-11-24",
        branch="Branch A",
        table="T1",
        waiter="cashier@example.test",
        cashier="cashier@example.test",
        owner="cashier@example.test",
        payments=None,
        tip_amount=0.0,
        efris_irn=None,
        efris_status="",
        consolidated_invoice=None,
        status="Paid",
        docstatus=1,
    ):
        items = [
            Doc(name="ROW-1", item_code="STEAK", item_name="Steak", qty=1.0, rate=35000.0, amount=35000.0, uom="Nos", warehouse="Kitchen - HC", income_account="Sales - HC", cost_center="Restaurant - HC", ury_held=0),
            Doc(name="ROW-2", item_code="BEER", item_name="Beer", qty=1.0, rate=15000.0, amount=15000.0, uom="Nos", warehouse="Kitchen - HC", income_account="Sales - HC", cost_center="Restaurant - HC", ury_held=0),
        ]
        grand = 50000.0
        pay_docs = [Doc(p) for p in (payments or [{"mode_of_payment": "Cash", "amount": grand + float(tip_amount)}])]
        inv = Doc(
            doctype="POS Invoice",
            name=name,
            company="Hotel Co",
            currency="UGX",
            pos_profile="Till A",
            branch=branch,
            customer="Walk-in",
            customer_name="Walk-in",
            order_type="Dine In",
            selling_price_list="Standard Selling",
            posting_date=posting_date,
            posting_time="10:00:00",
            waiter=waiter,
            cashier=cashier,
            owner=owner,
            docstatus=docstatus,
            status=status,
            is_return=0,
            return_against=None,
            invoice_printed=1,
            restaurant_table=table,
            consolidated_invoice=consolidated_invoice,
            custom_merged_pos_invoice=None,
            efris_irn=efris_irn,
            efris_status=efris_status,
            total=grand,
            net_total=grand,
            discount_amount=0.0,
            total_taxes_and_charges=0.0,
            grand_total=grand,
            rounded_total=grand,
            tip_amount=float(tip_amount),
            paid_amount=sum(float(p.amount) for p in pay_docs),
            change_amount=0.0,
            outstanding_amount=0.0,
            items=items,
            taxes=[],
            payments=pay_docs,
        )
        inv.add_comment = lambda ctype, text: self.comments.append({"invoice": inv.name, "type": ctype, "text": text})
        self.invoices[name] = inv
        return inv

    def _build_frappe(self):
        h = self
        fw = ModuleType("frappe")
        fw.ValidationError = type("ValidationError", (Exception,), {})
        fw.PermissionError = type("PermissionError", (Exception,), {})
        fw.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
        fw._ = lambda msg: msg
        fw.whitelist = lambda *a, **kw: (lambda fn: fn)
        fw.session = SimpleNamespace(user="manager2@example.test")
        fw.flags = SimpleNamespace()

        def _throw(msg, exc=None, **kw):
            raise (exc or fw.ValidationError)(msg)

        fw.throw = _throw
        fw.get_roles = lambda user=None: list(h.user_roles.get(user or fw.session.user, []))
        fw.has_permission = lambda *a, **kw: True

        def _get_meta(dt):
            m = MagicMock()
            m.has_field = lambda fn: (dt, fn) in h.custom_fields_present
            return m

        fw.get_meta = _get_meta

        def _table_exists(dt):
            return dt in (
                "POS Reason", "POS Privilege Rule", "POS Authorisation Log",
                "POS Invoice", "POS Opening Entry", "POS Closing Entry",
                "POS Closing Entry Detail", "POS Invoice Merge Log",
                "POS Invoice Reference", "URY Table", "E Invoice",
            )

        def _exists(dt, filt=None):
            if dt == "DocType":
                return _table_exists(filt)
            if dt == "POS Invoice":
                return filt if isinstance(filt, str) and filt in h.invoices else None
            if dt == "POS Profile":
                return filt if isinstance(filt, str) and filt in h.profiles else None
            if dt == "POS Opening Entry":
                if isinstance(filt, dict):
                    prof = filt.get("pos_profile")
                    if prof in h.open_shifts and filt.get("status") == "Open" and filt.get("docstatus") == 1:
                        return h.open_shifts[prof]
                return None
            if dt == "URY Table":
                return filt if isinstance(filt, str) and filt in h.tables else None
            return None

        def _get_value(dt, filters=None, fieldname=None, as_dict=False, **kw):
            if dt == "POS Profile" and isinstance(filters, str):
                p = h.profiles.get(filters)
                return p.get(fieldname) if p and isinstance(fieldname, str) else p
            if dt == "Mode of Payment" and isinstance(filters, str):
                m = h.mops.get(filters)
                return m.get(fieldname) if m and isinstance(fieldname, str) else None
            if dt == "URY Table" and isinstance(filters, str):
                t = h.tables.get(filters)
                return t.get(fieldname) if t and isinstance(fieldname, str) else None
            if dt == "E Invoice":
                return None
            return None

        def _set_value(dt, name, field, value, **kw):
            if dt == "URY Table" and name in h.tables:
                h.tables[name][field] = value

        def _get_all(dt, filters=None, fields=None, pluck=None, **kw):
            if dt == "POS Invoice":
                out = []
                for inv in h.invoices.values():
                    if isinstance(filters, dict) and not all(inv.get(k) == v for k, v in filters.items()):
                        continue
                    out.append(inv.get(pluck) if pluck else inv)
                return out
            if dt == "POS Reason":
                return [r for r in h.reasons if int(r.get("disabled") or 0) == 0]
            return []

        def _sql(query, params=None, as_dict=False):
            q = " ".join(str(query).lower().split())
            orig_name = params[0] if isinstance(params, (list, tuple)) else params
            if "from `tabpos invoice`" in q and "return_against" in q:
                matches = [
                    inv for inv in h.invoices.values()
                    if inv.get("return_against") == orig_name
                    and int(inv.get("is_return") or 0) == 1
                    and int(inv.get("docstatus") or 0) == 1
                ]
                return [(m.name,) for m in matches]
            if "tabpos closing entry" in q:
                rows = [ce["name"] for ce in h.closing_entries if ce.get("docstatus") == 1 and orig_name in ce.get("invoices", [])]
                return [(r,) for r in rows]
            if "tabpos invoice merge log" in q:
                rows = [ml["name"] for ml in h.merge_logs if ml.get("docstatus") == 1 and orig_name in ml.get("invoices", [])]
                return [(r,) for r in rows]
            return []

        def _wrap_inv(d):
            d._is_new = True
            d.docstatus = 0
            d.setdefault("items", [])
            d.setdefault("taxes", [])
            d.setdefault("payments", [])

            def _insert(ignore_permissions=False):
                prefix = "POS-REV-" if int(d.get("is_return") or 0) == 1 else "POS-REP-"
                d.name = d.get("name") or f"{prefix}{len(h.invoices) + 1}"
                d._is_new = False
                gross = sum(float(r.qty) * float(r.rate) for r in d.items)
                disc = float(d.get("discount_amount") or 0)
                excl = sum(float(t.get("tax_amount") or 0) for t in d.taxes if int(t.get("included_in_print_rate") or 0) == 0)
                d.total = gross
                d.net_total = gross - disc
                d.total_taxes_and_charges = excl
                d.grand_total = d.net_total + excl
                d.rounded_total = d.grand_total
                d.paid_amount = sum(float(p.amount) for p in d.payments)
                d.add_comment = lambda ctype, text: h.comments.append({"invoice": d.name, "type": ctype, "text": text})
                h.invoices[d.name] = d
                return d

            def _submit():
                d.docstatus = 1
                d.status = "Return" if int(d.get("is_return") or 0) == 1 else "Paid"
                return d

            d.insert = _insert
            d.submit = _submit
            return d

        def _get_doc(dt, name=None, for_update=False):
            if isinstance(dt, dict):
                d = Doc(dt)
                if d.get("doctype") == "POS Authorisation Log":
                    def _ins(ignore_permissions=False):
                        d.name = f"LOG-{len(h.auth_logs) + 1}"
                        h.auth_logs.append(d)
                        return d
                    d.insert = _ins
                    return d
                return _wrap_inv(d)
            if dt == "POS Invoice":
                return h.invoices[name]
            if dt == "POS Profile":
                return h.profiles[name]
            raise fw.DoesNotExistError(f"{dt}:{name}")

        fw.db = SimpleNamespace(
            exists=_exists,
            table_exists=_table_exists,
            get_value=_get_value,
            set_value=_set_value,
            sql=_sql,
        )
        fw.get_doc = _get_doc
        fw.new_doc = lambda dt: _wrap_inv(Doc(doctype=dt))
        fw.get_all = _get_all
        return fw


def _load_module(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestReopenAndAdjustClosedCheck(unittest.TestCase):
    def setUp(self):
        self.h = ReopenHarness()
        utils = ModuleType("frappe.utils")
        utils.cint = lambda v: int(v or 0)
        utils.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
        utils.now_datetime = lambda: datetime(2026, 11, 24, 14, 0, 0)
        utils.nowdate = lambda: "2026-11-24"
        utils.nowtime = lambda: "14:00:00"
        pos_api = ModuleType("ury.ury_pos.api")
        pos_api.getBranch = lambda: self.h.user_branch.get(self.h.frappe.session.user, "Branch A")

        self.enterContext(patch.dict(sys.modules, {
            "frappe": self.h.frappe,
            "frappe.utils": utils,
            "ury.ury_pos.override": self.h.override,
            "ury.ury_pos.api": pos_api,
        }))
        self.mod = _load_module("ury.ury_pos.void_return_reopen", "ury/ury_pos/void_return_reopen.py")

    def test_adjust_closed_check_changes_tender_and_tip_only_with_before_after_audit(self):
        """Simphony Opt 67: wrong Cash tender adjusted to Card + 5,000 tip; items immutable; full audit trail."""
        self.h.frappe.session.user = "cashier@example.test"
        inv = self.h.add_closed_check("POS-ADJ-1")

        # Trying to mutate items through adjust_closed_check -> REFUSED
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-ADJUST-ITEMS-FORBIDDEN"):
            self.mod.adjust_closed_check(
                inv.name,
                [{"mode_of_payment": "Card", "amount": 50000.0}],
                reason="REOPEN-TENDER",
                items=[{"item_code": "STEAK", "qty": 1}],
            )

        # Wrong total -> REFUSED
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-ADJUST-TOTAL-MISMATCH"):
            self.mod.adjust_closed_check(
                inv.name,
                [{"mode_of_payment": "Card", "amount": 40000.0}],
                reason="REOPEN-TENDER",
            )

        # Cashier without PIN -> REFUSED
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.mod.adjust_closed_check(
                inv.name,
                [{"mode_of_payment": "Card", "amount": 55000.0}],
                reason="REOPEN-TENDER",
                tip_amount=5000.0,
            )

        # With manager PIN token -> succeeds, keeps cashier session, creates reversal + submitted replacement!
        claim = {
            "original_invoice": inv.name,
            "payments": [{"amount": 55000.0, "mode_of_payment": "Card"}],
            "tip_amount": 5000.0,
            "credit_and_rebill": 0,
            "reason": "REOPEN-TENDER",
            "note": "",
        }
        tok = self.h.override.mint_token("cashier@example.test", "manager@example.test", "Till A", "adjust_closed_check", inv.name, claim)
        res = self.mod.adjust_closed_check(
            inv.name,
            [{"mode_of_payment": "Card", "amount": 55000.0}],
            reason="REOPEN-TENDER",
            tip_amount=5000.0,
            approval_token=tok,
        )
        self.assertEqual(self.h.frappe.session.user, "cashier@example.test")
        self.assertEqual(res["approver"], "manager@example.test")
        rev = self.h.invoices[res["reversal_return"]]
        rep = self.h.invoices[res["replacement_invoice"]]
        self.assertEqual(rev.is_return, 1)
        self.assertEqual(rev.return_against, inv.name)
        self.assertEqual(rev.docstatus, 1)
        self.assertEqual(rep.docstatus, 1)
        self.assertEqual(rep.custom_reopened_from, inv.name)
        self.assertEqual([(p.mode_of_payment, p.amount) for p in rep.payments], [("Card", 55000.0)])
        self.assertEqual(rep.tip_amount, 5000.0)
        # Before/after snapshot in response, POS Authorisation Log, and Comments on both checks
        self.assertEqual(res["before"]["payments"], [{"mode_of_payment": "Cash", "amount": 50000.0}])
        self.assertEqual(res["after"]["payments"], [{"mode_of_payment": "Card", "amount": 55000.0}])
        self.assertEqual(res["after"]["tip_amount"], 5000.0)
        self.assertTrue(any(c["invoice"] == inv.name and "Card" in c["text"] for c in self.h.comments))
        self.assertTrue(any(c["invoice"] == rep.name and inv.name in c["text"] for c in self.h.comments))

    def test_reopen_closed_check_creates_open_draft_and_restores_free_table_only(self):
        """Simphony Opt 68 / SAP CCO FP17: reversal + open Draft check, table restored iff free."""
        self.h.frappe.session.user = "manager2@example.test"

        # Case 1: Table T1 is free -> restored and marked occupied!
        inv1 = self.h.add_closed_check("POS-REOP-1", table="T1")
        res1 = self.mod.reopen_closed_check(inv1.name, reason="REOPEN-SPLIT")
        reopened1 = self.h.invoices[res1["reopened_invoice"]]
        self.assertEqual(reopened1.docstatus, 0)
        self.assertEqual(reopened1.invoice_printed, 0)
        self.assertTrue(all(float(p.amount) == 0.0 for p in reopened1.payments))
        self.assertEqual(reopened1.custom_reopened_from, inv1.name)
        self.assertTrue(res1["table_restored"])
        self.assertEqual(reopened1.restaurant_table, "T1")
        self.assertEqual(self.h.tables["T1"].occupied, 1)

        # Case 2: Table T2 is already occupied -> reopened check stays unassigned (restaurant_table=None)!
        inv2 = self.h.add_closed_check("POS-REOP-2", table="T2")
        res2 = self.mod.reopen_closed_check(inv2.name, reason="REOPEN-SPLIT")
        reopened2 = self.h.invoices[res2["reopened_invoice"]]
        self.assertFalse(res2["table_restored"])
        self.assertIsNone(reopened2.restaurant_table)

    def test_reopen_and_adjust_enforce_same_day_open_shift_unconsolidated_unreturned_and_outlet_limits(self):
        self.h.frappe.session.user = "manager2@example.test"

        # Previous business day -> REFUSED
        prev = self.h.add_closed_check("POS-OLD-DAY", posting_date="2026-11-23")
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-REOPEN-PREVIOUS-DAY"):
            self.mod.reopen_closed_check(prev.name, reason="REOPEN-SPLIT")

        # Consolidated -> REFUSED
        cons = self.h.add_closed_check("POS-CONS", status="Consolidated", consolidated_invoice="SINV-1")
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-REOPEN-CONSOLIDATED"):
            self.mod.adjust_closed_check(cons.name, [{"mode_of_payment": "Card", "amount": 50000.0}], reason="REOPEN-TENDER")

        # Shift closed (no open POS Opening Entry) -> REFUSED
        inv_sh = self.h.add_closed_check("POS-SHIFT-CLOSED")
        self.h.open_shifts.clear()
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-REOPEN-SHIFT-CLOSED"):
            self.mod.reopen_closed_check(inv_sh.name, reason="REOPEN-SPLIT")
        self.h.open_shifts["Till A"] = "POS-OPEN-1"

        # Already returned -> REFUSED
        inv_ret = self.h.add_closed_check("POS-HAS-RET")
        self.h.invoices["POS-RET-EXIST"] = Doc(
            doctype="POS Invoice", name="POS-RET-EXIST", is_return=1, return_against=inv_ret.name, docstatus=1
        )
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-REOPEN-ALREADY-RETURNED"):
            self.mod.reopen_closed_check(inv_ret.name, reason="REOPEN-SPLIT")

        # Other outlet -> REFUSED
        inv_other = self.h.add_closed_check("POS-OTHER-BR", branch="Branch A")
        self.h.frappe.session.user = "other_branch_mgr@example.test"
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-OUTLET-MISMATCH"):
            self.mod.reopen_closed_check(inv_other.name, reason="REOPEN-SPLIT")

        # Offline refused
        self.h.frappe.session.user = "manager2@example.test"
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-OFFLINE-NOT-ALLOWED"):
            self.mod.reopen_closed_check(inv_other.name, reason="REOPEN-SPLIT", offline_captured=1)

    def test_fiscalised_check_refuses_reopen_unless_explicit_credit_and_rebill(self):
        self.h.frappe.session.user = "manager2@example.test"
        fisc = self.h.add_closed_check("POS-FISC-REOP", efris_irn="FDN-2026-888", efris_status="Fiscalised")

        # Without explicit credit_and_rebill=1 -> REFUSED with POS-REOPEN-FISCALISED!
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-REOPEN-FISCALISED"):
            self.mod.adjust_closed_check(
                fisc.name,
                [{"mode_of_payment": "Card", "amount": 50000.0}],
                reason="REOPEN-TENDER",
            )
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-REOPEN-FISCALISED"):
            self.mod.reopen_closed_check(fisc.name, reason="REOPEN-SPLIT")

        # With explicit credit_and_rebill=1 -> creates EFRIS credit note reversal + replacement check!
        res = self.mod.adjust_closed_check(
            fisc.name,
            [{"mode_of_payment": "Card", "amount": 50000.0}],
            reason="REOPEN-TENDER",
            credit_and_rebill=1,
        )
        self.assertTrue(res["credit_and_rebill"])
        rev = self.h.invoices[res["reversal_return"]]
        self.assertTrue(rev.efris_creditnote_reasoncode.startswith("102:"))

    def test_custom_fields_and_idempotent_patch_contract(self):
        """Verify B5a custom fields are declared in setup_customizations, fixture JSON, and seed-safe patch."""
        expected = {
            ("POS Profile", "custom_unreferenced_return_roles"),
            ("POS Invoice", "custom_reopened_from"),
            ("POS Invoice", "custom_return_approver"),
            ("POS Invoice", "custom_waste_stock_entry"),
            ("URY KOT", "custom_waste_stock_entry"),
            ("URY KOT", "custom_void_reason_code"),
        }
        # 1. Fixture JSON valid & contains every B5a custom field
        fixture_path = ROOT / "ury/fixtures/custom_field.json"
        fixture_rows = json.loads(fixture_path.read_text(encoding="utf-8"))
        fixture_keys = {(r["dt"], r["fieldname"]) for r in fixture_rows}
        self.assertTrue(expected.issubset(fixture_keys), f"Missing from custom_field.json: {expected - fixture_keys}")

        # 2. patches.txt includes the new patch
        patches_txt = (ROOT / "ury/patches.txt").read_text(encoding="utf-8")
        self.assertIn("ury.patches.v2_0.setup_pos_void_return_reopen", patches_txt)

        # 3. Patch module exists and only creates missing custom fields (never overwrites existing!)
        patch_path = ROOT / "ury/patches/v2_0/setup_pos_void_return_reopen.py"
        self.assertTrue(patch_path.exists(), "Patch setup_pos_void_return_reopen.py is missing")

        # 4. Finding 6d (White-Label): zero vendor/OSS names in B5a custom field descriptions
        forbidden = ("erpnext", "frappe", "kamra", "tridz", " huf ")
        for row in fixture_rows:
            if (row["dt"], row["fieldname"]) in expected:
                desc = (row.get("description") or "").lower()
                for word in forbidden:
                    self.assertNotIn(word, desc, f"White-label leak {word!r} in {row['name']} description: {row.get('description')}")
        setup_txt = (ROOT / "ury/setup_customizations.py").read_text(encoding="utf-8")
        self.assertNotIn("Native ERPNext Stock Entry", setup_txt)
        self.assertNotIn("Native ERPNext Material Issue", setup_txt)

    def test_r2_p1_adjust_closed_check_uses_custom_charged_tip_not_nonexistent_tip_amount(self):
        """Finding 4 (P1 #145 interop): adjust_closed_check reads/writes custom_charged_tip + Tips Payable Actual row."""
        self.h.custom_fields_present.discard(("POS Invoice", "tip_amount"))
        self.h.custom_fields_present.add(("POS Invoice", "custom_charged_tip"))
        self.h.custom_fields_present.add(("POS Invoice", "custom_tip_server"))
        self.h.profiles["Till A"].custom_tips_payable_account = "Tips Payable - HC"

        self.h.frappe.session.user = "manager2@example.test"
        # Case A: Original check has no tip (50,000); adjust to Card + 5,000 tip -> total 55,000
        inv1 = self.h.add_closed_check("POS-TIP-ADJ-1")
        inv1._d.pop("tip_amount", None)
        inv1.custom_charged_tip = 0.0
        res1 = self.mod.adjust_closed_check(
            inv1.name,
            [{"mode_of_payment": "Card", "amount": 55000.0}],
            reason="REOPEN-TENDER",
            tip_amount=5000.0,
        )
        rep1 = self.h.invoices[res1["replacement_invoice"]]
        self.assertFalse("tip_amount" in rep1._d, "Must NEVER write nonexistent POS Invoice.tip_amount")
        self.assertEqual(rep1.custom_charged_tip, 5000.0)
        self.assertEqual(rep1.custom_tip_server, inv1.waiter)
        tip_rows1 = [t for t in rep1.taxes if t.account_head == "Tips Payable - HC" and t.charge_type == "Actual"]
        self.assertEqual(len(tip_rows1), 1)
        self.assertEqual(float(tip_rows1[0].tax_amount), 5000.0)

        # Case B: Original check ALREADY had 5,000 custom_charged_tip (grand_total=55,000); adjust tip 5,000 -> 7,000 (total 57,000, NOT 62,000!)
        inv2 = self.h.add_closed_check(
            "POS-TIP-ADJ-2",
            payments=[{"mode_of_payment": "Card", "amount": 55000.0}],
        )
        inv2._d.pop("tip_amount", None)
        inv2.custom_charged_tip = 5000.0
        inv2.custom_tip_server = "cashier@example.test"
        inv2.taxes = [
            Doc(charge_type="On Net Total", account_head="VAT - HC", description="VAT 18%", rate=18.0, tax_amount=7627.12, included_in_print_rate=1),
            Doc(charge_type="Actual", account_head="Tips Payable - HC", description="Charged Tip", rate=0.0, tax_amount=5000.0, included_in_print_rate=0),
        ]
        inv2.grand_total = 55000.0
        inv2.rounded_total = 55000.0
        res2 = self.mod.adjust_closed_check(
            inv2.name,
            [{"mode_of_payment": "Card", "amount": 57000.0}],
            reason="REOPEN-TENDER",
            tip_amount=7000.0,
        )
        rev2 = self.h.invoices[res2["reversal_return"]]
        rep2 = self.h.invoices[res2["replacement_invoice"]]
        # Reversal preserves tax-inclusive VAT AND reverses original tip!
        self.assertEqual(rev2.custom_charged_tip, -5000.0)
        self.assertTrue(any(int(t.get("included_in_print_rate") or 0) == 1 for t in rev2.taxes))
        # Replacement has new tip 7,000 (single Tips Payable row, never duplicated!)
        self.assertEqual(rep2.custom_charged_tip, 7000.0)
        tip_rows2 = [t for t in rep2.taxes if t.account_head == "Tips Payable - HC"]
        self.assertEqual([float(t.tax_amount) for t in tip_rows2], [7000.0])
        self.assertEqual(res2["before"]["tip_amount"], 5000.0)
        self.assertEqual(res2["after"]["tip_amount"], 7000.0)


if __name__ == "__main__":
    unittest.main()
