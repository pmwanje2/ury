"""RED/GREEN unit and adversarial tests for Scope e pricing privileged actions.

Covers Oracle Simphony / SAP POS parity for:
- Matrix row 11: Price override (reason, limit, PIN above limit, native allow_rate_change)
- Matrix row 12: Open item (name/reference, HALO, min price) and zero-price item
- Matrix row 13: Complimentary / house / non-revenue check or item (full comp = 2nd person)
- Matrix row 14: Service charge auto-apply, waive, and change
- Matrix row 15: Tax exempt per check or item (native Tax Category/Tax Rule, certificate, EFRIS)
- Matrix row 30: Post-close tip adjustment (on #145 tip model: Tips Payable Liability JE)

Adversarial coverage:
- Rate below cost or zero
- Negative rate / negative service charge / negative tip
- Price override -> bill discount -> return
- EFRIS-fiscalised check protection
- Comp of a room charge (both directions)
- Tax-exempt customer changed after exempting
- Direct REST/Desk mutation bypass in POS Invoice validate hook
- Offline replay refusal of every privileged action
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]


class FakePermissionError(Exception):
    pass


class FakeValidationError(Exception):
    pass


class FakeDuplicateEntryError(Exception):
    pass


class FakeDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value

    def get(self, key, default=None):
        return super().get(key, default)


class FakeDoc(FakeDict):
    def __init__(self, data=None, *, is_new=False, before_save=None):
        super().__init__(data or {})
        self._is_new = is_new
        self._before_save = before_save
        self.flags = FakeDict()
        self.saved = False
        self.inserted = False
        self.submitted = False
        self._db_sets = []

    def is_new(self):
        return self._is_new

    def get_doc_before_save(self):
        return self._before_save

    def precision(self, _field=None):
        return 2

    def check_permission(self, _perm=None):
        return True

    def append(self, field, row):
        self.setdefault(field, [])
        row_obj = row if isinstance(row, FakeDict) else FakeDict(row)
        row_obj.setdefault("idx", len(self[field]) + 1)
        row_obj.setdefault("name", f"ROW-{len(self[field]) + 1}")
        self[field].append(row_obj)
        return row_obj

    def set(self, field, rows):
        self[field] = [r if isinstance(r, FakeDict) else FakeDict(r) for r in (rows or [])]

    def calculate_taxes_and_totals(self):
        items = self.get("items") or []
        net = 0.0
        for row in items:
            qty = float(row.get("qty") or 0)
            rate = float(row.get("rate") or 0)
            disc_pct = float(row.get("discount_percentage") or 0)
            # Mirror ERPNext: if discount_percentage set and price_list_rate > 0, apply on price_list_rate
            plr = float(row.get("price_list_rate") if row.get("price_list_rate") is not None else rate)
            if disc_pct > 0 and plr > 0:
                eff_rate = round(plr * (1.0 - disc_pct / 100.0), 2)
                row["rate"] = eff_rate
            else:
                eff_rate = rate
            amt = round(qty * eff_rate, 2)
            row["amount"] = amt
            row["net_amount"] = amt
            net += amt
        self["total"] = round(net, 2)
        self["net_total"] = round(net, 2)

        taxes = self.get("taxes") or []
        running_total = net
        tax_total = 0.0
        for row in taxes:
            ct = row.get("charge_type") or "On Net Total"
            if ct == "Actual":
                t_amt = round(float(row.get("tax_amount") or 0), 2)
            elif ct == "On Previous Row Total":
                t_amt = round(running_total * float(row.get("rate") or 0) / 100.0, 2)
            else:
                # Per-item tax template override if present on rows
                if any(it.get("item_tax_template") for it in items):
                    t_amt = 0.0
                    acct = row.get("account_head")
                    for it in items:
                        it_rate = float(row.get("rate") or 0)
                        raw_map = it.get("item_tax_rate")
                        if raw_map:
                            parsed = json.loads(raw_map) if isinstance(raw_map, str) else raw_map
                            if acct in parsed:
                                it_rate = float(parsed[acct])
                        t_amt += round(float(it.get("net_amount") or 0) * it_rate / 100.0, 2)
                    t_amt = round(t_amt, 2)
                else:
                    t_amt = round(net * float(row.get("rate") or 0) / 100.0, 2)
            row["tax_amount"] = t_amt
            running_total = round(running_total + t_amt, 2)
            row["total"] = running_total
            tax_total = round(tax_total + t_amt, 2)

        gross = round(net + tax_total, 2)
        add_pct = float(self.get("additional_discount_percentage") or 0)
        disc_amt = float(self.get("discount_amount") or 0)
        if add_pct > 0:
            disc_amt = round(gross * add_pct / 100.0, 2)
            self["discount_amount"] = disc_amt
        elif disc_amt > 0:
            self["discount_amount"] = round(disc_amt, 2)
        else:
            self["discount_amount"] = 0.0

        self["total_taxes_and_charges"] = tax_total
        self["grand_total"] = round(gross - self["discount_amount"], 2)
        self["rounded_total"] = self["grand_total"]
        paid = sum(float(p.get("amount") or 0) for p in (self.get("payments") or []))
        self["paid_amount"] = round(paid, 2)
        self["outstanding_amount"] = round(max(0.0, self["grand_total"] - paid), 2)

    def save(self, ignore_permissions=False):
        self.saved = True
        return self

    def insert(self, ignore_permissions=False):
        self.inserted = True
        self._is_new = False
        return self

    def submit(self):
        self.submitted = True
        self["docstatus"] = 1
        return self

    def db_set(self, field, value=None, update_modified=True):
        if isinstance(field, dict):
            for k, v in field.items():
                self[k] = v
                self._db_sets.append((k, v))
        else:
            self[field] = value
            self._db_sets.append((field, value))


class FakeFrappeHarness:
    """Self-contained fake Frappe runtime for site-less unit and adversarial tests."""

    def __init__(self):
        self.docs: dict[tuple[str, str], FakeDoc] = {}
        self.rules: list[dict] = []
        self.reasons: list[dict] = []
        self.logs: list[FakeDoc] = []
        self.journal_entries: list[FakeDoc] = []
        self.tax_rules: list[dict] = []
        self.tax_templates: dict[str, list[dict]] = {}
        self.item_tax_templates: dict[str, dict] = {}
        self.tax_categories: dict[str, dict] = {}
        self.e_invoices: dict[str, dict] = {}
        self.bins: dict[tuple[str, str], float] = {}
        self.item_prices: dict[tuple[str, str], float] = {}
        self.mop_accounts: dict[tuple[str, str], str] = {}
        self.accounts: dict[str, dict] = {}
        self.user_roles: dict[str, list[str]] = {
            "cashier@example.com": ["URY Cashier"],
            "waiter@example.com": ["URY Captain"],
            "manager@example.com": ["URY Manager"],
            "manager2@example.com": ["URY Manager"],
            "admin@example.com": ["URY Admin"],
        }
        self.user_enabled: dict[str, int] = {u: 1 for u in self.user_roles}
        self.tokens: dict[str, dict] = {}
        self.consumed_tokens: set[str] = set()
        self.branch_by_user: dict[str, str] = {u: "Main Branch" for u in self.user_roles}
        self.company_guest_clearing: dict[str, str] = {"Test Co": "Guest Ledger Clearing - TC"}
        self.frappe = self._build_frappe()

    def mint_token(
        self,
        *,
        approver: str,
        privilege: str,
        pos_profile: str,
        reference_name: str,
        claim: dict,
        requester: str | None = None,
    ) -> str:
        raw = f"tok-{approver}-{privilege}-{len(self.tokens) + 1}"
        req = requester or self.frappe.session.user
        self.tokens[raw] = {
            "requester": req,
            "approver": approver,
            "privilege": privilege,
            "pos_profile": pos_profile,
            "reference_name": reference_name,
            "claim": claim,
        }
        return raw

    def _build_frappe(self):
        harness = self
        frappe_mod = types.ModuleType("frappe")
        frappe_mod.PermissionError = FakePermissionError
        frappe_mod.ValidationError = FakeValidationError
        frappe_mod.DuplicateEntryError = FakeDuplicateEntryError
        frappe_mod.UniqueValidationError = FakeDuplicateEntryError
        frappe_mod.DoesNotExistError = KeyError
        frappe_mod._dict = FakeDict
        frappe_mod._ = lambda text: text
        frappe_mod.session = FakeDict(user="cashier@example.com")
        frappe_mod.flags = FakeDict()
        frappe_mod.form_dict = {}
        frappe_mod.request = None
        frappe_mod.STANDARD_USERS = ("Guest", "Administrator")

        def throw(msg, exc=None, title=None):
            raise (exc or FakeValidationError)(msg)

        frappe_mod.throw = throw
        frappe_mod.whitelist = lambda *args, **kwargs: (
            args[0] if args and callable(args[0]) else (lambda fn: fn)
        )
        frappe_mod.parse_json = lambda val: json.loads(val) if isinstance(val, str) else val
        frappe_mod.get_roles = lambda user=None: list(
            harness.user_roles.get(user or frappe_mod.session.user, [])
        )
        frappe_mod.has_permission = lambda doctype, perm="read", doc=None, user=None: True
        frappe_mod.log_error = lambda *a, **k: None
        frappe_mod.publish_realtime = lambda *a, **k: None
        frappe_mod.get_hooks = lambda *a, **k: []

        def get_doc(arg1, arg2=None, for_update=False):
            if isinstance(arg1, dict):
                dt = arg1.get("doctype")
                doc = FakeDoc(arg1, is_new=True)
                if dt == "POS Authorisation Log":
                    orig_insert = doc.insert

                    def _insert(ignore_permissions=False):
                        harness.logs.append(doc)
                        return orig_insert(ignore_permissions=ignore_permissions)

                    doc.insert = _insert
                elif dt == "Journal Entry":
                    doc["name"] = f"JV-TEST-{len(harness.journal_entries) + 1}"
                    orig_submit = doc.submit

                    def _submit():
                        harness.journal_entries.append(doc)
                        return orig_submit()

                    doc.submit = _submit
                return doc
            key = (arg1, arg2)
            if key not in harness.docs:
                raise KeyError(f"{arg1} {arg2} not found")
            return harness.docs[key]

        frappe_mod.get_doc = get_doc
        frappe_mod.get_cached_doc = lambda dt, name: get_doc(dt, name)
        frappe_mod.new_doc = lambda dt: get_doc({"doctype": dt})

        def get_meta(dt):
            meta = FakeDict()
            meta.has_field = lambda fn: True
            return meta

        frappe_mod.get_meta = get_meta

        def db_exists(dt, filt=None):
            if dt == "POS Privilege Rule":
                if isinstance(filt, dict):
                    return any(
                        all(r.get(k) == v for k, v in filt.items())
                        for r in harness.rules
                    )
                return bool(harness.rules)
            if dt == "POS Reason":
                if isinstance(filt, str):
                    return any(r.get("code") == filt or r.get("name") == filt for r in harness.reasons)
                return bool(harness.reasons)
            if dt == "Tax Category":
                return filt in harness.tax_categories
            if dt == "Item Tax Template":
                return filt in harness.item_tax_templates
            if dt == "Sales Taxes and Charges Template":
                return filt in harness.tax_templates
            if dt == "Account":
                return filt in harness.accounts
            if isinstance(filt, str):
                return (dt, filt) in harness.docs
            return False

        def db_get_value(dt, filt, fieldname=None, as_dict=False, order_by=None, for_update=False):
            if dt == "User" and isinstance(filt, str):
                if fieldname == "enabled":
                    return harness.user_enabled.get(filt, 0)
            if dt == "Company" and fieldname == "custom_guest_ledger_clearing":
                return harness.company_guest_clearing.get(filt)
            if dt == "E Invoice":
                inv = filt.get("invoice") if isinstance(filt, dict) else filt
                rec = harness.e_invoices.get(inv)
                if not rec:
                    return None
                if isinstance(fieldname, list):
                    return FakeDict({f: rec.get(f) for f in fieldname}) if as_dict else tuple(rec.get(f) for f in fieldname)
                return rec.get(fieldname)
            if dt == "Bin" and isinstance(filt, dict):
                return harness.bins.get((filt.get("item_code"), filt.get("warehouse")), 0.0)
            if dt == "Item Price" and isinstance(filt, dict):
                return harness.item_prices.get((filt.get("item_code"), filt.get("price_list")))
            if dt == "Mode of Payment Account" and isinstance(filt, dict):
                return harness.mop_accounts.get((filt.get("parent"), filt.get("company")))
            if dt == "Account" and isinstance(filt, str):
                rec = harness.accounts.get(filt)
                if not rec:
                    return None
                if isinstance(fieldname, list):
                    return FakeDict({f: rec.get(f) for f in fieldname}) if as_dict else tuple(rec.get(f) for f in fieldname)
                return rec.get(fieldname)
            if dt == "Tax Category" and isinstance(filt, str):
                rec = harness.tax_categories.get(filt)
                if not rec:
                    return None
                if isinstance(fieldname, list):
                    return FakeDict({f: rec.get(f) for f in fieldname}) if as_dict else tuple(rec.get(f) for f in fieldname)
                return rec.get(fieldname)
            if dt == "Item Tax Template" and isinstance(filt, str):
                rec = harness.item_tax_templates.get(filt)
                if not rec:
                    return None
                if isinstance(fieldname, list):
                    return FakeDict({f: rec.get(f) for f in fieldname}) if as_dict else tuple(rec.get(f) for f in fieldname)
                return rec.get(fieldname)
            if dt == "Sales Taxes and Charges Template":
                if isinstance(filt, dict):
                    tc = filt.get("tax_category")
                    comp = filt.get("company")
                    for tpl_name, meta_or_rows in harness.tax_templates.items():
                        doc = harness.docs.get(("Sales Taxes and Charges Template", tpl_name))
                        if doc and doc.get("tax_category") == tc and (not comp or doc.get("company") == comp):
                            return tpl_name if fieldname == "name" else doc.get(fieldname)
                    return None
                doc = harness.docs.get((dt, filt))
                if doc:
                    return doc.get(fieldname)
            if dt == "POS Reason":
                name = filt if isinstance(filt, str) else filt.get("code") or filt.get("name")
                for r in harness.reasons:
                    if r.get("code") == name or r.get("name") == name:
                        if isinstance(fieldname, list):
                            return FakeDict({f: r.get(f) for f in fieldname}) if as_dict else tuple(r.get(f) for f in fieldname)
                        return r.get(fieldname)
                return None
            if isinstance(filt, str) and (dt, filt) in harness.docs:
                doc = harness.docs[(dt, filt)]
                if isinstance(fieldname, list):
                    return FakeDict({f: doc.get(f) for f in fieldname}) if as_dict else tuple(doc.get(f) for f in fieldname)
                return doc.get(fieldname)
            if isinstance(filt, dict):
                for (d_dt, _d_name), doc in harness.docs.items():
                    if d_dt == dt and all(doc.get(k) == v for k, v in filt.items()):
                        if isinstance(fieldname, list):
                            return FakeDict({f: doc.get(f) for f in fieldname}) if as_dict else tuple(doc.get(f) for f in fieldname)
                        return doc.get(fieldname)
            return None

        def get_all(dt, filters=None, fields=None, pluck=None, order_by=None, limit=None, **kwargs):
            filters = filters or {}
            rows = []
            if dt == "POS Privilege Rule":
                source = harness.rules
            elif dt == "POS Reason":
                source = harness.reasons
            elif dt == "Tax Rule":
                source = harness.tax_rules
            elif dt == "Sales Taxes and Charges":
                tpl = filters.get("parent")
                source = harness.tax_templates.get(tpl, [])
            elif dt == "Item Tax Template Detail":
                tpl = filters.get("parent")
                tpl_obj = harness.item_tax_templates.get(tpl) or {}
                source = tpl_obj.get("taxes", [])
            elif dt == "Item Tax":
                item_code = filters.get("parent")
                item_doc = harness.docs.get(("Item", item_code))
                source = (item_doc.get("taxes") or []) if item_doc else []
            else:
                source = [doc for (d_dt, _), doc in harness.docs.items() if d_dt == dt]

            for row in source:
                ok = True
                for k, v in filters.items():
                    if k in ("parent", "parenttype") and dt in ("Sales Taxes and Charges", "Item Tax Template Detail", "Item Tax"):
                        continue
                    rv = row.get(k)
                    if isinstance(v, (list, tuple)) and len(v) == 2:
                        op, target = v
                        if op == "in":
                            if rv not in target:
                                ok = False
                        elif op == "!=":
                            if rv == target:
                                ok = False
                    else:
                        if rv != v:
                            ok = False
                if ok:
                    rows.append(FakeDict(row))
            if pluck:
                return [r.get(pluck) for r in rows]
            return rows

        frappe_mod.get_all = get_all
        frappe_mod.get_list = get_all
        frappe_mod.get_value = db_get_value
        frappe_mod.get_cached_value = db_get_value

        db = FakeDict()
        db.exists = db_exists
        db.get_value = db_get_value
        db.get_all = get_all
        db.set_value = lambda dt, name, field, val=None, update_modified=True: (
            harness.docs[(dt, name)].db_set(field, val, update_modified=update_modified)
            if (dt, name) in harness.docs
            else None
        )
        db.table_exists = lambda dt: dt in {
            "POS Privilege Rule",
            "POS Reason",
            "POS Authorisation Log",
            "Tax Category",
            "Tax Rule",
            "Item Tax Template",
            "E Invoice",
        }
        db.commit = lambda: None
        db.rollback = lambda: None
        frappe_mod.db = db

        utils_mod = types.ModuleType("frappe.utils")
        utils_mod.cint = lambda v: int(float(v or 0))
        utils_mod.flt = lambda v, precision=None: (
            round(float(v or 0), precision) if precision is not None else float(v or 0)
        )
        utils_mod.now_datetime = lambda: "2026-11-24 10:00:00"
        utils_mod.nowdate = lambda: "2026-11-24"
        utils_mod.today = lambda: "2026-11-24"
        utils_mod.getdate = lambda d=None: str(d or "2026-11-24")[:10]
        utils_mod.get_datetime = lambda d=None: str(d or "2026-11-24 10:00:00")
        frappe_mod.utils = utils_mod

        return frappe_mod


def _make_override_stub(harness: FakeFrappeHarness):
    """Contract-faithful mock of B2a's ury.ury_pos.override."""
    mod = types.ModuleType("ury.ury_pos.override")

    def _canonical(claim):
        return json.dumps(claim or {}, sort_keys=True, separators=(",", ":"))

    def _claim_hash(claim):
        return hashlib.sha256(_canonical(claim).encode()).hexdigest()

    def requires_reason(pos_profile, privilege):
        prof = harness.docs.get(("POS Profile", pos_profile))
        company = prof.company if prof else None
        rows = [
            r for r in harness.rules
            if r.get("company") == company
            and r.get("privilege") == privilege
            and r.get("pos_profile") in (pos_profile, "", None)
        ]
        if rows:
            prof_rows = [r for r in rows if r.get("pos_profile")]
            target = prof_rows or rows
            return any(int(r.get("requires_reason") or 0) == 1 for r in target)
        return privilege in {
            "price_override",
            "complimentary",
            "tax_exempt",
            "service_charge_waive",
            "tip_adjust",
        }

    def may_perform(user, pos_profile, privilege, percent=None, amount=None):
        if privilege == "complimentary":
            return False
        prof = harness.docs.get(("POS Profile", pos_profile))
        if not prof:
            return False
        roles = set(harness.user_roles.get(user, []))
        rows = [
            r for r in harness.rules
            if r.get("company") == prof.company
            and r.get("privilege") == privilege
            and r.get("role") in roles
            and r.get("pos_profile") in (pos_profile, "", None)
        ]
        resolved = {}
        for r in rows:
            role = r["role"]
            if role not in resolved or (r.get("pos_profile") and not resolved[role].get("pos_profile")):
                resolved[role] = r
        for r in resolved.values():
            if int(r.get("may_perform") or 0) != 1:
                continue
            mp = float(r.get("max_percent") or 0)
            ma = float(r.get("max_amount") or 0)
            if percent is not None and mp > 0 and float(percent) > mp + 1e-9:
                continue
            if amount is not None and ma > 0 and float(amount) > ma + 1e-9:
                continue
            return True
        return False

    def consume(token, privilege, pos_profile, reference_name, claim):
        if not token or token in harness.consumed_tokens or token not in harness.tokens:
            raise FakeValidationError("Approval expired or already used [POS-AUTH-EXPIRED]")
        rec = harness.tokens[token]
        if rec["requester"] != harness.frappe.session.user:
            raise FakeValidationError("Session mismatch [POS-AUTH-CHANGED]")
        if rec["approver"] == harness.frappe.session.user:
            raise FakePermissionError("Self approval forbidden [POS-AUTH-SELF]")
        if (
            rec["privilege"] != privilege
            or rec["pos_profile"] != pos_profile
            or rec["reference_name"] != reference_name
            or _claim_hash(rec["claim"]) != _claim_hash(claim)
        ):
            raise FakeValidationError("Claim changed after approval [POS-AUTH-CHANGED]")
        harness.consumed_tokens.add(token)
        log = FakeDoc({
            "doctype": "POS Authorisation Log",
            "requester": rec["requester"],
            "approver": rec["approver"],
            "privilege": privilege,
            "pos_profile": pos_profile,
            "reference_name": reference_name,
            "claim_values": _canonical(claim),
            "reason": str((claim or {}).get("reason") or ""),
            "outcome": "Consumed",
        })
        harness.logs.append(log)
        return FakeDict(
            approver=rec["approver"],
            percent=(claim or {}).get("percent"),
            amount=(claim or {}).get("amount"),
            claim=claim,
        )

    def require(privilege, pos_profile, reference_name, claim, token=None, percent=None, amount=None):
        if token:
            return consume(token, privilege, pos_profile, reference_name, claim)
        if may_perform(harness.frappe.session.user, pos_profile, privilege, percent=percent, amount=amount):
            return None
        raise FakePermissionError("A manager must approve this action. [POS-AUTH-REQUIRED]")

    mod.requires_reason = requires_reason
    mod.may_perform = may_perform
    mod.consume = consume
    mod.require = require
    return mod


def _load_module(rel_path: str, name: str, extra_modules: dict):
    full = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, full)
    if spec is None or spec.loader is None:
        raise ImportError(f"Module not found at {full}")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, extra_modules):
        spec.loader.exec_module(module)
    return module


class TestPricingPrivilegedActions(unittest.TestCase):
    def setUp(self):
        self.h = FakeFrappeHarness()
        self.override_stub = _make_override_stub(self.h)

        # Profile with allow_rate_change=1, service charge 10% for Dine In >= 4 pax, Tips Payable account
        self.profile = FakeDoc({
            "doctype": "POS Profile",
            "name": "Main Till",
            "company": "Test Co",
            "branch": "Main Branch",
            "currency": "UGX",
            "warehouse": "Main Kitchen - TC",
            "cost_center": "Main - TC",
            "allow_rate_change": 1,
            "custom_enable_discount": 1,
            "custom_service_charge_account": "Service Charge Payable - TC",
            "custom_service_charge_rate": 10.0,
            "custom_service_charge_min_pax": 4,
            "custom_service_charge_order_types": "Dine In",
            "custom_service_charge_taxable": 0,
            "custom_service_charge_description": "Service Charge 10%",
            "custom_tips_payable_account": "Tips Payable - TC",
            "custom_max_tip_percent": 30.0,
            "custom_tax_exempt_category": "Tax Exempt",
            "custom_exempt_item_tax_template": "VAT Out of Scope - TC",
            "payments": [
                FakeDict(mode_of_payment="Cash"),
                FakeDict(mode_of_payment="Card"),
                FakeDict(mode_of_payment="Room Charge"),
            ],
        })
        self.h.docs[("POS Profile", "Main Till")] = self.profile

        self.h.docs[("URY Restaurant", "Main Rest")] = FakeDoc({
            "doctype": "URY Restaurant",
            "name": "Main Rest",
            "branch": "Main Branch",
            "default_tax_template": "VAT 18% - TC",
        })
        self.h.docs[("Branch", "Main Branch")] = FakeDoc({
            "doctype": "Branch",
            "name": "Main Branch",
            "company": "Test Co",
            "custom_no_taxes": 0,
        })

        # Standard priced item (list 20,000, cost 12,000)
        self.h.docs[("Item", "STEAK-01")] = FakeDoc({
            "doctype": "Item",
            "name": "STEAK-01",
            "item_code": "STEAK-01",
            "item_name": "Ribeye Steak",
            "stock_uom": "Nos",
            "valuation_rate": 12000.0,
            "custom_is_open_priced": 0,
            "custom_allow_zero_price": 0,
        })
        self.h.bins[("STEAK-01", "Main Kitchen - TC")] = 12000.0
        self.h.item_prices[("STEAK-01", "Standard Selling")] = 20000.0

        # Open-priced market fish item (HALO 200,000, min 15,000, requires reference)
        self.h.docs[("Item", "OPEN-FISH")] = FakeDoc({
            "doctype": "Item",
            "name": "OPEN-FISH",
            "item_code": "OPEN-FISH",
            "item_name": "Market Catch",
            "stock_uom": "Nos",
            "valuation_rate": 10000.0,
            "custom_is_open_priced": 1,
            "custom_allow_zero_price": 0,
            "custom_open_item_require_reference": 1,
            "custom_halo_max_price": 200000.0,
            "custom_min_price": 15000.0,
            "custom_privilege_group": 0,
        })

        # Zero-price item (e.g. Complimentary Tap Water / Ice)
        self.h.docs[("Item", "ZERO-WATER")] = FakeDoc({
            "doctype": "Item",
            "name": "ZERO-WATER",
            "item_code": "ZERO-WATER",
            "item_name": "Tap Water",
            "stock_uom": "Nos",
            "valuation_rate": 0.0,
            "custom_is_open_priced": 0,
            "custom_allow_zero_price": 1,
            "custom_open_item_require_reference": 0,
        })

        # Native Tax Category, Tax Rule, Sales Taxes & Charges Template, Item Tax Template
        self.h.tax_categories["Tax Exempt"] = {
            "name": "Tax Exempt",
            "custom_efris_tax_category": "03",
        }
        self.h.tax_categories["Export Zero Rated"] = {
            "name": "Export Zero Rated",
            "custom_efris_tax_category": "02",
        }
        self.h.tax_rules = [
            {
                "name": "TR-EXEMPT-1",
                "tax_type": "Sales",
                "company": "Test Co",
                "tax_category": "Tax Exempt",
                "sales_tax_template": "VAT Exempt - TC",
                "priority": 1,
            },
            {
                "name": "TR-ZERO-1",
                "tax_type": "Sales",
                "company": "Test Co",
                "tax_category": "Export Zero Rated",
                "sales_tax_template": "VAT Zero Rated - TC",
                "priority": 1,
            },
        ]
        self.h.docs[("Sales Taxes and Charges Template", "VAT 18% - TC")] = FakeDoc({
            "name": "VAT 18% - TC",
            "company": "Test Co",
        })
        self.h.tax_templates["VAT 18% - TC"] = [
            {"charge_type": "On Net Total", "account_head": "VAT Output - TC", "description": "VAT 18%", "rate": 18.0}
        ]
        self.h.docs[("Sales Taxes and Charges Template", "VAT Exempt - TC")] = FakeDoc({
            "name": "VAT Exempt - TC",
            "company": "Test Co",
            "tax_category": "Tax Exempt",
        })
        self.h.tax_templates["VAT Exempt - TC"] = [
            {"charge_type": "On Net Total", "account_head": "VAT Exempt Output - TC", "description": "VAT Exempt 0%", "rate": 0.0}
        ]
        self.h.docs[("Sales Taxes and Charges Template", "VAT Zero Rated - TC")] = FakeDoc({
            "name": "VAT Zero Rated - TC",
            "company": "Test Co",
            "tax_category": "Export Zero Rated",
        })
        self.h.tax_templates["VAT Zero Rated - TC"] = [
            {"charge_type": "On Net Total", "account_head": "VAT Zero Output - TC", "description": "VAT Zero Rated 0%", "rate": 0.0}
        ]
        self.h.item_tax_templates["VAT Out of Scope - TC"] = {
            "name": "VAT Out of Scope - TC",
            "company": "Test Co",
            "custom_efris_tax_category": "03",
            "taxes": [{"tax_type": "VAT Output - TC", "tax_rate": 0.0}],
        }

        # Accounts & Modes of Payment
        self.h.accounts["Tips Payable - TC"] = {
            "company": "Test Co", "root_type": "Liability", "is_group": 0, "disabled": 0
        }
        self.h.accounts["Card Clearing - TC"] = {
            "company": "Test Co", "root_type": "Asset", "is_group": 0, "disabled": 0
        }
        self.h.accounts["Service Charge Payable - TC"] = {
            "company": "Test Co", "root_type": "Liability", "is_group": 0, "disabled": 0
        }
        self.h.accounts["Guest Ledger Clearing - TC"] = {
            "company": "Test Co", "root_type": "Asset", "is_group": 0, "disabled": 0
        }
        self.h.mop_accounts[("Card", "Test Co")] = "Card Clearing - TC"
        self.h.mop_accounts[("Room Charge", "Test Co")] = "Guest Ledger Clearing - TC"

        # Seeded POS Reasons
        self.h.reasons = [
            {"code": "PRICE-MATCH", "description": "Manager Agreed Price", "applies_to": "price_override", "requires_note": 0, "disabled": 0},
            {"code": "PRICE-OTHER", "description": "Other Price Override", "applies_to": "price_override", "requires_note": 1, "disabled": 0},
            {"code": "PRICE-DISABLED", "description": "Old Reason", "applies_to": "price_override", "requires_note": 0, "disabled": 1},
            {"code": "VIP-COMP", "description": "VIP Complimentary", "applies_to": "complimentary", "requires_note": 0, "disabled": 0},
            {"code": "DIPLOMAT-EXEMPT", "description": "Diplomatic Mission Exemption", "applies_to": "tax_exempt", "requires_note": 0, "disabled": 0},
            {"code": "SVC-WAIVE", "description": "Service Issue Waive", "applies_to": "service_charge_waive", "requires_note": 0, "disabled": 0},
            {"code": "TIP-SLIP", "description": "Signed Card Slip Tip", "applies_to": "tip_adjust", "requires_note": 0, "disabled": 0},
            {"code": "OPEN-SPECIAL", "description": "Kitchen Daily Special", "applies_to": "open_item", "requires_note": 0, "disabled": 0},
        ]

        # Privilege rules
        self.h.rules = [
            # Cashier may override up to 10% and 3,000 UGX; above requires manager PIN
            {"company": "Test Co", "pos_profile": "", "privilege": "price_override", "role": "URY Cashier", "may_perform": 1, "may_authorise": 0, "max_percent": 10.0, "max_amount": 3000.0, "requires_reason": 1},
            {"company": "Test Co", "pos_profile": "", "privilege": "price_override", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_percent": 50.0, "max_amount": 50000.0, "requires_reason": 1},
            # Open item: Cashier allowed up to 100,000, Manager up to 500,000
            {"company": "Test Co", "pos_profile": "", "privilege": "open_item", "role": "URY Cashier", "may_perform": 1, "may_authorise": 0, "max_percent": 0.0, "max_amount": 100000.0, "requires_reason": 0},
            {"company": "Test Co", "pos_profile": "", "privilege": "open_item", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_percent": 0.0, "max_amount": 500000.0, "requires_reason": 0},
            # Complimentary: Manager may authorise only (2nd person required!)
            {"company": "Test Co", "pos_profile": "", "privilege": "complimentary", "role": "URY Manager", "may_perform": 0, "may_authorise": 1, "max_percent": 100.0, "max_amount": 0.0, "requires_reason": 1},
            # Tax exempt: Manager may perform & authorise
            {"company": "Test Co", "pos_profile": "", "privilege": "tax_exempt", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_percent": 0.0, "max_amount": 0.0, "requires_reason": 1},
            # Service charge waive: Manager may perform & authorise
            {"company": "Test Co", "pos_profile": "", "privilege": "service_charge_waive", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_percent": 100.0, "max_amount": 0.0, "requires_reason": 1},
            # Tip adjust: Manager may perform & authorise
            {"company": "Test Co", "pos_profile": "", "privilege": "tip_adjust", "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_percent": 50.0, "max_amount": 200000.0, "requires_reason": 1},
        ]

        # Open draft check with 1x STEAK-01 @ 20,000 + VAT 18%
        self.invoice = self._make_open_invoice("POS-INV-100")
        self.h.docs[("POS Invoice", "POS-INV-100")] = self.invoice

    def _make_open_invoice(self, name="POS-INV-100", *, pax=2, docstatus=0):
        inv = FakeDoc({
            "doctype": "POS Invoice",
            "name": name,
            "docstatus": docstatus,
            "is_return": 0,
            "company": "Test Co",
            "branch": "Main Branch",
            "restaurant": "Main Rest",
            "pos_profile": "Main Till",
            "customer": "Walk-in Customer",
            "order_type": "Dine In",
            "no_of_pax": pax,
            "waiter": "waiter@example.com",
            "owner": "cashier@example.com",
            "selling_price_list": "Standard Selling",
            "taxes_and_charges": "VAT 18% - TC",
            "tax_category": "",
            "items": [
                FakeDict({
                    "name": "ITEM-ROW-1",
                    "item_code": "STEAK-01",
                    "item_name": "Ribeye Steak",
                    "qty": 1.0,
                    "rate": 20000.0,
                    "price_list_rate": 20000.0,
                    "base_price_list_rate": 20000.0,
                    "amount": 20000.0,
                    "warehouse": "Main Kitchen - TC",
                    "discount_percentage": 0.0,
                    "discount_amount": 0.0,
                })
            ],
            "taxes": [
                FakeDict({
                    "charge_type": "On Net Total",
                    "account_head": "VAT Output - TC",
                    "description": "VAT 18%",
                    "rate": 18.0,
                    "tax_amount": 3600.0,
                })
            ],
            "payments": [
                FakeDict({"mode_of_payment": "Card", "amount": 23600.0})
            ],
        })
        inv.calculate_taxes_and_totals()
        return inv

    def _import_pricing_actions(self):
        ury_brand = types.ModuleType("ury.brand")
        ury_brand.coded_message = lambda code, msg: f"[{code}] {msg}"
        ury_api = types.ModuleType("ury.ury_pos.api")
        ury_api.getBranch = lambda: self.h.branch_by_user.get(self.h.frappe.session.user, "Main Branch")
        extra = {
            "frappe": self.h.frappe,
            "frappe.utils": self.h.frappe.utils,
            "ury": types.ModuleType("ury"),
            "ury.brand": ury_brand,
            "ury.ury_pos": types.ModuleType("ury.ury_pos"),
            "ury.ury_pos.api": ury_api,
            "ury.ury_pos.override": self.override_stub,
        }
        # Load privileged_auth first, then pricing_actions
        priv_auth = _load_module("ury/ury_pos/privileged_auth.py", "ury.ury_pos.privileged_auth", extra)
        extra["ury.ury_pos.privileged_auth"] = priv_auth
        pricing = _load_module("ury/ury_pos/pricing_actions.py", "ury.ury_pos.pricing_actions", extra)
        return pricing, priv_auth

    # ------------------------------------------------------------------
    # E1: Price Override (Matrix Row 11)
    # ------------------------------------------------------------------

    def test_price_override_refused_when_allow_rate_change_disabled(self):
        pricing, _ = self._import_pricing_actions()
        self.profile["allow_rate_change"] = 0
        with self.assertRaises(FakePermissionError) as ctx:
            pricing.override_line_price(
                invoice="POS-INV-100",
                pos_profile="Main Till",
                item_row_name="ITEM-ROW-1",
                new_rate=19000.0,
                reason="PRICE-MATCH",
            )
        self.assertIn("PRICE-OVERRIDE-DISABLED", str(ctx.exception))

    def test_price_override_refuses_negative_rate_even_with_manager_pin(self):
        pricing, _ = self._import_pricing_actions()
        token = self.h.mint_token(
            approver="manager@example.com",
            privilege="price_override",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim={"action": "price_override", "invoice": "POS-INV-100", "item_row_name": "ITEM-ROW-1", "new_rate": -500.0},
        )
        with self.assertRaises(FakeValidationError) as ctx:
            pricing.override_line_price(
                invoice="POS-INV-100",
                pos_profile="Main Till",
                item_row_name="ITEM-ROW-1",
                new_rate=-500.0,
                reason="PRICE-MATCH",
                approval_token=token,
            )
        self.assertIn("PRICE-NEGATIVE", str(ctx.exception))

    def test_price_override_requires_active_reason_and_note_when_configured(self):
        pricing, _ = self._import_pricing_actions()
        with self.assertRaises(FakeValidationError) as ctx1:
            pricing.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 19000.0, reason="")
        self.assertIn("POS-REASON-REQUIRED", str(ctx1.exception))

        with self.assertRaises(FakeValidationError) as ctx2:
            pricing.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 19000.0, reason="PRICE-DISABLED")
        self.assertIn("POS-REASON-INVALID", str(ctx2.exception))

        with self.assertRaises(FakeValidationError) as ctx3:
            pricing.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 19000.0, reason="PRICE-OTHER", reason_note="")
        self.assertIn("POS-REASON-NOTE-REQUIRED", str(ctx3.exception))

    def test_price_override_within_cashier_limit_succeeds_solo_and_logs(self):
        pricing, _ = self._import_pricing_actions()
        # 20,000 -> 18,500 = 7.5% (<= 10%), delta 1,500 (<= 3,000), above cost 12,000
        result = pricing.override_line_price(
            invoice="POS-INV-100",
            pos_profile="Main Till",
            item_row_name="ITEM-ROW-1",
            new_rate=18500.0,
            reason="PRICE-MATCH",
        )
        row = self.invoice["items"][0]
        self.assertEqual(row["rate"], 18500.0)
        self.assertEqual(row["price_list_rate"], 18500.0)
        self.assertEqual(row["custom_original_price_list_rate"], 20000.0)
        self.assertEqual(row["custom_price_overridden"], 1)
        self.assertEqual(row["custom_price_override_reason"], "PRICE-MATCH")
        self.assertEqual(result["rate"], 18500.0)
        self.assertTrue(any(log.get("privilege") == "price_override" for log in self.h.logs))

    def test_price_override_above_limit_requires_pin_then_succeeds_without_session_switch(self):
        pricing, _ = self._import_pricing_actions()
        # 20,000 -> 15,000 = 25% (> 10%), delta 5,000 (> 3,000), above cost 12,000
        with self.assertRaises(FakePermissionError) as ctx:
            pricing.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 15000.0, reason="PRICE-MATCH")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

        claim = pricing.build_price_override_claim(
            invoice_name="POS-INV-100",
            item_row_name="ITEM-ROW-1",
            item_code="STEAK-01",
            original_rate=20000.0,
            new_rate=15000.0,
            reason="PRICE-MATCH",
            reason_note="",
            below_cost=False,
            item_cost=12000.0,
        )
        token = self.h.mint_token(
            approver="manager@example.com",
            privilege="price_override",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim=claim,
        )
        result = pricing.override_line_price(
            invoice="POS-INV-100",
            pos_profile="Main Till",
            item_row_name="ITEM-ROW-1",
            new_rate=15000.0,
            reason="PRICE-MATCH",
            approval_token=token,
        )
        self.assertEqual(self.h.frappe.session.user, "cashier@example.com")
        self.assertEqual(result["approver"], "manager@example.com")
        self.assertEqual(self.invoice["items"][0]["rate"], 15000.0)
        self.assertEqual(self.invoice["items"][0]["custom_price_override_approver"], "manager@example.com")

    def test_adversarial_rate_below_cost_always_requires_manager_pin_even_within_pct_limit(self):
        pricing, _ = self._import_pricing_actions()
        # Raise cost to 19,500 so 19,000 is only 5% off (within cashier's 10%/3000 limit) BUT below cost!
        self.h.bins[("STEAK-01", "Main Kitchen - TC")] = 19500.0
        with self.assertRaises(FakePermissionError) as ctx:
            pricing.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 19000.0, reason="PRICE-MATCH")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

        claim = pricing.build_price_override_claim(
            invoice_name="POS-INV-100",
            item_row_name="ITEM-ROW-1",
            item_code="STEAK-01",
            original_rate=20000.0,
            new_rate=19000.0,
            reason="PRICE-MATCH",
            reason_note="",
            below_cost=True,
            item_cost=19500.0,
        )
        self.assertTrue(claim["below_cost"])
        token = self.h.mint_token(
            approver="manager@example.com",
            privilege="price_override",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim=claim,
        )
        result = pricing.override_line_price(
            "POS-INV-100", "Main Till", "ITEM-ROW-1", 19000.0, reason="PRICE-MATCH", approval_token=token
        )
        self.assertTrue(result["below_cost"])
        self.assertEqual(self.invoice["items"][0]["rate"], 19000.0)

    def test_adversarial_zero_rate_override_on_priced_item_requires_second_person_even_for_manager(self):
        pricing, _ = self._import_pricing_actions()
        self.h.frappe.session.user = "manager@example.com"
        # Even a manager with 100% price_override rule cannot zero out a priced item solo
        self.h.rules.append({
            "company": "Test Co", "pos_profile": "Main Till", "privilege": "price_override",
            "role": "URY Manager", "may_perform": 1, "may_authorise": 1, "max_percent": 100.0, "max_amount": 0.0, "requires_reason": 1,
        })
        with self.assertRaises(FakePermissionError) as ctx:
            pricing.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 0.0, reason="PRICE-MATCH")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

        claim = pricing.build_price_override_claim(
            invoice_name="POS-INV-100",
            item_row_name="ITEM-ROW-1",
            item_code="STEAK-01",
            original_rate=20000.0,
            new_rate=0.0,
            reason="PRICE-MATCH",
            reason_note="",
            below_cost=True,
            item_cost=12000.0,
        )
        token = self.h.mint_token(
            approver="manager2@example.com",
            privilege="price_override",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim=claim,
            requester="manager@example.com",
        )
        result = pricing.override_line_price(
            "POS-INV-100", "Main Till", "ITEM-ROW-1", 0.0, reason="PRICE-MATCH", approval_token=token
        )
        self.assertEqual(result["rate"], 0.0)
        self.assertEqual(result["approver"], "manager2@example.com")

    def test_adversarial_override_then_discount_then_return_preserves_net_refund(self):
        pricing, _ = self._import_pricing_actions()
        # No VAT for crisp arithmetic: list 20,000 -> override 15,000 -> 10% bill discount -> net 13,500 -> return -13,500
        self.invoice["taxes"] = []
        self.invoice["taxes_and_charges"] = ""
        self.invoice.calculate_taxes_and_totals()
        claim = pricing.build_price_override_claim(
            invoice_name="POS-INV-100",
            item_row_name="ITEM-ROW-1",
            item_code="STEAK-01",
            original_rate=20000.0,
            new_rate=15000.0,
            reason="PRICE-MATCH",
            reason_note="",
            below_cost=False,
            item_cost=12000.0,
        )
        token = self.h.mint_token(
            approver="manager@example.com",
            privilege="price_override",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim=claim,
        )
        pricing.override_line_price(
            "POS-INV-100", "Main Till", "ITEM-ROW-1", 15000.0, reason="PRICE-MATCH", approval_token=token
        )
        # Now apply 10% check discount on top of overridden rate
        self.invoice["additional_discount_percentage"] = 10.0
        self.invoice.calculate_taxes_and_totals()
        self.assertEqual(self.invoice["total"], 15000.0)
        self.assertEqual(self.invoice["discount_amount"], 1500.0)
        self.assertEqual(self.invoice["grand_total"], 13500.0)

        # Simulate native return copying overridden rate + discount
        ret = FakeDoc({
            "doctype": "POS Invoice",
            "is_return": 1,
            "return_against": "POS-INV-100",
            "additional_discount_percentage": self.invoice["additional_discount_percentage"],
            "items": [
                FakeDict({
                    "item_code": "STEAK-01",
                    "qty": -1.0,
                    "rate": self.invoice["items"][0]["rate"],
                    "price_list_rate": self.invoice["items"][0]["price_list_rate"],
                    "custom_original_price_list_rate": self.invoice["items"][0]["custom_original_price_list_rate"],
                })
            ],
            "taxes": [],
        })
        ret.calculate_taxes_and_totals()
        self.assertEqual(ret["grand_total"], -13500.0)

    def test_adversarial_efris_fiscalised_check_blocks_pricing_mutations(self):
        pricing, _ = self._import_pricing_actions()
        self.invoice["efris_irn"] = "FDN-2026-000123"
        with self.assertRaises(FakeValidationError) as ctx1:
            pricing.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 18500.0, reason="PRICE-MATCH")
        self.assertIn("EFRIS_FISCALISED_USE_RETURN", str(ctx1.exception))

        with self.assertRaises(FakeValidationError) as ctx2:
            pricing.apply_complimentary("POS-INV-100", "Main Till", scope="check", reason="VIP-COMP")
        self.assertIn("EFRIS_FISCALISED_USE_RETURN", str(ctx2.exception))

        with self.assertRaises(FakeValidationError) as ctx3:
            pricing.apply_tax_exemption(
                "POS-INV-100", "Main Till", scope="check", tax_category="Tax Exempt",
                certificate_reference="DIP-2026-99", reason="DIPLOMAT-EXEMPT",
            )
        self.assertIn("EFRIS_FISCALISED_USE_RETURN", str(ctx3.exception))

        with self.assertRaises(FakeValidationError) as ctx4:
            pricing.adjust_service_charge("POS-INV-100", "Main Till", action="waive", reason="SVC-WAIVE")
        self.assertIn("EFRIS_FISCALISED_USE_RETURN", str(ctx4.exception))

    def test_adversarial_direct_rest_rate_tamper_blocked_by_validate_hook(self):
        pricing, _ = self._import_pricing_actions()
        before = FakeDoc({
            "name": "POS-INV-100",
            "customer": "Walk-in Customer",
            "pos_profile": "Main Till",
            "items": [FakeDict({"name": "ITEM-ROW-1", "item_code": "STEAK-01", "rate": 20000.0, "price_list_rate": 20000.0})],
            "taxes": [],
            "payments": [],
        })
        tampered = FakeDoc({
            "name": "POS-INV-100",
            "customer": "Walk-in Customer",
            "pos_profile": "Main Till",
            "items": [FakeDict({"name": "ITEM-ROW-1", "item_code": "STEAK-01", "rate": 1000.0, "price_list_rate": 20000.0})],
            "taxes": [],
            "payments": [],
        }, before_save=before)
        with self.assertRaises(FakePermissionError) as ctx:
            pricing.validate_privileged_pricing_mutations(tampered)
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

        # Negative rate in direct save always raises PRICE-NEGATIVE
        tampered["items"][0]["rate"] = -10.0
        with self.assertRaises(FakeValidationError) as ctx2:
            pricing.validate_privileged_pricing_mutations(tampered)
        self.assertIn("PRICE-NEGATIVE", str(ctx2.exception))

    # ------------------------------------------------------------------
    # E2: Open Item & Zero-Price Item (Matrix Row 12)
    # ------------------------------------------------------------------

    def test_open_item_requires_reference_enforces_halo_and_logs(self):
        pricing, _ = self._import_pricing_actions()
        # Missing reference refused (Simphony Menu Item Class Opt 5)
        with self.assertRaises(FakeValidationError) as ctx1:
            pricing.add_open_item("POS-INV-100", "Main Till", "OPEN-FISH", rate=45000.0, qty=1, reference_name_text="")
        self.assertIn("OPEN-ITEM-REFERENCE-REQUIRED", str(ctx1.exception))

        # Negative rate refused
        with self.assertRaises(FakeValidationError) as ctx2:
            pricing.add_open_item("POS-INV-100", "Main Till", "OPEN-FISH", rate=-100.0, qty=1, reference_name_text="Red Snapper")
        self.assertIn("PRICE-NEGATIVE", str(ctx2.exception))

        # Zero rate refused when custom_allow_zero_price == 0
        with self.assertRaises(FakeValidationError) as ctx3:
            pricing.add_open_item("POS-INV-100", "Main Till", "OPEN-FISH", rate=0.0, qty=1, reference_name_text="Red Snapper")
        self.assertIn("OPEN-ITEM-PRICE-REQUIRED", str(ctx3.exception))

        # Within cashier limit (<= 100,000) and within [min=15,000, halo=200,000] succeeds solo
        res = pricing.add_open_item(
            "POS-INV-100", "Main Till", "OPEN-FISH", rate=45000.0, qty=1, reference_name_text="Grilled Red Snapper 650g"
        )
        added = self.invoice["items"][-1]
        self.assertEqual(added["rate"], 45000.0)
        self.assertEqual(added["custom_is_open_item"], 1)
        self.assertEqual(added["custom_open_item_reference"], "Grilled Red Snapper 650g")
        self.assertIn("Grilled Red Snapper 650g", added["item_name"])
        self.assertTrue(any(log.get("privilege") == "open_item" for log in self.h.logs))
        self.assertEqual(res["rate"], 45000.0)

        # Above HALO (250,000 > 200,000) requires Manager PIN
        with self.assertRaises(FakePermissionError) as ctx4:
            pricing.add_open_item("POS-INV-100", "Main Till", "OPEN-FISH", rate=250000.0, qty=1, reference_name_text="Whole Lobster 2kg")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx4.exception))

    def test_zero_price_item_allowed_at_zero_when_configured(self):
        pricing, _ = self._import_pricing_actions()
        res = pricing.add_open_item("POS-INV-100", "Main Till", "ZERO-WATER", rate=0.0, qty=2)
        added = self.invoice["items"][-1]
        self.assertEqual(added["rate"], 0.0)
        self.assertEqual(added["qty"], 2.0)
        self.assertEqual(res["rate"], 0.0)

    # ------------------------------------------------------------------
    # E3: Complimentary / House / Non-Revenue (Matrix Row 13)
    # ------------------------------------------------------------------

    def test_full_comp_always_requires_second_person_even_for_manager(self):
        pricing, _ = self._import_pricing_actions()
        self.h.frappe.session.user = "manager@example.com"
        with self.assertRaises(FakePermissionError) as ctx:
            pricing.apply_complimentary("POS-INV-100", "Main Till", scope="check", comp_type="Complimentary", reason="VIP-COMP")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

        claim = pricing.build_complimentary_claim(
            invoice_name="POS-INV-100",
            scope="check",
            item_row_name=None,
            comp_type="House",
            amount=23600.0,
            reason="VIP-COMP",
            reason_note="",
        )
        token = self.h.mint_token(
            approver="manager2@example.com",
            privilege="complimentary",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim=claim,
            requester="manager@example.com",
        )
        res = pricing.apply_complimentary(
            "POS-INV-100", "Main Till", scope="check", comp_type="House", reason="VIP-COMP", approval_token=token
        )
        self.assertEqual(self.invoice["custom_is_complimentary"], 1)
        self.assertEqual(self.invoice["custom_comp_type"], "House")
        self.assertEqual(self.invoice["custom_comp_approver"], "manager2@example.com")
        self.assertEqual(self.invoice["grand_total"], 0.0)
        self.assertEqual(res["approver"], "manager2@example.com")

    def test_adversarial_comp_of_room_charge_forbidden_even_with_pin(self):
        pricing, _ = self._import_pricing_actions()
        # Case A: check has room_number attached
        self.invoice["room_number"] = "RM-204"
        claim = pricing.build_complimentary_claim(
            invoice_name="POS-INV-100", scope="check", item_row_name=None,
            comp_type="Complimentary", amount=23600.0, reason="VIP-COMP", reason_note="",
        )
        token = self.h.mint_token(
            approver="manager@example.com", privilege="complimentary",
            pos_profile="Main Till", reference_name="POS-INV-100", claim=claim,
        )
        with self.assertRaises(FakeValidationError) as ctx:
            pricing.apply_complimentary(
                "POS-INV-100", "Main Till", scope="check", reason="VIP-COMP", approval_token=token
            )
        self.assertIn("COMP-ROOM-CHARGE-FORBIDDEN", str(ctx.exception))

        # Case B: check has Room Charge payment row (even without room_number)
        self.invoice["room_number"] = ""
        self.invoice["payments"] = [FakeDict({"mode_of_payment": "Room Charge", "amount": 23600.0})]
        with self.assertRaises(FakeValidationError) as ctx2:
            pricing.apply_complimentary(
                "POS-INV-100", "Main Till", scope="check", reason="VIP-COMP", approval_token=token
            )
        self.assertIn("COMP-ROOM-CHARGE-FORBIDDEN", str(ctx2.exception))

        # Case C: already-comped check refuses adding Room Charge tender in validate hook
        comped = FakeDoc({
            "name": "POS-INV-101",
            "company": "Test Co",
            "pos_profile": "Main Till",
            "custom_is_complimentary": 1,
            "items": [],
            "taxes": [],
            "payments": [FakeDict({"mode_of_payment": "Room Charge", "amount": 1000.0})],
        })
        with self.assertRaises(FakeValidationError) as ctx3:
            pricing.validate_privileged_pricing_mutations(comped)
        self.assertIn("COMP-ROOM-CHARGE-FORBIDDEN", str(ctx3.exception))

    # ------------------------------------------------------------------
    # E4: Tax Exempt (Matrix Row 15)
    # ------------------------------------------------------------------

    def test_tax_exempt_check_uses_native_tax_category_and_tax_rule_not_hand_zeroed(self):
        pricing, _ = self._import_pricing_actions()
        # Missing certificate refused
        with self.assertRaises(FakeValidationError) as ctx_cert:
            pricing.apply_tax_exemption(
                "POS-INV-100", "Main Till", scope="check", tax_category="Tax Exempt",
                certificate_reference="", reason="DIPLOMAT-EXEMPT",
            )
        self.assertIn("TAX-EXEMPT-CERT-REQUIRED", str(ctx_cert.exception))

        # Missing Tax Rule / unknown Tax Category refused (never hand-zeroes VAT row!)
        with self.assertRaises(FakeValidationError) as ctx_rule:
            pricing.apply_tax_exemption(
                "POS-INV-100", "Main Till", scope="check", tax_category="Nonexistent Category",
                certificate_reference="CERT-001", reason="DIPLOMAT-EXEMPT",
            )
        self.assertIn("TAX-EXEMPT-NO-RULE", str(ctx_rule.exception))

        # Cashier requires Manager PIN
        with self.assertRaises(FakePermissionError) as ctx_auth:
            pricing.apply_tax_exemption(
                "POS-INV-100", "Main Till", scope="check", tax_category="Tax Exempt",
                certificate_reference="DIP-UG-2026-009", reason="DIPLOMAT-EXEMPT",
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx_auth.exception))

        claim = pricing.build_tax_exempt_claim(
            invoice_name="POS-INV-100",
            scope="check",
            item_row_name=None,
            tax_category="Tax Exempt",
            item_tax_template=None,
            certificate_reference="DIP-UG-2026-009",
            efris_tax_category="03",
            reason="DIPLOMAT-EXEMPT",
            reason_note="",
        )
        token = self.h.mint_token(
            approver="manager@example.com",
            privilege="tax_exempt",
            pos_profile="Main Till",
            reference_name="POS-INV-100",
            claim=claim,
        )
        res = pricing.apply_tax_exemption(
            "POS-INV-100", "Main Till", scope="check", tax_category="Tax Exempt",
            certificate_reference="DIP-UG-2026-009", reason="DIPLOMAT-EXEMPT", approval_token=token,
        )
        self.assertEqual(self.invoice["tax_category"], "Tax Exempt")
        self.assertEqual(self.invoice["taxes_and_charges"], "VAT Exempt - TC")
        self.assertEqual(self.invoice["custom_efris_tax_category"], "03")
        self.assertEqual(self.invoice["custom_tax_exempt_certificate"], "DIP-UG-2026-009")
        self.assertEqual(self.invoice["custom_tax_exempt_customer"], "Walk-in Customer")
        self.assertEqual(self.invoice["total_taxes_and_charges"], 0.0)
        self.assertEqual(res["taxes_and_charges"], "VAT Exempt - TC")
        # Confirm it came from the native exempt template account, NOT a hand-zeroed VAT Output row
        self.assertEqual(self.invoice["taxes"][0]["account_head"], "VAT Exempt Output - TC")

    def test_tax_exempt_item_uses_native_item_tax_template(self):
        pricing, _ = self._import_pricing_actions()
        self.h.frappe.session.user = "manager@example.com"
        res = pricing.apply_tax_exemption(
            "POS-INV-100", "Main Till", scope="item", item_row_name="ITEM-ROW-1",
            item_tax_template="VAT Out of Scope - TC", certificate_reference="NGO-442",
            reason="DIPLOMAT-EXEMPT",
        )
        row = self.invoice["items"][0]
        self.assertEqual(row["item_tax_template"], "VAT Out of Scope - TC")
        self.assertEqual(self.invoice["total_taxes_and_charges"], 0.0)
        self.assertEqual(res["efris_tax_category"], "03")

    def test_adversarial_tax_exempt_customer_changed_after_exempting_is_refused(self):
        pricing, _ = self._import_pricing_actions()
        self.h.frappe.session.user = "manager@example.com"
        self.invoice["customer"] = "Embassy of Sweden"
        pricing.apply_tax_exemption(
            "POS-INV-100", "Main Till", scope="check", tax_category="Tax Exempt",
            certificate_reference="EMB-SE-77", reason="DIPLOMAT-EXEMPT",
        )
        self.assertEqual(self.invoice["custom_tax_exempt_customer"], "Embassy of Sweden")

        # Now attempt to change customer to Walk-in Customer after exempting
        before = FakeDoc(dict(self.invoice))
        self.invoice["customer"] = "Walk-in Customer"
        self.invoice._before_save = before
        with self.assertRaises(FakeValidationError) as ctx:
            pricing.validate_privileged_pricing_mutations(self.invoice)
        self.assertIn("TAX-EXEMPT-CUSTOMER-CHANGED", str(ctx.exception))

        # Clearing exemption restores default tax template so customer can safely change
        self.invoice["customer"] = "Embassy of Sweden"
        pricing.clear_tax_exemption("POS-INV-100", "Main Till")
        self.assertEqual(self.invoice["custom_tax_exempt"], 0)
        self.assertEqual(self.invoice["taxes_and_charges"], "VAT 18% - TC")

    # ------------------------------------------------------------------
    # E5: Service Charge Auto-Apply, Waive & Change (Matrix Row 14)
    # ------------------------------------------------------------------

    def test_service_charge_auto_applies_above_pax_threshold_and_waive_requires_pin(self):
        pricing, _ = self._import_pricing_actions()
        inv = self._make_open_invoice("POS-INV-SC", pax=6)
        self.h.docs[("POS Invoice", "POS-INV-SC")] = inv

        pricing.sync_service_charge(inv, self.profile)
        inv.calculate_taxes_and_totals()
        sc_rows = [r for r in inv["taxes"] if r.get("account_head") == "Service Charge Payable - TC"]
        self.assertEqual(len(sc_rows), 1)
        self.assertEqual(sc_rows[0]["tax_amount"], 2000.0)  # 10% of 20,000

        # Cashier cannot waive without Manager PIN
        with self.assertRaises(FakePermissionError) as ctx:
            pricing.adjust_service_charge("POS-INV-SC", "Main Till", action="waive", reason="SVC-WAIVE")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

        # Negative service charge rate refused even for manager
        self.h.frappe.session.user = "manager@example.com"
        with self.assertRaises(FakeValidationError) as ctx_neg:
            pricing.adjust_service_charge("POS-INV-SC", "Main Till", action="change", new_rate=-5.0, reason="SVC-WAIVE")
        self.assertIn("SERVICE-CHARGE-NEGATIVE", str(ctx_neg.exception))

        # Manager waives with reason -> row removed, flag set, log written
        res = pricing.adjust_service_charge("POS-INV-SC", "Main Till", action="waive", reason="SVC-WAIVE")
        self.assertEqual(inv["custom_service_charge_waived"], 1)
        self.assertFalse(any(r.get("account_head") == "Service Charge Payable - TC" for r in inv["taxes"]))
        self.assertEqual(res["action"], "waive")
        self.assertTrue(any(log.get("privilege") == "service_charge_waive" for log in self.h.logs))

    # ------------------------------------------------------------------
    # E6: Post-Close Tip Adjustment (Matrix Row 30, #145 model)
    # ------------------------------------------------------------------

    def test_post_close_tip_adjust_posts_liability_je_and_works_on_efris_closed_check(self):
        pricing, _ = self._import_pricing_actions()
        # Refused on draft check
        with self.assertRaises(FakeValidationError) as ctx_draft:
            pricing.adjust_closed_check_tip("POS-INV-100", "Main Till", new_tip_amount=5000.0, reason="TIP-SLIP")
        self.assertIn("TIP-CHECK-NOT-CLOSED", str(ctx_draft.exception))

        # Closed, EFRIS-fiscalised check paid by Card
        closed = self._make_open_invoice("POS-INV-CLOSED", docstatus=1)
        closed["efris_irn"] = "FDN-2026-999999"
        closed["custom_charged_tip"] = 2000.0
        closed["custom_tip_server"] = "waiter@example.com"
        self.h.docs[("POS Invoice", "POS-INV-CLOSED")] = closed

        # Negative tip always refused
        with self.assertRaises(FakeValidationError) as ctx_neg:
            pricing.adjust_closed_check_tip("POS-INV-CLOSED", "Main Till", new_tip_amount=-100.0, reason="TIP-SLIP")
        self.assertIn("TIP-NEGATIVE", str(ctx_neg.exception))

        # Cashier requires Manager PIN
        with self.assertRaises(FakePermissionError) as ctx_auth:
            pricing.adjust_closed_check_tip("POS-INV-CLOSED", "Main Till", new_tip_amount=5000.0, reason="TIP-SLIP")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx_auth.exception))

        # Manager adjusts tip 2,000 -> 5,000 (+3,000) -> Liability JE created, EFRIS FDN/items untouched
        self.h.frappe.session.user = "manager@example.com"
        res = pricing.adjust_closed_check_tip(
            "POS-INV-CLOSED", "Main Till", new_tip_amount=5000.0, payment_mode="Card", reason="TIP-SLIP"
        )
        self.assertEqual(closed["custom_charged_tip"], 5000.0)
        self.assertEqual(closed["efris_irn"], "FDN-2026-999999")
        self.assertEqual(len(self.h.journal_entries), 1)
        je = self.h.journal_entries[0]
        self.assertTrue(je.submitted)
        debit_row = [r for r in je["accounts"] if float(r.get("debit_in_account_currency") or 0) > 0][0]
        credit_row = [r for r in je["accounts"] if float(r.get("credit_in_account_currency") or 0) > 0][0]
        self.assertEqual(debit_row["account"], "Card Clearing - TC")
        self.assertEqual(debit_row["debit_in_account_currency"], 3000.0)
        self.assertEqual(credit_row["account"], "Tips Payable - TC")
        self.assertEqual(credit_row["credit_in_account_currency"], 3000.0)
        self.assertEqual(res["delta"], 3000.0)
        self.assertTrue(any(log.get("privilege") == "tip_adjust" for log in self.h.logs))

    # ------------------------------------------------------------------
    # Adversarial: Offline Replay Refusal for EVERY Pricing Action
    # ------------------------------------------------------------------

    def test_adversarial_offline_replay_refused_for_every_pricing_action(self):
        pricing, _ = self._import_pricing_actions()
        self.h.frappe.session.user = "manager@example.com"
        self.h.frappe.flags.ury_offline_replay = True
        try:
            for call in (
                lambda: pricing.override_line_price("POS-INV-100", "Main Till", "ITEM-ROW-1", 18500.0, reason="PRICE-MATCH"),
                lambda: pricing.add_open_item("POS-INV-100", "Main Till", "OPEN-FISH", rate=45000.0, qty=1, reference_name_text="Catch"),
                lambda: pricing.apply_complimentary("POS-INV-100", "Main Till", scope="check", reason="VIP-COMP"),
                lambda: pricing.apply_tax_exemption("POS-INV-100", "Main Till", scope="check", tax_category="Tax Exempt", certificate_reference="CERT-1", reason="DIPLOMAT-EXEMPT"),
                lambda: pricing.adjust_service_charge("POS-INV-100", "Main Till", action="waive", reason="SVC-WAIVE"),
                lambda: pricing.adjust_closed_check_tip("POS-INV-100", "Main Till", new_tip_amount=3000.0, reason="TIP-SLIP"),
            ):
                with self.assertRaises(FakePermissionError) as ctx:
                    call()
                self.assertIn("POS-OFFLINE-PRIVILEGED-REFUSED", str(ctx.exception))
        finally:
            self.h.frappe.flags.ury_offline_replay = False


if __name__ == "__main__":
    unittest.main()
