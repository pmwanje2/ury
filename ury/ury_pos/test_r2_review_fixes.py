"""RED -> GREEN regression tests for all 4 Hydrogen R1 blockers on pmwanje2/ury#152.

Covers:
- Blocker 2 (P0 Security — G34 Path 4 fallback bypass):
  Company-wide `override.is_policy_active(company)` governs Path 3 across
  `authorize_or_perform` and ALL callers (`ury_kot_reprint`, `ury_availability`,
  `table_reservation`, `ury_order`, `ury_pos/api`), so deleting a single
  privilege's rules while company policy is active fails closed (`POS-AUTH-REQUIRED`)
  for both API-key bots and interactive managers.
- Blocker 3 (P1 Accounting & Lifecycle — G19 Service Charge):
  1. Direct Desk/REST save (`before_validate`) resyncs Service Charge on order_type / pax change,
     without masking an unauthorized REST strip when context is unchanged.
  2. `adjust_service_charge(..., action="restore")` is NOT blocked by `[POS-AUTH-REASON]`
     even when `POS Privilege Rule.requires_reason=1` on `service_charge_waive`, and returns
     `waived`, `service_charge_amount`, `service_charge_tax_amount`.
  3. Partial return `prorate_return_service_charge` runs in `before_validate` AND inside
     `calculate_taxes_and_totals_for_service_charge` so return `grand_total` is prorated
     BEFORE `POSInvoice.validate_pos()` checks `paid_amount <= grand_total`.
  4. `get_service_charge_status` sums `tax_amount_after_discount_amount` (post-discount effective
     amount matching GL), and Pre-Discount `Actual` Service Charge + chained SC VAT are excluded
     from Pass 1 of `calculate_taxes_and_totals` so `Grand Total` discounts do not inflate.
- Blocker 4 (P1 Financial & Security — G5 Open Items & Price Override):
  1. Multi-currency `below_cost` check converts `rate_val * conversion_rate` to company currency
     before comparing against company-currency `valuation_rate`, sets `base_price_list_rate` in
     company currency, and preserves 0/2/3dp precision (UGX/USD/KWD).
  2. `validate_privileged_pricing_mutations` blocks direct REST PUT bypasses via new rows
     (`prev is None`), `discount_percentage` rate smuggling, and unauthorized header discounts.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]


def _load_mod(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeRow(dict):
    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        return self.get(key)

    def __setattr__(self, key, value):
        self[key] = value


class FakeInv(dict):
    def __init__(self, data=None, *, before_save=None, is_new=False):
        super().__init__(data or {})
        self.flags = SimpleNamespace()
        self._before_save = before_save
        self._is_new = is_new
        self._saved = 0

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        return self.get(key)

    def __setattr__(self, key, value):
        if key in ("flags", "_before_save", "_is_new", "_saved"):
            object.__setattr__(self, key, value)
        else:
            self[key] = value

    def is_new(self):
        return self._is_new

    def get_doc_before_save(self):
        return self._before_save

    def precision(self, field=None):
        cur = (self.get("currency") or "UGX").upper()
        if cur == "UGX":
            return 0
        if cur == "KWD":
            return 3
        return 2

    def check_permission(self, _perm=None):
        return True

    def set(self, key, value):
        if isinstance(value, list):
            wrapped = []
            for idx, item in enumerate(value, start=1):
                r = item if isinstance(item, FakeRow) else FakeRow(item)
                r["idx"] = idx
                wrapped.append(r)
            self[key] = wrapped
        else:
            self[key] = value

    def append(self, key, value):
        rows = self.setdefault(key, [])
        r = value if isinstance(value, FakeRow) else FakeRow(value)
        r["idx"] = len(rows) + 1
        r.setdefault("name", f"ROW-{len(rows) + 1}")
        rows.append(r)
        return r

    def save(self, *args, **kwargs):
        self._saved += 1
        return self

    def insert(self, *args, **kwargs):
        if not self.get("name"):
            self["name"] = "POS-INV-NEW-1"
        self._is_new = False
        return self

    def set_missing_values(self, *args, **kwargs):
        return None

    def run_method(self, *args, **kwargs):
        return None

    def calculate_taxes_and_totals(self):
        """Simulates real ERPNext v16 taxes_and_totals.py including Grand Total discount inflation
        on Actual rows when those rows are still attached during Pass 1."""
        prec = self.precision("grand_total")
        items = self.get("items") or []
        taxes = self.get("taxes") or []
        for idx, t in enumerate(taxes, start=1):
            t["idx"] = idx

        raw_total = 0.0
        for row in items:
            if int(row.get("is_disposable") or 0) == 1:
                row["amount"] = 0.0
                row["net_amount"] = 0.0
                continue
            amt = round(float(row.get("qty") or 0) * float(row.get("rate") or 0), prec)
            row["amount"] = amt
            raw_total += amt
        self["total"] = round(raw_total, prec)

        # First compute pre-discount taxes so Grand Total discount mirrors ERPNext set_discount_amount()
        pre_tax_total = 0.0
        pre_by_idx = {}
        for t in taxes:
            ct = t.get("charge_type")
            if ct == "Actual":
                amt = round(float(t.get("tax_amount") or 0), prec)
            elif ct == "On Net Total":
                amt = round(raw_total * float(t.get("rate") or 0) / 100.0, prec)
            elif ct == "On Previous Row Amount":
                ref_amt = pre_by_idx.get(int(t.get("row_id") or 0), 0.0)
                amt = round(ref_amt * float(t.get("rate") or 0) / 100.0, prec)
            else:
                amt = round(float(t.get("tax_amount") or 0), prec)
            pre_by_idx[t["idx"]] = amt
            if not int(t.get("included_in_print_rate") or 0):
                pre_tax_total += amt

        pre_grand_total = round(raw_total + pre_tax_total, prec)

        disc_pct = float(self.get("additional_discount_percentage") or 0)
        disc_amt = float(self.get("discount_amount") or 0)
        basis = self.get("apply_discount_on") or "Grand Total"

        if disc_pct > 0:
            base_val = raw_total if basis == "Net Total" else pre_grand_total
            disc_amt = round(base_val * disc_pct / 100.0, prec)
            self["discount_amount"] = disc_amt

        # Non-Actual denominator mirrors ERPNext get_total_for_discount_amount()
        actual_indices = {t["idx"] for t in taxes if t.get("charge_type") == "Actual"}
        actual_chain_indices = {
            t["idx"]
            for t in taxes
            if t.get("charge_type") == "On Previous Row Amount"
            and int(t.get("row_id") or 0) in actual_indices
        }
        non_actual_grand = raw_total + sum(
            pre_by_idx[t["idx"]]
            for t in taxes
            if t["idx"] not in actual_indices
            and t["idx"] not in actual_chain_indices
            and not int(t.get("included_in_print_rate") or 0)
        )

        if disc_amt > 0 and non_actual_grand > 0:
            if basis == "Net Total":
                net_after = round(max(0.0, raw_total - disc_amt), prec)
            else:
                ratio = max(0.0, 1.0 - (disc_amt / non_actual_grand))
                net_after = round(raw_total * ratio, prec)
            self["net_total"] = net_after
            item_ratio = (net_after / raw_total) if raw_total > 0 else 0.0
            for row in items:
                row["net_amount"] = round(float(row.get("amount") or 0) * item_ratio, prec)
        else:
            self["net_total"] = raw_total
            for row in items:
                row["net_amount"] = float(row.get("amount") or 0)

        tax_total = 0.0
        post_by_idx = {}
        for t in taxes:
            ct = t.get("charge_type")
            if ct == "Actual":
                post_amt = round(float(t.get("tax_amount") or 0), prec)
            elif ct == "On Net Total":
                post_amt = round(self["net_total"] * float(t.get("rate") or 0) / 100.0, prec)
            elif ct == "On Previous Row Amount":
                ref_amt = post_by_idx.get(int(t.get("row_id") or 0), 0.0)
                post_amt = round(ref_amt * float(t.get("rate") or 0) / 100.0, prec)
            else:
                post_amt = round(float(t.get("tax_amount") or 0), prec)
            t["tax_amount"] = pre_by_idx[t["idx"]]
            t["tax_amount_after_discount_amount"] = post_amt
            post_by_idx[t["idx"]] = post_amt
            if not int(t.get("included_in_print_rate") or 0):
                tax_total += post_amt

        self["total_taxes_and_charges"] = round(tax_total, prec)
        self["grand_total"] = round(self["net_total"] + tax_total, prec)
        self["rounded_total"] = self["grand_total"]


class ValidationError(Exception):
    pass


class PermissionError_(Exception):
    pass


def _make_harness():
    f = ModuleType("frappe")
    f.ValidationError = ValidationError
    f.PermissionError = PermissionError_
    f.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    f.UniqueValidationError = type("UniqueValidationError", (Exception,), {})
    f.AuthenticationError = type("AuthenticationError", (Exception,), {})
    f._ = lambda msg, *a, **kw: msg
    f.whitelist = lambda *a, **kw: (lambda fn: fn)
    f.flags = SimpleNamespace(in_patch=False, in_migrate=False, ury_offline_replay=False)
    f.session = SimpleNamespace(user="cashier@example.com", sid="sess-1")
    f.response = {}
    f.local = SimpleNamespace(response=f.response, session=f.session)
    f.request = SimpleNamespace(headers={}, cookies={"sid": "sess-1"})
    f.form_dict = {}
    f.conf = {}
    f._dict = lambda d=None, **kw: FakeRow(d or {}, **kw)
    f.generate_hash = lambda length=10: "HASH012345"[:length]
    f.has_permission = MagicMock(return_value=True)
    f.cache = MagicMock()

    def throw(msg, exc=None, **kw):
        raise (exc or ValidationError)(msg)

    f.throw = throw

    roles_by_user = {
        "cashier@example.com": ["URY Cashier"],
        "manager@example.com": ["URY Manager", "URY Cashier"],
        "api_bot@example.com": ["URY Manager", "URY Cashier"],
    }
    f.get_roles = lambda user=None: list(roles_by_user.get(user or f.session.user, []))

    docs = {}
    rules = []
    reasons = [
        {"code": "PRICE-SPECIAL", "description": "Special Price", "applies_to": "price_override,open_item", "requires_note": 0, "disabled": 0, "sort_order": 1},
        {"code": "OTHER", "description": "Other", "applies_to": "", "requires_note": 1, "disabled": 0, "sort_order": 99},
    ]
    item_prices = {}
    items_master = {}
    logs = []
    auth_tables = {"POS Privilege Rule", "POS Reason", "POS Authorisation Log", "POS Invoice", "POS Profile", "Account", "Item", "Item Price"}

    db = SimpleNamespace()
    db.table_exists = lambda dt: dt in auth_tables
    db.has_column = lambda dt, col: True

    def db_exists(dt, filt=None):
        if dt == "POS Privilege Rule":
            if isinstance(filt, dict):
                return any(all(r.get(k) == v for k, v in filt.items()) for r in rules)
            return bool(rules)
        if isinstance(filt, str):
            return (dt, filt) in docs
        return False

    def db_get_value(dt, name_or_filt, fieldname=None, as_dict=False, **kw):
        if dt == "Company" and fieldname == "default_currency":
            return "UGX"
        if dt == "Bin":
            return 0.0
        if dt == "Item":
            item = items_master.get(name_or_filt)
            if not item:
                return None
            if isinstance(fieldname, (list, tuple)):
                return {k: item.get(k) for k in fieldname}
            return item.get(fieldname)
        if dt == "Item Price" and isinstance(name_or_filt, dict):
            key = (name_or_filt.get("item_code"), name_or_filt.get("price_list"))
            val = item_prices.get(key)
            if val is None:
                return None
            return val if fieldname == "price_list_rate" else {fieldname: val}
        if dt == "POS Profile" and isinstance(name_or_filt, str):
            doc = docs.get(("POS Profile", name_or_filt))
            if not doc:
                return None
            if isinstance(fieldname, (list, tuple)):
                return {k: doc.get(k) for k in fieldname}
            return doc.get(fieldname)
        if dt == "POS Invoice" and isinstance(name_or_filt, str):
            doc = docs.get(("POS Invoice", name_or_filt))
            if not doc:
                return None
            if isinstance(fieldname, (list, tuple)):
                return {k: doc.get(k) for k in fieldname}
            return doc.get(fieldname)
        return None

    db.exists = db_exists
    db.get_value = db_get_value
    db.set_value = MagicMock()
    db.sql = MagicMock(return_value=[])
    f.db = db

    def get_doc(arg1, arg2=None, **kw):
        if isinstance(arg1, dict):
            d = FakeInv(arg1)
            if arg1.get("doctype") == "POS Authorisation Log":
                logs.append(d)
            return d
        if arg1 == "Item" and arg2 in items_master:
            return FakeInv(items_master[arg2])
        doc = docs.get((arg1, arg2))
        if doc is None:
            raise f.DoesNotExistError(f"{arg1} {arg2}")
        return doc

    def get_all(dt, filters=None, fields=None, limit=None, **kw):
        filters = filters or {}
        if dt == "POS Privilege Rule":
            out = []
            for r in rules:
                ok = True
                for k, v in filters.items():
                    if isinstance(v, list) and len(v) == 2 and v[0] == "in":
                        if r.get(k) not in v[1]:
                            ok = False
                    elif r.get(k) != v:
                        ok = False
                if ok:
                    out.append(dict(r))
            return out[:limit] if limit else out
        if dt == "POS Reason":
            out = [dict(r) for r in reasons if all(r.get(k) == v for k, v in filters.items())]
            return out
        return []

    f.get_doc = get_doc
    f.get_cached_doc = get_doc
    f.get_all = get_all
    f.new_doc = lambda dt: FakeInv({"doctype": dt}, is_new=True)
    f.get_meta = lambda dt: SimpleNamespace(has_field=lambda fn: True)

    utils = ModuleType("frappe.utils")
    utils.cint = lambda v: int(float(v or 0))
    utils.flt = lambda v, p=None: round(float(v or 0), int(p)) if p is not None else float(v or 0)
    utils.nowdate = lambda: "2026-11-24"
    utils.today = lambda: "2026-11-24"
    utils.now_datetime = lambda: "2026-11-24 12:00:00"
    utils.now = lambda: "2026-11-24 12:00:00"
    utils.get_time = lambda v=None: v
    utils.getdate = lambda v=None: v
    utils.get_datetime = lambda v=None: v
    utils.get_first_day = lambda v=None: v
    utils.get_last_day = lambda v=None: v
    f.utils = utils

    # Minimal stub for override.py testing when using real override
    modules = {
        "frappe": f,
        "frappe.utils": utils,
        "frappe.utils.password": ModuleType("frappe.utils.password"),
    }
    state = {
        "docs": docs,
        "rules": rules,
        "reasons": reasons,
        "items": items_master,
        "item_prices": item_prices,
        "logs": logs,
        "roles": roles_by_user,
    }
    return f, state, modules


class TestBlocker2Path4SecurityBypass(unittest.TestCase):
    """P0 Security (G34): Company-wide policy_active prevents Path 4 fallback when a single privilege rule is missing."""

    def test_authorize_or_perform_refuses_missing_privilege_rule_when_company_policy_active(self):
        f, state, modules = _make_harness()
        # Company policy IS active because service_charge_waive has a rule,
        # while open_item has NO rules at all.
        state["rules"].append({
            "name": "RULE-SC-1",
            "company": "Maximus Hospitality Ltd",
            "pos_profile": "",
            "privilege": "service_charge_waive",
            "role": "URY Manager",
            "may_perform": 1,
            "may_authorise": 1,
            "requires_reason": 1,
        })
        profile = FakeInv({
            "doctype": "POS Profile",
            "name": "Maximus Main Till",
            "company": "Maximus Hospitality Ltd",
            "branch": "Main",
            "currency": "UGX",
            "warehouse": "Main WH",
            "applicable_for_users": [{"user": "api_bot@example.com"}, {"user": "manager@example.com"}],
        })
        state["docs"][("POS Profile", "Maximus Main Till")] = profile
        inv = FakeInv({
            "doctype": "POS Invoice",
            "name": "INV-G34",
            "company": "Maximus Hospitality Ltd",
            "pos_profile": "Maximus Main Till",
            "currency": "UGX",
            "conversion_rate": 1.0,
            "docstatus": 0,
            "items": [],
            "taxes": [],
        })
        state["docs"][("POS Invoice", "INV-G34")] = inv
        state["items"]["OPEN-FISH"] = {
            "name": "OPEN-FISH",
            "item_code": "OPEN-FISH",
            "item_name": "Market Catch",
            "custom_is_open_priced": 1,
            "custom_allow_zero_price": 1,
            "custom_open_item_require_reference": 1,
            "custom_halo_max_price": 150000,
            "custom_min_price": 0,
            "custom_privilege_group": 0,
            "valuation_rate": 0,
        }

        # Fake override implementing company-wide is_policy_active + fail-closed require
        fake_override = ModuleType("ury.ury_pos.override")
        fake_override.is_policy_active = lambda company: bool(
            [r for r in state["rules"] if r.get("company") == company]
        )

        def fake_require(privilege, pos_profile, ref, claim, token=None, percent=None, amount=None):
            # Non-interactive API-key OR missing rule when company policy active MUST fail closed
            auth_hdr = (f.request.headers or {}).get("Authorization", "")
            if auth_hdr.lower().startswith("token "):
                f.throw("A manager must approve this action. [POS-AUTH-REQUIRED]", f.PermissionError)
            matching = [
                r for r in state["rules"]
                if r.get("company") == "Maximus Hospitality Ltd"
                and r.get("privilege") == privilege
                and r.get("role") in f.get_roles()
                and r.get("may_perform") == 1
            ]
            if not matching:
                f.throw("A manager must approve this action. [POS-AUTH-REQUIRED]", f.PermissionError)

        fake_override.require = fake_require
        fake_override.requires_reason = lambda prof, priv: False
        modules["ury.ury_pos.override"] = fake_override

        with patch.dict(sys.modules, modules):
            for modname in ("ury.ury_pos.privileged_auth", "ury.ury_pos.pricing_actions"):
                sys.modules.pop(modname, None)
            pa = _load_mod("ury.ury_pos.privileged_auth", "ury/ury_pos/privileged_auth.py")
            pricing = _load_mod("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")

            # 1) API-key manager (non-interactive) MUST be refused (was HTTP 200 Path 4 bypass in R1!)
            f.session.user = "api_bot@example.com"
            f.request.headers = {"Authorization": "token abc:def"}
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.add_open_item(
                    invoice="INV-G34",
                    pos_profile="Maximus Main Till",
                    item_code="OPEN-FISH",
                    rate=50000,
                    qty=1,
                    reference_name_text="API Bypass Fish",
                    reason="PRICE-SPECIAL",
                )

            # 2) Interactive manager also fails closed when company policy is active & open_item rule is missing
            f.session.user = "manager@example.com"
            f.request.headers = {}
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.add_open_item(
                    invoice="INV-G34",
                    pos_profile="Maximus Main Till",
                    item_code="OPEN-FISH",
                    rate=50000,
                    qty=1,
                    reference_name_text="Interactive Missing Rule Fish",
                    reason="PRICE-SPECIAL",
                )

            # 3) Helper `is_company_policy_active` exists and returns True company-wide
            self.assertTrue(hasattr(pa, "is_company_policy_active"))
            self.assertTrue(pa.is_company_policy_active("Maximus Hospitality Ltd"))

    def test_all_callers_gate_on_company_wide_policy_active(self):
        """None of the callers may gate Path 3/auth bypass on per-privilege has_privilege_rules(company, priv)."""
        caller_files = [
            "ury/ury_pos/privileged_auth.py",
            "ury/ury/api/ury_kot_reprint.py",
            "ury/ury/api/ury_availability.py",
            "ury/ury/api/table_reservation.py",
            "ury/ury/doctype/ury_order/ury_order.py",
            "ury/ury_pos/api.py",
        ]
        for rel in caller_files:
            src = (ROOT / rel).read_text()
            self.assertNotIn(
                "has_privilege_rules(",
                src.replace("def has_privilege_rules(", ""),
                f"{rel} still calls per-privilege has_privilege_rules() instead of company-wide is_company_policy_active()",
            )


class TestBlocker3ServiceChargeLifecycleAndMath(unittest.TestCase):
    """P1 Accounting & Lifecycle (G19): direct save sync, restore reason, return before_validate, status & pre-discount math."""

    def _setup_sc_profile(self, state):
        profile = FakeInv({
            "doctype": "POS Profile",
            "name": "Maximus Main Till",
            "company": "Maximus Hospitality Ltd",
            "currency": "UGX",
            "cost_center": "Main - MHL",
            "custom_tips_payable_account": "Tips Payable - MHL",
            "custom_service_charge_rules": [
                {
                    "order_type": "Dine In",
                    "enabled": 1,
                    "charge_mode": "Percentage",
                    "rate": 10.0,
                    "min_amount_floor": 5000.0,
                    "min_pax": 2,
                    "calculation_basis": "Post-Discount Subtotal",
                    "taxable": 1,
                    "account_head": "Service Charge Collected - MHL",
                    "description": "Dine-In Service Charge",
                },
                {
                    "order_type": "Take Away",
                    "enabled": 1,
                    "charge_mode": "Fixed Amount",
                    "fixed_amount": 2000.0,
                    "min_pax": 0,
                    "calculation_basis": "Post-Discount Subtotal",
                    "taxable": 0,
                    "account_head": "Service Charge Collected - MHL",
                    "description": "Packaging Service Charge",
                },
                {
                    "order_type": "Room Service",
                    "enabled": 1,
                    "charge_mode": "Percentage",
                    "rate": 15.0,
                    "min_pax": 0,
                    "calculation_basis": "Pre-Discount Subtotal",
                    "taxable": 1,
                    "account_head": "Service Charge Collected - MHL",
                    "description": "Room Service Charge",
                },
            ],
        })
        state["docs"][("POS Profile", "Maximus Main Till")] = profile
        return profile

    def test_direct_save_before_validate_syncs_sc_on_order_type_and_pax_change(self):
        f, state, modules = _make_harness()
        self._setup_sc_profile(state)
        # Stub imports needed by ury_pos_invoice.py
        ury_order_stub = ModuleType("ury.ury.doctype.ury_order.ury_order")
        ury_order_stub.release_merge_cluster_tables = MagicMock()
        sdp_stub = ModuleType("ury.ury.doctype.staff_discount_policy.staff_discount_policy")
        sdp_stub.get_applicable_policy = MagicMock(return_value=None)
        disc_stub = ModuleType("ury.ury_pos.discount")
        disc_stub.prorate_return_discount = MagicMock()
        disc_stub.validate_pos_discount = MagicMock()
        modules["ury.ury.doctype.ury_order.ury_order"] = ury_order_stub
        modules["ury.ury.doctype.staff_discount_policy.staff_discount_policy"] = sdp_stub
        modules["ury.ury_pos.discount"] = disc_stub

        with patch.dict(sys.modules, modules):
            for modname in ("ury.ury_pos.privileged_auth", "ury.ury_pos.pricing_actions", "ury.ury.hooks.ury_pos_invoice"):
                sys.modules.pop(modname, None)
            pricing = _load_mod("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")
            hook = _load_mod("ury.ury.hooks.ury_pos_invoice", "ury/ury/hooks/ury_pos_invoice.py")

            before = FakeInv({
                "name": "INV-DIRECT-1",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "currency": "UGX",
                "order_type": "Dine In",
                "no_of_pax": 2,
                "docstatus": 0,
                "items": [FakeRow({"name": "R1", "item_code": "STEAK", "qty": 1, "rate": 50000.0, "price_list_rate": 50000.0, "amount": 50000.0, "net_amount": 50000.0})],
                "taxes": [
                    FakeRow({"idx": 1, "charge_type": "On Net Total", "account_head": "VAT 18% - MHL", "rate": 18.0, "description": "VAT 18%"}),
                    FakeRow({"idx": 2, "charge_type": "On Net Total", "account_head": "Service Charge Collected - MHL", "rate": 10.0, "tax_amount": 5000.0, "custom_pos_charge_kind": "Service Charge", "description": "Dine-In Service Charge"}),
                ],
            })
            doc = FakeInv({
                "name": "INV-DIRECT-1",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "currency": "UGX",
                "order_type": "Take Away",
                "no_of_pax": 1,
                "docstatus": 0,
                "items": [FakeRow({"name": "R1", "item_code": "STEAK", "qty": 1, "rate": 50000.0, "price_list_rate": 50000.0, "amount": 50000.0, "net_amount": 50000.0})],
                "taxes": [
                    FakeRow({"idx": 1, "charge_type": "On Net Total", "account_head": "VAT 18% - MHL", "rate": 18.0, "description": "VAT 18%"}),
                    FakeRow({"idx": 2, "charge_type": "On Net Total", "account_head": "Service Charge Collected - MHL", "rate": 10.0, "tax_amount": 5000.0, "custom_pos_charge_kind": "Service Charge", "description": "Dine-In Service Charge"}),
                ],
            }, before_save=before)

            hook.before_validate(doc, "before_validate")
            doc.calculate_taxes_and_totals()
            sc_rows = [t for t in doc.taxes if t.get("custom_pos_charge_kind") == "Service Charge"]
            self.assertEqual(len(sc_rows), 1)
            self.assertEqual(sc_rows[0]["tax_amount"], 2000.0, "Direct save changing Dine In -> Take Away must resync SC from 5000 to 2000")

            # Tampering SC row when context is UNCHANGED must still be refused (not silently masked)
            tampered = FakeInv({
                "name": "INV-DIRECT-1",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "currency": "UGX",
                "order_type": "Dine In",
                "no_of_pax": 2,
                "docstatus": 0,
                "items": [FakeRow({"name": "R1", "item_code": "STEAK", "qty": 1, "rate": 50000.0, "price_list_rate": 50000.0, "amount": 50000.0, "net_amount": 50000.0})],
                "taxes": [
                    FakeRow({"idx": 1, "charge_type": "On Net Total", "account_head": "VAT 18% - MHL", "rate": 18.0, "description": "VAT 18%"}),
                ],
            }, before_save=before)
            hook.before_validate(tampered, "before_validate")
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.validate_privileged_pricing_mutations(tampered)

    def test_restore_service_charge_not_blocked_by_pos_auth_reason_and_returns_amounts(self):
        f, state, modules = _make_harness()
        self._setup_sc_profile(state)
        state["rules"].append({
            "name": "RULE-SC-1",
            "company": "Maximus Hospitality Ltd",
            "pos_profile": "",
            "privilege": "service_charge_waive",
            "role": "URY Manager",
            "may_perform": 1,
            "may_authorise": 1,
            "requires_reason": 1,
        })
        # Load real override.py to test _validate_reason directly!
        modules["frappe.utils.password"].check_password = MagicMock()
        modules["frappe.utils.password"].update_password = MagicMock()
        with patch.dict(sys.modules, modules):
            for modname in ("ury.ury_pos.override", "ury.ury_pos.privileged_auth", "ury.ury_pos.pricing_actions"):
                sys.modules.pop(modname, None)
            ov = _load_mod("ury.ury_pos.override", "ury/ury_pos/override.py")
            pricing = _load_mod("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")

            # 1) Real override._validate_reason must allow blank reason on sc_action="restore"
            restore_claim = pricing.build_service_charge_claim(
                invoice_name="INV-RESTORE-1",
                action="restore",
                new_rate=None,
                new_amount=None,
                reason="",
                reason_note="",
            )
            resolved = ov._validate_reason("Maximus Main Till", "service_charge_waive", restore_claim)
            self.assertTrue(bool(resolved), "restore must resolve a non-empty canonical audit reason without throwing POS-AUTH-REASON")

            # 2) Blank reason on sc_action="waive" MUST still throw POS-AUTH-REASON!
            waive_claim = pricing.build_service_charge_claim(
                invoice_name="INV-RESTORE-1",
                action="waive",
                new_rate=None,
                new_amount=None,
                reason="",
                reason_note="",
            )
            with self.assertRaisesRegex(ValidationError, "POS-AUTH-REASON"):
                ov._validate_reason("Maximus Main Till", "service_charge_waive", waive_claim)

            # 3) adjust_service_charge restore returns waived=False and service_charge_amount=5000.0
            inv = FakeInv({
                "doctype": "POS Invoice",
                "name": "INV-RESTORE-1",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "currency": "UGX",
                "order_type": "Dine In",
                "no_of_pax": 2,
                "docstatus": 0,
                "custom_service_charge_waived": 1,
                "items": [FakeRow({"name": "R1", "item_code": "STEAK", "qty": 1, "rate": 50000.0, "amount": 50000.0, "net_amount": 50000.0})],
                "taxes": [FakeRow({"idx": 1, "charge_type": "On Net Total", "account_head": "VAT 18% - MHL", "rate": 18.0, "description": "VAT 18%"})],
            })
            state["docs"][("POS Invoice", "INV-RESTORE-1")] = inv
            f.session.user = "manager@example.com"
            with patch.object(ov, "require", return_value=None):
                res = pricing.adjust_service_charge(
                    invoice="INV-RESTORE-1",
                    pos_profile="Maximus Main Till",
                    action="restore",
                    reason="",
                )
            self.assertEqual(res.get("waived"), False)
            self.assertEqual(res.get("service_charge_amount"), 5000.0)
            self.assertEqual(res.get("service_charge_tax_amount"), 900.0)

    def test_partial_return_prorated_in_before_validate_before_pos_validate(self):
        f, state, modules = _make_harness()
        self._setup_sc_profile(state)
        ury_order_stub = ModuleType("ury.ury.doctype.ury_order.ury_order")
        ury_order_stub.release_merge_cluster_tables = MagicMock()
        sdp_stub = ModuleType("ury.ury.doctype.staff_discount_policy.staff_discount_policy")
        sdp_stub.get_applicable_policy = MagicMock(return_value=None)
        disc_stub = ModuleType("ury.ury_pos.discount")
        disc_stub.prorate_return_discount = MagicMock()
        disc_stub.validate_pos_discount = MagicMock()
        modules["ury.ury.doctype.ury_order.ury_order"] = ury_order_stub
        modules["ury.ury.doctype.staff_discount_policy.staff_discount_policy"] = sdp_stub
        modules["ury.ury_pos.discount"] = disc_stub

        with patch.dict(sys.modules, modules):
            for modname in ("ury.ury_pos.privileged_auth", "ury.ury_pos.pricing_actions", "ury.ury.hooks.ury_pos_invoice"):
                sys.modules.pop(modname, None)
            _load_mod("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")
            hook = _load_mod("ury.ury.hooks.ury_pos_invoice", "ury/ury/hooks/ury_pos_invoice.py")

            orig = FakeInv({
                "doctype": "POS Invoice",
                "name": "INV-ORIG-1",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "currency": "UGX",
                "items": [FakeRow({"item_code": "GRILL", "qty": 2, "rate": 10000.0, "amount": 20000.0})],
                "taxes": [
                    FakeRow({"idx": 1, "charge_type": "On Net Total", "account_head": "VAT 18% - MHL", "rate": 18.0, "tax_amount": 3600.0, "description": "VAT 18%"}),
                    FakeRow({"idx": 2, "charge_type": "Actual", "account_head": "Service Charge Collected - MHL", "tax_amount": 5000.0, "custom_pos_charge_kind": "Service Charge", "description": "Dine-In Service Charge"}),
                    FakeRow({"idx": 3, "charge_type": "On Previous Row Amount", "row_id": "2", "account_head": "VAT 18% - MHL", "rate": 18.0, "tax_amount": 900.0, "custom_pos_charge_kind": "Service Charge Tax", "description": "VAT 18% on Dine-In Service Charge"}),
                ],
            })
            state["docs"][("POS Invoice", "INV-ORIG-1")] = orig

            ret = FakeInv({
                "doctype": "POS Invoice",
                "name": "INV-RET-1",
                "is_return": 1,
                "return_against": "INV-ORIG-1",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "currency": "UGX",
                "items": [FakeRow({"item_code": "GRILL", "qty": -1, "rate": 10000.0, "amount": -10000.0})],
                "taxes": [
                    FakeRow({"idx": 1, "charge_type": "On Net Total", "account_head": "VAT 18% - MHL", "rate": 18.0, "tax_amount": -1800.0, "description": "VAT 18%"}),
                    FakeRow({"idx": 2, "charge_type": "Actual", "account_head": "Service Charge Collected - MHL", "tax_amount": -5000.0, "custom_pos_charge_kind": "Service Charge", "description": "Dine-In Service Charge"}),
                    FakeRow({"idx": 3, "charge_type": "On Previous Row Amount", "row_id": "2", "account_head": "VAT 18% - MHL", "rate": 18.0, "tax_amount": -900.0, "custom_pos_charge_kind": "Service Charge Tax", "description": "VAT 18% on Dine-In Service Charge"}),
                ],
            })

            hook.before_validate(ret, "before_validate")
            sc_row = next(t for t in ret.taxes if t.get("custom_pos_charge_kind") == "Service Charge")
            self.assertEqual(sc_row["tax_amount"], -2500.0, "before_validate must prorate Actual SC before POSInvoice.validate_pos()")
            self.assertEqual(ret["grand_total"], -14750.0)

    def test_get_service_charge_status_and_pre_discount_grand_total_isolation(self):
        f, state, modules = _make_harness()
        self._setup_sc_profile(state)

        with patch.dict(sys.modules, modules):
            for modname in ("ury.ury_pos.privileged_auth", "ury.ury_pos.pricing_actions"):
                sys.modules.pop(modname, None)
            pricing = _load_mod("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")

            # 1) get_service_charge_status must return effective tax_amount_after_discount_amount (8000 / 1440, not 10000 / 1800)
            inv_post = FakeInv({
                "doctype": "POS Invoice",
                "name": "INV-POST-DISC",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "currency": "UGX",
                "order_type": "Dine In",
                "no_of_pax": 4,
                "additional_discount_percentage": 20.0,
                "discount_amount": 25960.0,
                "items": [FakeRow({"item_code": "STEAK", "qty": 2, "rate": 50000.0})],
                "taxes": [
                    FakeRow({"idx": 1, "charge_type": "On Net Total", "account_head": "VAT 18% - MHL", "rate": 18.0, "tax_amount": 18000.0, "tax_amount_after_discount_amount": 14400.0, "description": "VAT 18%"}),
                    FakeRow({"idx": 2, "charge_type": "On Net Total", "account_head": "Service Charge Collected - MHL", "rate": 10.0, "tax_amount": 10000.0, "tax_amount_after_discount_amount": 8000.0, "custom_pos_charge_kind": "Service Charge", "description": "Dine-In Service Charge"}),
                    FakeRow({"idx": 3, "charge_type": "On Previous Row Amount", "row_id": "2", "account_head": "VAT 18% - MHL", "rate": 18.0, "tax_amount": 1800.0, "tax_amount_after_discount_amount": 1440.0, "custom_pos_charge_kind": "Service Charge Tax", "description": "VAT 18% on Dine-In Service Charge"}),
                ],
            })
            state["docs"][("POS Invoice", "INV-POST-DISC")] = inv_post
            status = pricing.get_service_charge_status("INV-POST-DISC", "Maximus Main Till")
            self.assertEqual(status["service_charge_amount"], 8000.0)
            self.assertEqual(status["service_charge_tax_amount"], 1440.0)

            # 2) Pre-Discount Actual SC + SC VAT under 10% Grand Total discount must NOT inflate discount_amount
            inv_pre = FakeInv({
                "doctype": "POS Invoice",
                "name": "INV-PRE-DISC",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "currency": "UGX",
                "order_type": "Room Service",
                "no_of_pax": 1,
                "apply_discount_on": "Grand Total",
                "additional_discount_percentage": 10.0,
                "items": [FakeRow({"item_code": "GRILL", "qty": 2, "rate": 10000.0})],
                "taxes": [
                    FakeRow({"idx": 1, "charge_type": "On Net Total", "account_head": "VAT 18% - MHL", "rate": 18.0, "description": "VAT 18%"}),
                ],
            })
            pricing.sync_service_charge(inv_pre, state["docs"][("POS Profile", "Maximus Main Till")])
            self.assertTrue(
                hasattr(pricing, "calculate_taxes_and_totals_for_service_charge"),
                "pricing_actions must export calculate_taxes_and_totals_for_service_charge for CustomPOSInvoice",
            )
            pricing.calculate_taxes_and_totals_for_service_charge(inv_pre, inv_pre.calculate_taxes_and_totals)
            self.assertEqual(inv_pre["discount_amount"], 2360.0)
            self.assertEqual(inv_pre["net_total"], 18000.0)
            sc_row = next(t for t in inv_pre.taxes if t.get("custom_pos_charge_kind") == "Service Charge")
            sc_vat = next(t for t in inv_pre.taxes if t.get("custom_pos_charge_kind") == "Service Charge Tax")
            self.assertEqual(sc_row["tax_amount_after_discount_amount"], 3000.0)
            self.assertEqual(sc_vat["tax_amount_after_discount_amount"], 540.0)
            self.assertEqual(inv_pre["grand_total"], 24780.0)


class TestBlocker4MultiCurrencyCostAndRestTamper(unittest.TestCase):
    """P1 Financial & Security (G5): multi-currency below-cost check, KWD 3dp precision, and REST PUT guards."""

    def test_multicurrency_below_cost_uses_conversion_rate_and_preserves_kwd_precision(self):
        f, state, modules = _make_harness()
        # Manager allowed to override within 100% (no PIN needed unless below_cost!)
        state["rules"].append({
            "name": "RULE-PO-1",
            "company": "Maximus Hospitality Ltd",
            "pos_profile": "",
            "privilege": "price_override",
            "role": "URY Manager",
            "may_perform": 1,
            "may_authorise": 1,
            "max_percent": 100,
            "requires_reason": 1,
        })
        state["rules"].append({
            "name": "RULE-OI-1",
            "company": "Maximus Hospitality Ltd",
            "pos_profile": "",
            "privilege": "open_item",
            "role": "URY Manager",
            "may_perform": 1,
            "may_authorise": 1,
            "requires_reason": 0,
        })
        state["items"]["GRILL-PLATTER"] = {
            "name": "GRILL-PLATTER",
            "item_code": "GRILL-PLATTER",
            "item_name": "Tilapia Platter",
            "valuation_rate": 6000.0,  # 6,000 UGX in company currency
            "last_purchase_rate": 6000.0,
        }
        state["items"]["OPEN-FISH"] = {
            "name": "OPEN-FISH",
            "item_code": "OPEN-FISH",
            "item_name": "Market Catch",
            "valuation_rate": 6000.0,  # 6,000 UGX in company currency
            "custom_is_open_priced": 1,
            "custom_allow_zero_price": 0,
            "custom_open_item_require_reference": 1,
            "custom_halo_max_price": 0,
            "custom_min_price": 0,
            "custom_privilege_group": 0,
        }

        fake_override = ModuleType("ury.ury_pos.override")
        fake_override.is_policy_active = lambda company: True
        fake_override.require = MagicMock(return_value=None)
        fake_override.requires_reason = lambda prof, priv: priv == "price_override"
        modules["ury.ury_pos.override"] = fake_override

        for cur, conv, test_rate in [("USD", 4230.64, 12.34), ("KWD", 13637.0, 12.345)]:
            prof_name = f"Till {cur}"
            state["docs"][("POS Profile", prof_name)] = FakeInv({
                "doctype": "POS Profile",
                "name": prof_name,
                "company": "Maximus Hospitality Ltd",
                "currency": cur,
                "warehouse": "Main WH",
                "allow_rate_change": 1,
            })
            state["docs"][("POS Invoice", f"INV-{cur}")] = FakeInv({
                "doctype": "POS Invoice",
                "name": f"INV-{cur}",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": prof_name,
                "currency": cur,
                "conversion_rate": conv,
                "docstatus": 0,
                "items": [FakeRow({"name": "R1", "item_code": "GRILL-PLATTER", "qty": 1, "rate": test_rate, "price_list_rate": test_rate})],
                "taxes": [],
            })

        with patch.dict(sys.modules, modules):
            for modname in ("ury.ury_pos.privileged_auth", "ury.ury_pos.pricing_actions"):
                sys.modules.pop(modname, None)
            pricing = _load_mod("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")
            f.session.user = "manager@example.com"

            # 12.34 USD * 4230.64 = 52,206 UGX >> 6,000 UGX -> NOT below_cost (no PIN forced!)
            res_usd = pricing.override_line_price(
                invoice="INV-USD",
                pos_profile="Till USD",
                item_row_name="R1",
                new_rate=12.34,
                reason="PRICE-SPECIAL",
            )
            self.assertFalse(res_usd["below_cost"], "12.34 USD (~52,206 UGX) must NOT be below 6,000 UGX cost")
            inv_usd = state["docs"][("POS Invoice", "INV-USD")]
            self.assertAlmostEqual(inv_usd.items[0]["base_price_list_rate"], 12.34 * 4230.64, places=2)

            # 12.345 KWD * 13637 = 168,348 UGX >> 6,000 UGX -> NOT below_cost & preserves 3dp 12.345
            res_kwd = pricing.override_line_price(
                invoice="INV-KWD",
                pos_profile="Till KWD",
                item_row_name="R1",
                new_rate=12.345,
                reason="PRICE-SPECIAL",
            )
            self.assertFalse(res_kwd["below_cost"], "12.345 KWD (~168k UGX) must NOT be below 6,000 UGX cost")
            self.assertAlmostEqual(res_kwd["rate"], 12.345, places=4)

            # 1.00 USD * 4230.64 = 4,230.64 UGX < 6,000 UGX -> genuinely below_cost -> forces PIN!
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.override_line_price(
                    invoice="INV-USD",
                    pos_profile="Till USD",
                    item_row_name="R1",
                    new_rate=1.00,
                    reason="PRICE-SPECIAL",
                )
            self.assertTrue(f.local.response["pos_auth_challenge"]["claim"]["below_cost"])

            # add_open_item in USD at 10.00 USD (~42,306 UGX >= 6,000 UGX) -> NOT below_cost!
            res_oi = pricing.add_open_item(
                invoice="INV-USD",
                pos_profile="Till USD",
                item_code="OPEN-FISH",
                rate=10.00,
                qty=1,
                reference_name_text="USD Snapper",
                reason="PRICE-SPECIAL",
            )
            self.assertEqual(res_oi["rate"], 10.0)

    def test_validate_privileged_pricing_mutations_blocks_new_row_smuggling_and_header_discount(self):
        f, state, modules = _make_harness()
        state["item_prices"][("STEAK-PREMIUM", "Maximus Dine-In UGX")] = 50000.0
        state["items"]["OPEN-FISH"] = {"name": "OPEN-FISH", "custom_is_open_priced": 1}
        state["items"]["STEAK-PREMIUM"] = {"name": "STEAK-PREMIUM", "custom_is_open_priced": 0}

        with patch.dict(sys.modules, modules):
            for modname in ("ury.ury_pos.privileged_auth", "ury.ury_pos.pricing_actions"):
                sys.modules.pop(modname, None)
            pricing = _load_mod("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")

            before = FakeInv({
                "name": "INV-TAMPER",
                "company": "Maximus Hospitality Ltd",
                "pos_profile": "Maximus Main Till",
                "selling_price_list": "Maximus Dine-In UGX",
                "currency": "UGX",
                "discount_amount": 0.0,
                "additional_discount_percentage": 0.0,
                "items": [
                    FakeRow({"name": "R1", "item_code": "STEAK-PREMIUM", "qty": 1, "rate": 50000.0, "price_list_rate": 50000.0, "discount_percentage": 0.0, "discount_amount": 0.0}),
                ],
                "taxes": [],
            })

            # Attack 1: Append a brand-new row (prev is None) with forged rate 100 vs Item Price 50000
            doc_new_row = FakeInv({
                **dict(before),
                "items": [
                    FakeRow({"name": "R1", "item_code": "STEAK-PREMIUM", "qty": 1, "rate": 50000.0, "price_list_rate": 50000.0, "discount_percentage": 0.0, "discount_amount": 0.0}),
                    FakeRow({"name": "R-NEW", "item_code": "STEAK-PREMIUM", "qty": 1, "rate": 100.0, "price_list_rate": 100.0, "discount_percentage": 0.0, "discount_amount": 0.0}),
                ],
            }, before_save=before)
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.validate_privileged_pricing_mutations(doc_new_row)

            # Attack 2: Append an open-priced item via direct REST PUT (bypassing add_open_item)
            doc_open_rest = FakeInv({
                **dict(before),
                "items": [
                    FakeRow({"name": "R1", "item_code": "STEAK-PREMIUM", "qty": 1, "rate": 50000.0, "price_list_rate": 50000.0, "discount_percentage": 0.0, "discount_amount": 0.0}),
                    FakeRow({"name": "R-OPEN", "item_code": "OPEN-FISH", "qty": 1, "rate": 1.0, "price_list_rate": 1.0, "discount_percentage": 0.0, "discount_amount": 0.0}),
                ],
            }, before_save=before)
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.validate_privileged_pricing_mutations(doc_open_rest)

            # Attack 3: Smuggle rate 50000 -> 1 by tweaking discount_percentage 0 -> 0.01
            doc_smuggle = FakeInv({
                **dict(before),
                "items": [
                    FakeRow({"name": "R1", "item_code": "STEAK-PREMIUM", "qty": 1, "rate": 1.0, "price_list_rate": 50000.0, "discount_percentage": 0.01, "discount_amount": 5.0}),
                ],
            }, before_save=before)
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.validate_privileged_pricing_mutations(doc_smuggle)

            # Attack 4: Direct REST PUT invoice-level discount_amount=9000 without server discount flag
            doc_hdr_amt = FakeInv({
                **dict(before),
                "discount_amount": 9000.0,
            }, before_save=before)
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.validate_privileged_pricing_mutations(doc_hdr_amt)

            # Attack 5: Direct REST PUT invoice-level additional_discount_percentage=50 without server discount flag
            doc_hdr_pct = FakeInv({
                **dict(before),
                "additional_discount_percentage": 50.0,
            }, before_save=before)
            with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                pricing.validate_privileged_pricing_mutations(doc_hdr_pct)

            # Legitimate server discount write (frappe.flags.ury_server_discount_write = True) passes cleanly!
            f.flags.ury_server_discount_write = True
            try:
                pricing.validate_privileged_pricing_mutations(doc_hdr_pct)
            finally:
                f.flags.ury_server_discount_write = False


if __name__ == "__main__":
    unittest.main()
