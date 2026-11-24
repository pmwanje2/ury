"""Site-less tests for POS Return / Refund at the till (B5a Slice 2, Matrix row 9, tracker G34).

Covers:
- Full and partial line return against original check (never above sold qty)
- Exact proportional discount & tax in whole UGX shillings (cumulative remainder closure!)
- Native POS return (`is_return=1`, `return_against`, `restaurant_table=None`, `invoice_printed=0`)
- EFRIS credit note reason code mapping on fiscalised checks
- Refund tender rules: same tender by default (net of change_amount), different tender refused
  unless explicitly allowed and bound in manager PIN claim
- Four-eyes: never return own check alone, never self-approve, approver cannot be check actor
- Unreferenced return: default-denied even for `URY Manager` with `return`; allowed ONLY with
  its own configured `POS Profile.custom_unreferenced_return_roles` + distinct manager PIN + reason
- Adversarial cases:
  - Double return (second return exceeding remaining qty refused)
  - Return after day close / consolidated check refused
  - Concurrent return & consolidation (`for_update=True` re-check against `consolidated_invoice` / Merge Log)
  - Return of a room charge (Cash/Card refused; Room Charge folio credit allowed preserving `room_number`)
  - Partially paid check (refund tender capped at remaining paid amount; excess reduces outstanding)
  - Split check (return isolated to Split A; Split B untouched)
  - Merged check (Add Check single-invoice merge returns cleanly; legacy `custom_merged_pos_invoice` refused)
  - Other outlet's check refused (`POS-OUTLET-MISMATCH`)
  - Another server's check (cashier needs manager PIN; distinct manager may act alone)
  - Offline replay refused (`POS-OFFLINE-NOT-ALLOWED`)
  - Discounted UGX check (3 x 10,000 - 5,000 flat discount = 25,000 returned 1-by-1 -> 8,333 + 8,334 + 8,333 = 25,000!)
  - Direct REST/Desk mutation bypass on `POS Invoice` return blocked by `validate_invoice` hook
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
    """Lightweight document double supporting both attribute and key access without shadowing `.items`."""

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
            "claim": claim,
            "consumed": False,
        }
        return tok

    def consume(self, token, privilege, pos_profile, reference_name, claim):
        entry = self._tokens.get(token)
        if not entry or entry["consumed"]:
            self._h.frappe.throw("Approval expired or already used. [POS-AUTH-EXPIRED]", self._h.frappe.ValidationError)
        if entry["requester"] != self._h.frappe.session.user:
            self._h.frappe.throw("Approval requester mismatch. [POS-AUTH-CHANGED]", self._h.frappe.ValidationError)
        if entry["approver"] == self._h.frappe.session.user:
            self._h.frappe.throw("Self-approval is not allowed. [POS-AUTH-SELF]", self._h.frappe.PermissionError)
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


class ReturnHarness:
    def __init__(self):
        self.user_roles = {
            "cashier@example.test": ["URY Cashier"],
            "waiter1@example.test": ["URY Waiter"],
            "waiter2@example.test": ["URY Waiter"],
            "manager@example.test": ["URY Manager", "URY Cashier"],
            "manager2@example.test": ["URY Manager", "URY Cashier"],
            "gm@example.test": ["URY Admin", "URY Manager"],
            "other_branch_mgr@example.test": ["URY Manager"],
            "Administrator": ["System Manager", "URY Admin"],
        }
        self.user_branch = {
            "cashier@example.test": "Branch A",
            "waiter1@example.test": "Branch A",
            "waiter2@example.test": "Branch A",
            "manager@example.test": "Branch A",
            "manager2@example.test": "Branch A",
            "gm@example.test": "Branch A",
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
                custom_unreferenced_return_roles="",
                payments=[
                    Doc(mode_of_payment="Cash", default=1),
                    Doc(mode_of_payment="Card", default=0),
                    Doc(mode_of_payment="Mobile Money", default=0),
                    Doc(mode_of_payment="Room Charge", default=0),
                ],
            ),
            "Till USD": Doc(
                name="Till USD",
                company="Hotel Co",
                currency="USD",
                branch="Branch A",
                warehouse="Kitchen - HC",
                cost_center="Restaurant - HC",
                selling_price_list="USD Selling",
                custom_unreferenced_return_roles="",
                payments=[
                    Doc(mode_of_payment="Cash", default=1),
                    Doc(mode_of_payment="Card", default=0),
                ],
            ),
        }
        self.mops = {
            "Cash": Doc(name="Cash", type="Cash"),
            "Card": Doc(name="Card", type="Bank"),
            "Mobile Money": Doc(name="Mobile Money", type="Phone"),
            "Room Charge": Doc(name="Room Charge", type="General"),
        }
        self.items_master = {
            "BURGER": Doc(item_code="BURGER", item_name="Burger", is_stock_item=1, stock_uom="Nos"),
            "BEER": Doc(item_code="BEER", item_name="Beer", is_stock_item=1, stock_uom="Nos"),
            "STEAK": Doc(item_code="STEAK", item_name="Steak", is_stock_item=1, stock_uom="Nos"),
        }
        self.item_prices = {
            ("BURGER", "Standard Selling"): 10000.0,
            ("BEER", "Standard Selling"): 15000.0,
            ("STEAK", "Standard Selling"): 35000.0,
        }
        self.privilege_rules = [
            Doc(company="Hotel Co", pos_profile="", privilege="return", role="URY Manager", may_perform=1, may_authorise=1, requires_reason=1),
            Doc(company="Hotel Co", pos_profile="", privilege="return", role="URY Admin", may_perform=1, may_authorise=1, requires_reason=1),
        ]
        self.reasons = [
            Doc(code="RET-QUALITY", description="Item quality complaint", applies_to="return", requires_note=0, sort_order=50, disabled=0),
            Doc(code="RET-OVERCHARGE", description="Billing error / overcharge", applies_to="return", requires_note=0, sort_order=60, disabled=0),
            Doc(code="OTHER", description="Other", applies_to="", requires_note=1, sort_order=999, disabled=0),
        ]
        self.invoices = {}
        self.closing_entries = []  # [{"name": "PCE-1", "docstatus": 1, "invoices": ["POS-INV-1"]}]
        self.merge_logs = []  # [{"name": "PIML-1", "docstatus": 1, "invoices": ["POS-INV-1"]}]
        self.e_invoices = {}  # invoice_name -> {"irn": ...}
        self.auth_logs = []
        self.for_update_locks = []
        self.on_lock_original = None  # hook for simulating concurrent consolidation race!
        self.custom_fields_present = {
            ("POS Profile", "custom_unreferenced_return_roles"),
            ("POS Invoice", "custom_return_approver"),
            ("POS Invoice", "cancel_reason"),
            ("POS Invoice", "efris_creditnote_reasoncode"),
            ("POS Invoice", "efris_creditnote_reason"),
            ("POS Invoice", "room_number"),
            ("POS Invoice", "efris_irn"),
            ("POS Invoice", "efris_status"),
        }
        self.frappe = self._build_frappe()
        self.override = ContractOverrideStub(self)

    def add_closed_check(
        self,
        name="POS-INV-1",
        *,
        currency="UGX",
        pos_profile="Till A",
        branch="Branch A",
        waiter="waiter1@example.test",
        cashier="cashier@example.test",
        owner="waiter1@example.test",
        items=None,
        discount_amount=0.0,
        additional_discount_percentage=0.0,
        apply_discount_on="Grand Total",
        taxes=None,
        payments=None,
        change_amount=0.0,
        outstanding_amount=0.0,
        status="Paid",
        docstatus=1,
        consolidated_invoice=None,
        custom_merged_pos_invoice=None,
        room_number=None,
        efris_irn=None,
        efris_status="",
        posting_date="2026-11-24",
    ):
        raw_items = items or [
            {"name": "ROW-1", "item_code": "BURGER", "item_name": "Burger", "qty": 3, "rate": 10000.0, "amount": 30000.0, "uom": "Nos", "warehouse": "Kitchen - HC", "income_account": "Sales - HC", "cost_center": "Restaurant - HC"},
        ]
        item_docs = []
        for idx, it in enumerate(raw_items, start=1):
            d = Doc(
                name=it.get("name", f"ROW-{idx}"),
                item_code=it["item_code"],
                item_name=it.get("item_name", it["item_code"]),
                qty=float(it["qty"]),
                rate=float(it["rate"]),
                amount=float(it.get("amount", float(it["qty"]) * float(it["rate"]))),
                uom=it.get("uom", "Nos"),
                warehouse=it.get("warehouse", "Kitchen - HC"),
                income_account=it.get("income_account", "Sales - HC"),
                cost_center=it.get("cost_center", "Restaurant - HC"),
                ury_held=0,
            )
            item_docs.append(d)

        gross = sum(r.qty * r.rate for r in item_docs)
        tax_docs = [Doc(t) for t in (taxes or [])]
        excl_tax = sum(float(t.get("tax_amount") or 0) for t in tax_docs if int(t.get("included_in_print_rate") or 0) == 0)
        net_total = gross - float(discount_amount)
        grand_total = net_total + excl_tax

        raw_payments = payments if payments is not None else [{"mode_of_payment": "Cash", "amount": grand_total}]
        pay_docs = [Doc(p) for p in raw_payments]
        paid_amount = sum(float(p.amount) for p in pay_docs)

        inv = Doc(
            doctype="POS Invoice",
            name=name,
            company="Hotel Co",
            currency=currency,
            pos_profile=pos_profile,
            branch=branch,
            customer="Walk-in",
            customer_name="Walk-in",
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
            restaurant_table="T1",
            room_number=room_number,
            consolidated_invoice=consolidated_invoice,
            custom_merged_pos_invoice=custom_merged_pos_invoice,
            efris_irn=efris_irn,
            efris_status=efris_status,
            total=gross,
            net_total=net_total,
            discount_amount=float(discount_amount),
            additional_discount_percentage=float(additional_discount_percentage),
            apply_discount_on=apply_discount_on,
            total_taxes_and_charges=excl_tax,
            grand_total=grand_total,
            rounded_total=grand_total,
            paid_amount=paid_amount,
            change_amount=float(change_amount),
            outstanding_amount=float(outstanding_amount),
            items=item_docs,
            taxes=tax_docs,
            payments=pay_docs,
        )
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
        fw.has_permission = lambda dt, ptype="read", doc=None, user=None: True

        def _get_meta(dt):
            m = MagicMock()
            m.has_field = lambda fn: (dt, fn) in h.custom_fields_present
            return m

        fw.get_meta = _get_meta

        def _table_exists(dt):
            return dt in (
                "POS Reason", "POS Privilege Rule", "POS Authorisation Log",
                "POS Invoice", "POS Closing Entry", "POS Closing Entry Detail",
                "POS Invoice Merge Log", "POS Invoice Reference", "E Invoice",
            )

        def _exists(dt, filt=None):
            if dt == "DocType":
                return _table_exists(filt)
            if dt == "POS Invoice":
                return filt if isinstance(filt, str) and filt in h.invoices else None
            if dt == "POS Profile":
                return filt if isinstance(filt, str) and filt in h.profiles else None
            if dt == "Mode of Payment":
                return filt if isinstance(filt, str) and filt in h.mops else None
            if dt == "Item":
                return filt if isinstance(filt, str) and filt in h.items_master else None
            return None

        def _get_value(dt, filters=None, fieldname=None, as_dict=False, **kw):
            if dt == "POS Profile" and isinstance(filters, str):
                p = h.profiles.get(filters)
                if not p:
                    return None
                return p.get(fieldname) if isinstance(fieldname, str) else p
            if dt == "Mode of Payment" and isinstance(filters, str):
                m = h.mops.get(filters)
                return m.get(fieldname) if m and isinstance(fieldname, str) else None
            if dt == "Item" and isinstance(filters, str):
                it = h.items_master.get(filters)
                if not it:
                    return None
                if isinstance(fieldname, str):
                    return it.get(fieldname)
                return Doc({f: it.get(f) for f in fieldname}) if as_dict else tuple(it.get(f) for f in fieldname)
            if dt == "Item Price" and isinstance(filters, dict):
                return h.item_prices.get((filters.get("item_code"), filters.get("price_list")))
            if dt == "E Invoice" and isinstance(filters, dict):
                return h.e_invoices.get(filters.get("invoice"))
            if dt == "POS Invoice" and isinstance(filters, str):
                inv = h.invoices.get(filters)
                if not inv:
                    return None
                return inv.get(fieldname) if isinstance(fieldname, str) else Doc({f: inv.get(f) for f in fieldname})
            return None

        def _get_all(dt, filters=None, fields=None, pluck=None, **kw):
            if dt == "POS Invoice":
                out = []
                for inv in h.invoices.values():
                    if isinstance(filters, dict):
                        ok = True
                        for k, v in filters.items():
                            if isinstance(v, list) and v[0] == "in":
                                if inv.get(k) not in v[1]:
                                    ok = False
                                    break
                            elif inv.get(k) != v:
                                ok = False
                                break
                        if not ok:
                            continue
                    if pluck:
                        out.append(inv.get(pluck))
                    else:
                        out.append(inv)
                return out
            if dt == "POS Reason":
                return [r for r in h.reasons if int(r.get("disabled") or 0) == 0]
            return []

        def _sql(query, params=None, as_dict=False):
            q = " ".join(str(query).lower().split())
            if "from `tabpos invoice`" in q and "return_against" in q:
                orig_name = params[0] if isinstance(params, (list, tuple)) else params
                matches = [
                    inv for inv in h.invoices.values()
                    if inv.get("return_against") == orig_name
                    and int(inv.get("is_return") or 0) == 1
                    and int(inv.get("docstatus") or 0) == 1
                ]
                if "for update" in q:
                    h.for_update_locks.append(("prior_returns", orig_name))
                return [{"name": m.name} for m in matches] if as_dict else [(m.name,) for m in matches]
            if "tabpos closing entry" in q:
                orig_name = params[0] if isinstance(params, (list, tuple)) else params
                rows = [
                    ce["name"] for ce in h.closing_entries
                    if ce.get("docstatus") == 1 and orig_name in ce.get("invoices", [])
                ]
                return [{"parent": r} for r in rows] if as_dict else [(r,) for r in rows]
            if "tabpos invoice merge log" in q:
                orig_name = params[0] if isinstance(params, (list, tuple)) else params
                rows = [
                    ml["name"] for ml in h.merge_logs
                    if ml.get("docstatus") == 1 and orig_name in ml.get("invoices", [])
                ]
                return [{"parent": r} for r in rows] if as_dict else [(r,) for r in rows]
            return []

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
                return _wrap_new_pos_invoice(d)
            if dt == "POS Invoice":
                if for_update:
                    h.for_update_locks.append(("POS Invoice", name))
                    if callable(h.on_lock_original):
                        h.on_lock_original(name)
                if name not in h.invoices:
                    raise fw.DoesNotExistError(name)
                return h.invoices[name]
            if dt == "POS Profile":
                if name not in h.profiles:
                    raise fw.DoesNotExistError(name)
                return h.profiles[name]
            raise fw.DoesNotExistError(f"{dt}:{name}")

        def _wrap_new_pos_invoice(d):
            d._is_new = True
            d.docstatus = 0
            d.setdefault("items", [])
            d.setdefault("taxes", [])
            d.setdefault("payments", [])

            def _insert(ignore_permissions=False):
                d.name = d.get("name") or f"POS-RET-{len(h.invoices) + 1}"
                d._is_new = False
                # Recompute totals matching ERPNext native behavior for returns
                gross = sum(float(r.qty) * float(r.rate) for r in d.items)
                disc = float(d.get("discount_amount") or 0)
                excl_tax = sum(float(t.get("tax_amount") or 0) for t in d.taxes if int(t.get("included_in_print_rate") or 0) == 0)
                d.total = gross
                d.net_total = gross - disc
                d.total_taxes_and_charges = excl_tax
                d.grand_total = d.net_total + excl_tax
                d.rounded_total = d.grand_total
                d.paid_amount = sum(float(p.amount) for p in d.payments)
                d.outstanding_amount = d.grand_total - d.paid_amount
                h.invoices[d.name] = d
                return d

            def _submit():
                d.docstatus = 1
                d.status = "Return"
                return d

            d.insert = _insert
            d.submit = _submit
            return d

        def _new_doc(dt):
            if dt == "POS Invoice":
                return _wrap_new_pos_invoice(Doc(doctype="POS Invoice"))
            raise NotImplementedError(dt)

        fw.db = SimpleNamespace(
            exists=_exists,
            table_exists=_table_exists,
            get_value=_get_value,
            set_value=lambda *a, **kw: None,
            sql=_sql,
        )
        fw.get_doc = _get_doc
        fw.get_cached_doc = _get_doc
        fw.new_doc = _new_doc
        fw.get_all = _get_all
        fw.publish_realtime = MagicMock()
        return fw


def _load_module(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestPOSReturnAndRefund(unittest.TestCase):
    def setUp(self):
        self.h = ReturnHarness()
        utils = ModuleType("frappe.utils")
        utils.cint = lambda v: int(v or 0)
        utils.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
        utils.now_datetime = lambda: datetime(2026, 11, 24, 11, 30, 0)
        utils.nowdate = lambda: "2026-11-24"
        utils.nowtime = lambda: "11:30:00"
        utils.now = lambda: "2026-11-24 11:30:00"
        utils.get_time = lambda v=None: v
        utils.get_first_day = lambda v=None: v
        utils.get_last_day = lambda v=None: v
        utils.get_datetime = lambda v=None: (
            v if isinstance(v, datetime) else datetime.strptime(str(v or "2026-11-24 11:30:00")[:19], "%Y-%m-%d %H:%M:%S")
        )
        utils.getdate = lambda v=None: datetime.strptime(str(v or "2026-11-24")[:10], "%Y-%m-%d").date()

        pos_api = ModuleType("ury.ury_pos.api")
        pos_api.getBranch = lambda: self.h.user_branch.get(self.h.frappe.session.user, "Branch A")
        ury_order_stub = ModuleType("ury.ury.doctype.ury_order.ury_order")
        ury_order_stub.release_merge_cluster_tables = MagicMock()
        sdp_stub = ModuleType("ury.ury.doctype.staff_discount_policy.staff_discount_policy")
        sdp_stub.get_applicable_policy = MagicMock(return_value=None)

        self.enterContext(patch.dict(sys.modules, {
            "frappe": self.h.frappe,
            "frappe.utils": utils,
            "ury.ury_pos.override": self.h.override,
            "ury.ury_pos.api": pos_api,
            "ury.ury.doctype.ury_order.ury_order": ury_order_stub,
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy": sdp_stub,
        }))
        self.mod = _load_module("ury.ury_pos.void_return_reopen", "ury/ury_pos/void_return_reopen.py")
        self.inv_hook = _load_module("ury.ury.hooks.ury_pos_invoice", "ury/ury/hooks/ury_pos_invoice.py")

    # --- 1. Core whole & partial returns + UGX proportional math (Adversarial #10) ---

    def test_adversarial_ugx_discounted_check_partial_returns_whole_shillings_exact_closure(self):
        """3 x 10,000 UGX with 5,000 flat discount (net 25,000) returned 1 by 1 -> 8,333 + 8,334 + 8,333 = 25,000."""
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "POS-UGX-DISC",
            currency="UGX",
            items=[{"name": "ROW-1", "item_code": "BURGER", "item_name": "Burger", "qty": 3, "rate": 10000.0}],
            discount_amount=5000.0,
            payments=[{"mode_of_payment": "Cash", "amount": 25000.0}],
        )

        # Return 1 of 3
        r1 = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")
        # Return 2 of 3
        r2 = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")
        # Return 3 of 3
        r3 = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")

        totals = [r1["refund_total"], r2["refund_total"], r3["refund_total"]]
        # Every refund MUST be an exact whole UGX shilling (no fractions!)
        for amt in totals:
            self.assertEqual(amt, int(amt), f"UGX refund must be whole shillings, got {amt}")
        self.assertEqual(totals, [8333.0, 8334.0, 8333.0])
        self.assertEqual(sum(totals), 25000.0)
        # Native ERPNext POS Return invariants on every created return check
        ret_doc = self.h.invoices[r1["return_invoice"]]
        self.assertEqual(ret_doc.is_return, 1)
        self.assertEqual(ret_doc.return_against, inv.name)
        self.assertEqual(ret_doc.docstatus, 1)
        self.assertIsNone(ret_doc.restaurant_table)
        self.assertEqual(ret_doc.invoice_printed, 0)
        self.assertEqual(ret_doc.items[0].qty, -1.0)
        self.assertEqual(ret_doc.payments[0].amount, -8333.0)

    def test_proportional_exclusive_tax_return_and_preview_is_read_only(self):
        """USD check: 2 x $25 = $50, $10 discount -> $40 net + 18% VAT ($7.20) = $47.20. Returning 1 -> $23.60."""
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "POS-USD-TAX",
            currency="USD",
            pos_profile="Till USD",
            items=[{"name": "ROW-1", "item_code": "STEAK", "item_name": "Steak", "qty": 2, "rate": 25.0}],
            discount_amount=10.0,
            taxes=[{"charge_type": "On Net Total", "account_head": "VAT - HC", "description": "VAT 18%", "rate": 18.0, "tax_amount": 7.20, "included_in_print_rate": 0}],
            payments=[{"mode_of_payment": "Card", "amount": 47.20}],
        )
        preview = self.mod.preview_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}])
        self.assertEqual(preview["subtotal"], 25.0)
        self.assertEqual(preview["discount_amount"], 5.0)
        self.assertEqual(preview["tax_amount"], 3.6)
        self.assertEqual(preview["refund_total"], 23.6)
        self.assertEqual(preview["default_refund_payments"], [{"mode_of_payment": "Card", "amount": 23.6}])
        # Preview must NOT create any invoice or log
        self.assertEqual(len(self.h.invoices), 1)
        self.assertEqual(self.h.auth_logs, [])

    # --- 2. Adversarial #5: Double return blocked ---

    def test_adversarial_double_return_and_duplicate_line_payload_refused(self):
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "POS-DBL",
            items=[{"name": "ROW-1", "item_code": "BURGER", "qty": 2, "rate": 10000.0}],
            payments=[{"mode_of_payment": "Cash", "amount": 20000.0}],
        )
        # Splitting same line across two payload entries summing to 3 > 2 -> refused
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-OVER-QTY"):
            self.mod.create_pos_return(
                inv.name,
                [{"item_row_name": "ROW-1", "qty": 2}, {"item_row_name": "ROW-1", "qty": 1}],
                reason="RET-QUALITY",
            )
        # First full return succeeds
        self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 2}], reason="RET-QUALITY")
        # Second return (double return!) MUST be refused
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-OVER-QTY"):
            self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")

    # --- 3. EFRIS credit note mapping ---

    def test_fiscalised_check_return_sets_efris_credit_note_reason_code(self):
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check("POS-EFRIS", efris_irn="FDN-2026-0001", efris_status="Fiscalised")
        res = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-OVERCHARGE")
        self.assertTrue(res["is_fiscalised"])
        self.assertTrue(res["efris_creditnote_reasoncode"].startswith("103:"))
        ret_doc = self.h.invoices[res["return_invoice"]]
        self.assertTrue(ret_doc.efris_creditnote_reasoncode.startswith("103:"))
        self.assertIn("Billing error", ret_doc.efris_creditnote_reason)

    # --- 4. Tender rules, Room Charge (Adversarial #7), Partially Paid (Adversarial #3) ---

    def test_refund_tender_same_by_default_and_net_of_change_amount(self):
        """Guest paid 50,000 Cash on 30,000 check with 20,000 change -> refund is 30,000 Cash; Card refused unless allowed."""
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "POS-CASH-CHG",
            payments=[{"mode_of_payment": "Cash", "amount": 50000.0}],
            change_amount=20000.0,
        )
        # Different tender without allow_different_tender=1 -> refused
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-DIFFERENT-TENDER"):
            self.mod.create_pos_return(
                inv.name,
                [{"item_row_name": "ROW-1", "qty": 3}],
                reason="RET-QUALITY",
                refund_payments=[{"mode_of_payment": "Card", "amount": 30000.0}],
            )
        # Default tender -> Cash 30,000 (net of 20,000 change!)
        res = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 3}], reason="RET-QUALITY")
        self.assertEqual(res["refund_payments"], [{"mode_of_payment": "Cash", "amount": 30000.0}])

    def test_adversarial_room_charge_return_blocks_cash_and_credits_folio_only(self):
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "POS-ROOM-1",
            room_number="304",
            payments=[{"mode_of_payment": "Room Charge", "amount": 30000.0}],
        )
        # Even with allow_different_tender=1, returning a room charge to Cash is REFUSED!
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-ROOM-CHARGE"):
            self.mod.create_pos_return(
                inv.name,
                [{"item_row_name": "ROW-1", "qty": 1}],
                reason="RET-QUALITY",
                refund_payments=[{"mode_of_payment": "Cash", "amount": 10000.0}],
                allow_different_tender=1,
            )
        # Returning to Room Charge folio succeeds and preserves room_number!
        res = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")
        self.assertEqual(res["refund_payments"], [{"mode_of_payment": "Room Charge", "amount": 10000.0}])
        ret_doc = self.h.invoices[res["return_invoice"]]
        self.assertEqual(ret_doc.room_number, "304")

    def test_adversarial_partially_paid_check_caps_refund_at_paid_amount(self):
        """30,000 check with 10,000 Cash paid + 20,000 outstanding. Full return refunds max 10,000 Cash, 20,000 credits outstanding."""
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "POS-PART-PAID",
            payments=[{"mode_of_payment": "Cash", "amount": 10000.0}],
            outstanding_amount=20000.0,
            status="Partly Paid",
        )
        # Attempting to hand back 30,000 Cash when only 10,000 was ever paid -> REFUSED!
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-OVER-PAID"):
            self.mod.create_pos_return(
                inv.name,
                [{"item_row_name": "ROW-1", "qty": 3}],
                reason="RET-QUALITY",
                refund_payments=[{"mode_of_payment": "Cash", "amount": 30000.0}],
            )
        # Default return caps Cash refund at 10,000 and credits remaining 20,000 against outstanding!
        res = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 3}], reason="RET-QUALITY")
        self.assertEqual(res["refund_total"], 30000.0)
        self.assertEqual(res["refund_payments"], [{"mode_of_payment": "Cash", "amount": 10000.0}])
        self.assertEqual(res["unpaid_credit_reduction"], 20000.0)

    # --- 5. Four-eyes, Own Check, Another Server's Check (Adversarial #9), Other Outlet (#8) ---

    def test_four_eyes_never_return_own_check_alone_and_approver_cannot_be_check_actor(self):
        # manager@example.test was the cashier on this check -> CANNOT return it alone!
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_closed_check("POS-OWN-1", cashier="manager@example.test", waiter="waiter1@example.test", owner="waiter1@example.test")
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")

        # Distinct manager2@example.test approves via PIN -> succeeds and keeps manager@example.test session!
        claim = {
            "original_invoice": inv.name,
            "items": [{"item_code": "BURGER", "item_row_name": "ROW-1", "qty": 1.0}],
            "refund_total": 10000.0,
            "currency": "UGX",
            "refund_payments": [{"amount": 10000.0, "mode_of_payment": "Cash"}],
            "allow_different_tender": 0,
            "reason": "RET-QUALITY",
            "note": "",
        }
        tok = self.h.override.mint_token("manager@example.test", "manager2@example.test", "Till A", "return", inv.name, claim)
        res = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY", approval_token=tok)
        self.assertEqual(res["approver"], "manager2@example.test")
        self.assertEqual(self.h.frappe.session.user, "manager@example.test")

        # Cashier returning check where manager@example.test was the original waiter:
        # manager@example.test CANNOT approve a return on their own check!
        self.h.frappe.session.user = "cashier@example.test"
        inv2 = self.h.add_closed_check("POS-OWN-2", cashier="waiter2@example.test", waiter="manager@example.test", owner="manager@example.test")
        claim2 = {**claim, "original_invoice": inv2.name}
        tok_own_mgr = self.h.override.mint_token("cashier@example.test", "manager@example.test", "Till A", "return", inv2.name, claim2)
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-RETURN-OWN-CHECK"):
            self.mod.create_pos_return(inv2.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY", approval_token=tok_own_mgr)

    def test_adversarial_other_outlet_and_another_servers_check(self):
        inv = self.h.add_closed_check("POS-BR-A", branch="Branch A", waiter="waiter1@example.test", cashier="waiter2@example.test", owner="waiter1@example.test")
        # Other outlet's manager -> REFUSED with POS-OUTLET-MISMATCH
        self.h.frappe.session.user = "other_branch_mgr@example.test"
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-OUTLET-MISMATCH"):
            self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")

        # Another server (cashier) on same outlet without PIN -> REFUSED with POS-AUTH-REQUIRED
        self.h.frappe.session.user = "cashier@example.test"
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")

        # Independent manager on same outlet -> allowed alone and logged!
        self.h.frappe.session.user = "manager2@example.test"
        res = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")
        self.assertEqual(res["approver"], "manager2@example.test")
        self.assertTrue(any(l.get("privilege") == "return" and l.get("outcome") == "Consumed" for l in self.h.auth_logs))

    # --- 6. Unreferenced return: dedicated privilege only (default deny!) ---

    def test_unreferenced_return_default_denied_and_allowed_only_with_dedicated_privilege(self):
        # Even URY Manager with `return` is REFUSED when custom_unreferenced_return_roles is empty!
        self.h.frappe.session.user = "manager@example.test"
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-RETURN-UNREFERENCED-NOT-ALLOWED"):
            self.mod.create_unreferenced_return(
                "Till A",
                "Walk-in",
                [{"item_code": "BURGER", "qty": 1}],
                [{"mode_of_payment": "Cash", "amount": 10000.0}],
                reason="RET-QUALITY",
            )

        # Enable unreferenced return ONLY for `URY Admin` on Till A
        self.h.profiles["Till A"].custom_unreferenced_return_roles = "URY Admin"

        # Plain URY Manager cannot perform alone (forbid_solo) and cannot approve either!
        self.h.frappe.session.user = "cashier@example.test"
        claim = {
            "unreferenced": 1,
            "pos_profile": "Till A",
            "customer": "Walk-in",
            "items": [{"item_code": "BURGER", "qty": 1.0, "rate": 10000.0}],
            "refund_total": 10000.0,
            "currency": "UGX",
            "refund_payments": [{"amount": 10000.0, "mode_of_payment": "Cash"}],
            "reason": "RET-QUALITY",
            "note": "",
        }
        tok_mgr = self.h.override.mint_token("cashier@example.test", "manager@example.test", "Till A", "return", "Till A", claim)
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-RETURN-UNREFERENCED-NOT-ALLOWED"):
            self.mod.create_unreferenced_return(
                "Till A", "Walk-in", [{"item_code": "BURGER", "qty": 1}],
                [{"mode_of_payment": "Cash", "amount": 10000.0}],
                reason="RET-QUALITY", approval_token=tok_mgr,
            )

        # Distinct approver with `URY Admin` role succeeds!
        tok_gm = self.h.override.mint_token("cashier@example.test", "gm@example.test", "Till A", "return", "Till A", claim)
        res = self.mod.create_unreferenced_return(
            "Till A", "Walk-in", [{"item_code": "BURGER", "qty": 1}],
            [{"mode_of_payment": "Cash", "amount": 10000.0}],
            reason="RET-QUALITY", approval_token=tok_gm,
        )
        ret_doc = self.h.invoices[res["return_invoice"]]
        self.assertEqual(ret_doc.is_return, 1)
        self.assertIsNone(ret_doc.return_against)
        self.assertEqual(ret_doc.grand_total, -10000.0)
        self.assertEqual(res["approver"], "gm@example.test")

    # --- 7. Adversarial #2 (Split & Merged), #4 (Offline), #6 (Day Close), #11 (Concurrent Consolidation) ---

    def test_adversarial_split_and_merged_checks(self):
        self.h.frappe.session.user = "manager2@example.test"
        # Split checks: Split A and Split B are independent closed checks
        split_a = self.h.add_closed_check("POS-SPLIT-A", items=[{"name": "SA-1", "item_code": "BURGER", "qty": 1, "rate": 10000.0}], payments=[{"mode_of_payment": "Cash", "amount": 10000.0}])
        split_b = self.h.add_closed_check("POS-SPLIT-B", items=[{"name": "SB-1", "item_code": "BEER", "qty": 1, "rate": 15000.0}], payments=[{"mode_of_payment": "Cash", "amount": 15000.0}])
        res_a = self.mod.create_pos_return(split_a.name, [{"item_row_name": "SA-1", "qty": 1}], reason="RET-QUALITY")
        self.assertEqual(res_a["refund_total"], 10000.0)
        # Split B remains completely returnable
        info_b = self.mod.get_returnable_check(split_b.name)
        self.assertEqual(info_b["items"][0]["remaining_qty"], 1.0)

        # Legacy linked merged check (`custom_merged_pos_invoice` set) -> REFUSED
        legacy_merged = self.h.add_closed_check("POS-LEGACY-MERGED", custom_merged_pos_invoice="POS-PARENT-9")
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-MERGED"):
            self.mod.create_pos_return(legacy_merged.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")

        # #143 Add Check consolidated single-invoice merge (`custom_merged_pos_invoice` is None) -> returns cleanly!
        add_check_merged = self.h.add_closed_check(
            "POS-ADD-CHECK-MERGED",
            items=[
                {"name": "M-1", "item_code": "BURGER", "qty": 2, "rate": 10000.0},
                {"name": "M-2", "item_code": "BEER", "qty": 1, "rate": 15000.0},
            ],
            payments=[{"mode_of_payment": "Cash", "amount": 35000.0}],
        )
        res_m = self.mod.create_pos_return(add_check_merged.name, [{"item_row_name": "M-2", "qty": 1}], reason="RET-QUALITY")
        self.assertEqual(res_m["refund_total"], 15000.0)

    def test_adversarial_return_after_day_close_and_concurrent_consolidation_and_offline(self):
        self.h.frappe.session.user = "manager2@example.test"

        # Offline replay refused
        inv_off = self.h.add_closed_check("POS-OFF-1")
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-OFFLINE-NOT-ALLOWED"):
            self.mod.create_pos_return(inv_off.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY", offline_captured=1)

        # Return after day close (consolidated_invoice set) -> REFUSED
        inv_closed = self.h.add_closed_check("POS-DAY-CLOSED", status="Consolidated", consolidated_invoice="SINV-2026-001")
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-CONSOLIDATED"):
            self.mod.create_pos_return(inv_closed.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")

        # Return after POS Closing Entry submitted (even before consolidated_invoice field backfilled) -> REFUSED
        inv_pce = self.h.add_closed_check("POS-PCE-1")
        self.h.closing_entries.append({"name": "PCE-1", "docstatus": 1, "invoices": [inv_pce.name]})
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-AFTER-DAY-CLOSE"):
            self.mod.create_pos_return(inv_pce.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")

        # Concurrent return and consolidation: consolidation lands while acquiring FOR UPDATE lock -> REFUSED!
        inv_race = self.h.add_closed_check("POS-RACE-1")

        def _simulate_concurrent_merge(locked_name):
            if locked_name == inv_race.name:
                self.h.merge_logs.append({"name": "PIML-RACE", "docstatus": 1, "invoices": [inv_race.name]})

        self.h.on_lock_original = _simulate_concurrent_merge
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-CONSOLIDATED"):
            self.mod.create_pos_return(inv_race.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")
        self.assertIn(("POS Invoice", inv_race.name), self.h.for_update_locks)

    # --- 8. Mutation-level REST / Desk bypass blocked on return POS Invoices ---

    def test_validate_invoice_hook_blocks_direct_rest_over_return_and_unreferenced_return(self):
        self.h.frappe.session.user = "cashier@example.test"
        orig = self.h.add_closed_check("POS-HOOK-1", items=[{"name": "ROW-1", "item_code": "BURGER", "qty": 1, "rate": 10000.0}])

        # Direct REST over-return (qty -2 against sold 1) -> REFUSED
        bad_over = Doc(
            doctype="POS Invoice", _is_new=True, is_return=1, return_against=orig.name,
            pos_profile="Till A", company="Hotel Co", restaurant_table="T1", invoice_printed=1,
            waiter="waiter1@example.test", modified_by="cashier@example.test",
            items=[Doc(item_code="BURGER", pos_invoice_item="ROW-1", qty=-2.0, rate=10000.0)],
        )
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-RETURN-OVER-QTY"):
            self.inv_hook.validate_invoice(bad_over, "validate")

        # Direct REST unreferenced return when disabled -> REFUSED
        bad_unref = Doc(
            doctype="POS Invoice", _is_new=True, is_return=1, return_against=None,
            pos_profile="Till A", company="Hotel Co", restaurant_table="T1", invoice_printed=1,
            waiter="waiter1@example.test", modified_by="cashier@example.test",
            items=[Doc(item_code="BURGER", qty=-1.0, rate=10000.0)],
        )
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-RETURN-UNREFERENCED-NOT-ALLOWED"):
            self.inv_hook.validate_invoice(bad_unref, "validate")

    def test_three_decimal_currency_kwd_proportional_return_closure(self):
        """0/2/3dp currency precision: KWD (3dp) discounted check returned 1-by-1 closes to exact 3.250."""
        self.h.profiles["Till KWD"] = Doc(
            name="Till KWD",
            company="Hotel Co",
            currency="KWD",
            branch="Branch A",
            warehouse="Kitchen - HC",
            cost_center="Restaurant - HC",
            selling_price_list="KWD Selling",
            custom_unreferenced_return_roles="",
            payments=[Doc(mode_of_payment="Cash", default=1)],
        )
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "POS-KWD-1",
            pos_profile="Till KWD",
            currency="KWD",
            discount_amount=0.500,
            items=[{"name": "KWD-ROW-1", "item_code": "BURGER", "item_name": "Burger", "qty": 3, "rate": 1.250}],
            payments=[{"mode_of_payment": "Cash", "amount": 3.250}],
        )
        prev = self.mod.preview_pos_return(inv.name, [{"item_row_name": "KWD-ROW-1", "qty": 1}])
        self.assertEqual(prev["precision"], 3)
        self.assertEqual(prev["refund_total"], 1.083)

        r1 = self.mod.create_pos_return(inv.name, [{"item_row_name": "KWD-ROW-1", "qty": 1}], reason="RET-QUALITY")
        r2 = self.mod.create_pos_return(inv.name, [{"item_row_name": "KWD-ROW-1", "qty": 1}], reason="RET-QUALITY")
        r3 = self.mod.create_pos_return(inv.name, [{"item_row_name": "KWD-ROW-1", "qty": 1}], reason="RET-QUALITY")
        self.assertEqual([r1["refund_total"], r2["refund_total"], r3["refund_total"]], [1.083, 1.084, 1.083])
        self.assertAlmostEqual(r1["refund_total"] + r2["refund_total"] + r3["refund_total"], 3.250, places=3)

    def test_ugx_zero_decimal_precision_overrides_legacy_currency_fraction_units_100(self):
        """Frappe's shipped UGX Currency fixture carries fraction_units=100; ISO-4217 UGX MUST stay 0dp."""
        orig_get_value = self.h.frappe.db.get_value

        def _get_value_with_ugx_100(dt, filt=None, fieldname=None, *a, **kw):
            if dt == "Currency" and filt == "UGX" and fieldname == "fraction_units":
                return 100
            return orig_get_value(dt, filt, fieldname, *a, **kw)

        self.h.frappe.db.get_value = _get_value_with_ugx_100
        self.assertEqual(self.mod._currency_precision("UGX"), 0)

    # --- 9. Hydrogen R1 / R2 regressions (Findings 1, 2, 3, 5, 6a) ---

    def test_r2_p0_direct_rest_return_by_second_cashier_without_approval_is_refused(self):
        """Finding 1 (P0 Security): second URY Cashier NOT in check_actors cannot bypass via direct REST return."""
        self.h.user_roles["cashier2@example.test"] = ["URY Cashier"]
        self.h.user_branch["cashier2@example.test"] = "Branch A"
        orig = self.h.add_closed_check(
            "POS-REST-1",
            owner="waiter1@example.test",
            cashier="cashier@example.test",
            waiter="waiter1@example.test",
            items=[{"name": "ROW-1", "item_code": "BURGER", "qty": 2, "rate": 10000.0}],
            payments=[{"mode_of_payment": "Cash", "amount": 20000.0}],
        )

        # Direct REST POST by cashier2 (not check actor, no manager role, no approval_token, even spoofing approver) -> REFUSED!
        self.h.frappe.session.user = "cashier2@example.test"
        rest_ret = Doc(
            doctype="POS Invoice", _is_new=True, is_return=1, return_against=orig.name,
            pos_profile="Till A", company="Hotel Co", branch="Branch A",
            restaurant_table=None, invoice_printed=0,
            waiter="cashier2@example.test", modified_by="cashier2@example.test",
            custom_return_approver="manager2@example.test",
            cancel_reason="RET-QUALITY",
            grand_total=-10000.0, rounded_total=-10000.0,
            items=[Doc(item_code="BURGER", pos_invoice_item="ROW-1", qty=-1.0, rate=10000.0)],
            payments=[Doc(mode_of_payment="Cash", amount=-10000.0)],
        )
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.inv_hook.validate_invoice(rest_ret, "validate")
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.inv_hook.before_submit(rest_ret, "before_submit")

        # Independent URY Manager (has return privilege) without reason -> REFUSED [POS-AUTH-REASON]!
        self.h.frappe.session.user = "manager2@example.test"
        mgr_no_reason = Doc(
            doctype="POS Invoice", _is_new=True, is_return=1, return_against=orig.name,
            pos_profile="Till A", company="Hotel Co", branch="Branch A",
            restaurant_table=None, invoice_printed=0,
            waiter="manager2@example.test", modified_by="manager2@example.test",
            cancel_reason="",
            grand_total=-10000.0, rounded_total=-10000.0,
            items=[Doc(item_code="BURGER", pos_invoice_item="ROW-1", qty=-1.0, rate=10000.0)],
            payments=[Doc(mode_of_payment="Cash", amount=-10000.0)],
        )
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-AUTH-REASON"):
            self.inv_hook.validate_invoice(mgr_no_reason, "validate")

        # Independent URY Manager WITH valid reason -> allowed, server forces custom_return_approver=actor & logs on submit!
        mgr_ok = Doc(
            doctype="POS Invoice", name="POS-RET-DESK-1", _is_new=False, is_return=1, return_against=orig.name,
            pos_profile="Till A", company="Hotel Co", branch="Branch A",
            restaurant_table=None, invoice_printed=0, order_type="Dine In", no_of_pax=0,
            cashier="manager2@example.test", waiter="manager2@example.test", modified_by="manager2@example.test",
            custom_return_approver="spoofed@example.test",
            cancel_reason="RET-QUALITY",
            grand_total=-10000.0, rounded_total=-10000.0,
            items=[Doc(item_code="BURGER", pos_invoice_item="ROW-1", qty=-1.0, rate=10000.0, owner="manager2@example.test")],
            payments=[Doc(mode_of_payment="Cash", amount=-10000.0)],
        )
        self.inv_hook.validate_invoice(mgr_ok, "validate")
        self.assertEqual(mgr_ok.custom_return_approver, "manager2@example.test")
        logs_before = len(self.h.auth_logs)
        self.inv_hook.before_submit(mgr_ok, "before_submit")
        self.assertEqual(len(self.h.auth_logs), logs_before + 1)
        self.assertEqual(self.h.auth_logs[-1].approver, "manager2@example.test")

    def test_r2_p0_efris_credit_note_reasoncode_matches_uganda_compliance_and_dynamic_meta(self):
        """Finding 2 (P0 Runtime): exact uganda_compliance options (102 trailing '.', 103 'Stated' & ',etc.') + dynamic meta lookup."""
        self.assertEqual(
            self.mod.EFRIS_REASON_MAP["REOPEN-TENDER"],
            "102:Cancellation of the purchase.",
        )
        self.assertEqual(
            self.mod.EFRIS_REASON_MAP["REOPEN-SPLIT"],
            "102:Cancellation of the purchase.",
        )
        self.assertEqual(
            self.mod.DEFAULT_EFRIS_CREDIT_REASON,
            "102:Cancellation of the purchase.",
        )
        self.assertEqual(
            self.mod.EFRIS_REASON_MAP["RET-OVERCHARGE"],
            "103:Invoice amount wrongly Stated due to miscalculation of price, tax, or discounts,etc.",
        )

        # Dynamic resolution from POS Invoice meta Select options must follow live options by numeric prefix.
        orig_get_meta = self.h.frappe.get_meta

        def _meta_with_efris_opts(dt):
            m = orig_get_meta(dt)
            if dt == "POS Invoice":
                m.get_field = lambda fn: (
                    SimpleNamespace(
                        options=(
                            "\n101:Custom 101 Option."
                            "\n102:Custom 102 Cancellation."
                            "\n103:Custom 103 Miscalculation."
                        )
                    )
                    if fn == "efris_creditnote_reasoncode"
                    else None
                )
            return m

        self.h.frappe.get_meta = _meta_with_efris_opts
        self.assertEqual(self.mod._resolve_efris_credit_reason("RET-OVERCHARGE"), "103:Custom 103 Miscalculation.")
        self.assertEqual(self.mod._resolve_efris_credit_reason("REOPEN-TENDER"), "102:Custom 102 Cancellation.")
        self.assertEqual(self.mod._resolve_efris_credit_reason("RET-QUALITY"), "101:Custom 101 Option.")

    def test_r2_p1_tax_inclusive_vat_preserved_on_credit_notes_and_tips_payable_excluded(self):
        """Finding 3 (P1 Tax Accounting): inclusive VAT rows (included_in_print_rate=1) are preserved on returns."""
        self.h.frappe.session.user = "manager2@example.test"
        self.h.profiles["Till A"].custom_tips_payable_account = "Tips Payable - HC"
        inv = self.h.add_closed_check(
            "POS-VAT-INCL",
            currency="UGX",
            items=[{"name": "ROW-1", "item_code": "BURGER", "item_name": "Burger", "qty": 2, "rate": 10000.0}],
            taxes=[
                {
                    "charge_type": "On Net Total",
                    "account_head": "VAT - HC",
                    "description": "VAT 18% Inclusive",
                    "rate": 18.0,
                    "tax_amount": 3050.85,
                    "included_in_print_rate": 1,
                    "cost_center": "Restaurant - HC",
                },
                {
                    "charge_type": "Actual",
                    "account_head": "Tips Payable - HC",
                    "description": "Charged Tip",
                    "rate": 0.0,
                    "tax_amount": 5000.0,
                    "included_in_print_rate": 0,
                    "cost_center": "Restaurant - HC",
                },
            ],
            payments=[{"mode_of_payment": "Cash", "amount": 25000.0}],
        )
        inv.custom_charged_tip = 5000.0
        inv.net_total = 16949.15
        inv.total_taxes_and_charges = 8050.85
        inv.grand_total = 25000.0
        inv.rounded_total = 25000.0

        res = self.mod.create_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}], reason="RET-QUALITY")
        # Inclusive VAT is inside the 10,000 item price, and server tip is excluded from partial item return.
        self.assertEqual(res["refund_total"], 10000.0)
        ret_doc = self.h.invoices[res["return_invoice"]]
        incl_rows = [t for t in ret_doc.taxes if int(t.get("included_in_print_rate") or 0) == 1]
        self.assertEqual(len(incl_rows), 1, "Tax-inclusive VAT row MUST be preserved on credit note")
        self.assertEqual(incl_rows[0].account_head, "VAT - HC")
        self.assertEqual(incl_rows[0].rate, 18.0)
        self.assertLess(float(incl_rows[0].tax_amount), 0.0)
        # Tips Payable row must NOT be copied onto a partial item return, and custom_charged_tip explicitly 0
        tip_rows = [t for t in ret_doc.taxes if t.account_head == "Tips Payable - HC"]
        self.assertEqual(tip_rows, [])
        self.assertEqual(getattr(ret_doc.flags, "ury_return_discount_computed", False), True)

    def test_r2_p1_kwd_partial_returns_reconcile_with_2dp_field_precision_zero_over_refund(self):
        """Finding 5 (P1 Multi-Currency): 3-way partial KWD return on 2dp site never over-refunds 2.500 -> 2.507."""
        self.h.profiles["Till KWD"] = Doc(
            name="Till KWD",
            company="Hotel Co",
            currency="KWD",
            branch="Branch A",
            warehouse="Kitchen - HC",
            cost_center="Restaurant - HC",
            selling_price_list="KWD Selling",
            custom_unreferenced_return_roles="",
            payments=[Doc(mode_of_payment="Cash", default=1)],
        )
        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "R1KWD00001",
            pos_profile="Till KWD",
            currency="KWD",
            discount_amount=0.500,
            items=[{"name": "KWD-R1", "item_code": "BURGER", "item_name": "Burger", "qty": 3, "rate": 1.000}],
            payments=[{"mode_of_payment": "Cash", "amount": 2.500}],
        )
        # Simulate Frappe default 2dp POS Invoice.grand_total field precision (as reproduced live in Hydrogen R1).
        inv.precision = lambda fieldname=None: 2

        # Wrap new_doc so returned POS Invoices also round grand_total/paid_amount at 2dp just like live ERPNext!
        orig_new_doc = self.h.frappe.new_doc

        def _new_doc_2dp(dt):
            d = orig_new_doc(dt)
            d.precision = lambda fieldname=None: 2
            orig_ins = d.insert

            def _ins_2dp(ignore_permissions=False):
                orig_ins(ignore_permissions=ignore_permissions)
                d.grand_total = round(float(d.grand_total), 2)
                d.rounded_total = round(float(d.rounded_total), 2)
                d.paid_amount = round(float(d.paid_amount), 2)
                return d

            d.insert = _ins_2dp
            return d

        self.h.frappe.new_doc = _new_doc_2dp

        self.assertEqual(self.mod._currency_precision("KWD", doc=inv), 2)
        r1 = self.mod.create_pos_return(inv.name, [{"item_row_name": "KWD-R1", "qty": 1}], reason="RET-QUALITY")
        r2 = self.mod.create_pos_return(inv.name, [{"item_row_name": "KWD-R1", "qty": 1}], reason="RET-QUALITY")
        r3 = self.mod.create_pos_return(inv.name, [{"item_row_name": "KWD-R1", "qty": 1}], reason="RET-QUALITY")

        planned_sum = round(r1["refund_total"] + r2["refund_total"] + r3["refund_total"], 3)
        self.assertEqual(planned_sum, 2.500, f"Planned refunds must equal 2.500 exact, got {planned_sum}")
        for r in (r1, r2, r3):
            ret_doc = self.h.invoices[r["return_invoice"]]
            self.assertEqual(
                round(float(ret_doc.grand_total), 3),
                round(float(ret_doc.paid_amount), 3),
                f"grand_total ({ret_doc.grand_total}) must equal paid_amount ({ret_doc.paid_amount})",
            )

    def test_r2_p2_showroom_cash_not_misclassified_as_room_charge(self):
        """Finding 6a (P2 Mode Detection): 'Showroom Cash' must NOT be treated as a Room Charge."""
        self.h.mops["Showroom Cash"] = Doc(name="Showroom Cash", type="Cash")
        self.h.mops["Showroom Voucher"] = Doc(name="Showroom Voucher", type="General")
        self.h.mops["R1 Room Charge"] = Doc(name="R1 Room Charge", type="General")
        self.h.profiles["Till A"].payments.append(Doc(mode_of_payment="Showroom Cash", default=0))

        self.assertFalse(self.mod._is_room_charge_mode("Showroom Cash"))
        self.assertFalse(self.mod._is_room_charge_mode("Showroom Voucher"))
        self.assertTrue(self.mod._is_room_charge_mode("Room Charge"))
        self.assertTrue(self.mod._is_room_charge_mode("R1 Room Charge"))

        self.h.frappe.session.user = "manager2@example.test"
        inv = self.h.add_closed_check(
            "POS-SHOWROOM-1",
            payments=[{"mode_of_payment": "Showroom Cash", "amount": 30000.0}],
        )
        prev = self.mod.preview_pos_return(inv.name, [{"item_row_name": "ROW-1", "qty": 1}])
        self.assertFalse(prev["is_room_charge"])
        res = self.mod.create_pos_return(
            inv.name,
            [{"item_row_name": "ROW-1", "qty": 1}],
            reason="RET-QUALITY",
            refund_payments=[{"mode_of_payment": "Showroom Cash", "amount": 10000.0}],
        )
        self.assertFalse(res["is_room_charge"])
        self.assertEqual(res["refund_payments"], [{"mode_of_payment": "Showroom Cash", "amount": 10000.0}])


if __name__ == "__main__":
    unittest.main()
