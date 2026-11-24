"""Site-less RED->GREEN tests for G19 P0 Service Charge parity (POS-PR-5b).

Covers Oracle Simphony / SAP S/4HANA parity on top of #152:
- Multi-rule child DocType `POS Service Charge Rule` (`istable: 1`, `module: "URY POS"`)
- `Sales Taxes and Charges.custom_pos_charge_kind` metadata (`Service Charge`, `Service Charge Tax`, `Charged Tip`)
- `POS Profile.validate` rule overlap & GL account governance
- Backward-compatible fallback to #152 scalar fields when child table is empty
- Unconditional `sync_service_charge` across sync_order, make_invoice, preview_settlement_totals,
  table_transfer, transfer_check_outlet, split_bill, consolidate_checks, apply/clear_tax_exemption,
  apply_complimentary, override_line_price, add_open_item, and validate_privileged_pricing_mutations
- Exact Pre-Discount vs Post-Discount & Simphony Option 17 Floor math
- Taxable Service Charge (`On Previous Row Amount` + `row_id` chained immediately after SC, before Charged Tip)
- Waive / Override / Restore persistence & REST tamper guard
- Partial return proration capping total refunded Actual/Fixed SC to 100%
- Print format (`Merged POS Invoice Format` & `URY Bill Copy`) structured `custom_pos_charge_kind` classification
- API parity (`getPosProfileFull` rules + `getPosInvoiceItems` tax metadata)
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]


class FakeDoc(dict):
    def __init__(self, data=None, **kwargs):
        super().__init__()
        if data:
            self.update(data)
        self.update(kwargs)
        self.flags = SimpleNamespace()
        self._before_save = None
        self._saved = 0

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        if key in ("flags", "_before_save", "_saved"):
            object.__setattr__(self, key, value)
        else:
            self[key] = value

    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        if isinstance(value, list):
            wrapped = []
            for idx, item in enumerate(value, start=1):
                row = item if isinstance(item, FakeDoc) else FakeDoc(item)
                row["idx"] = idx
                wrapped.append(row)
            self[key] = wrapped
        else:
            self[key] = value

    def append(self, key, value):
        rows = self.setdefault(key, [])
        row = value if isinstance(value, FakeDoc) else FakeDoc(value)
        row["idx"] = len(rows) + 1
        rows.append(row)
        return row

    def remove(self, row):
        for key in ("items", "taxes", "payments"):
            rows = self.get(key)
            if isinstance(rows, list) and row in rows:
                rows.remove(row)
                for idx, r in enumerate(rows, start=1):
                    r["idx"] = idx

    def precision(self, fieldname):
        return 0 if self.get("currency") == "UGX" else 2

    def as_dict(self, **kwargs):
        out = dict(self)
        for k in ("items", "taxes", "payments"):
            if isinstance(out.get(k), list):
                out[k] = [dict(r) for r in out[k]]
        return out

    def get_doc_before_save(self):
        return self._before_save

    def check_permission(self, permtype="read"):
        return True

    def is_new(self):
        return not bool(self.get("name"))

    def set_missing_values(self, *args, **kwargs):
        return None

    def run_method(self, *args, **kwargs):
        return None

    def set_paid_amount(self):
        self["paid_amount"] = sum(float(p.get("amount") or 0) for p in (self.get("payments") or []))

    def set_outstanding_amount(self):
        self["outstanding_amount"] = max(0.0, float(self.get("grand_total") or 0) - float(self.get("paid_amount") or 0))

    def calculate_taxes_and_totals(self):
        """Faithful mini-controller matching ERPNext taxes_and_totals.py semantics for unit tests."""
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
        disc_pct = float(self.get("additional_discount_percentage") or 0)
        disc_amt = float(self.get("discount_amount") or 0)

        # Mirror ERPNext get_total_for_discount_amount: Actual charges and rows
        # referencing an Actual row via row_id are excluded from discount basis.
        actual_indices = {t["idx"] for t in taxes if t.get("charge_type") == "Actual"}
        if disc_pct > 0:
            # First pass pre-discount net_total
            net = raw_total
            # Apply discount proportionally to items (ERPNext Grand Total / Net Total discount reduces net_total)
            net_after = round(net * (1.0 - disc_pct / 100.0), prec)
            self["discount_amount"] = round(net - net_after, prec)
            self["net_total"] = net_after
            for row in items:
                row["net_amount"] = round(float(row.get("amount") or 0) * (1.0 - disc_pct / 100.0), prec)
        elif disc_amt > 0:
            self["net_total"] = round(max(0.0, raw_total - disc_amt), prec)
            ratio = (self["net_total"] / raw_total) if raw_total > 0 else 0.0
            for row in items:
                row["net_amount"] = round(float(row.get("amount") or 0) * ratio, prec)
        else:
            self["net_total"] = self["total"]
            for row in items:
                row["net_amount"] = float(row.get("amount") or 0)

        tax_total = 0.0
        by_idx = {}
        for t in taxes:
            ct = t.get("charge_type")
            if ct == "Actual":
                amt = round(float(t.get("tax_amount") or 0), prec)
            elif ct == "On Net Total":
                amt = round(self["net_total"] * float(t.get("rate") or 0) / 100.0, prec)
            elif ct == "On Previous Row Amount":
                ref_idx = int(t.get("row_id") or 0)
                ref_amt = by_idx.get(ref_idx, 0.0)
                amt = round(ref_amt * float(t.get("rate") or 0) / 100.0, prec)
            else:
                amt = round(float(t.get("tax_amount") or 0), prec)
            t["tax_amount"] = amt
            by_idx[t["idx"]] = amt
            if not int(t.get("included_in_print_rate") or 0):
                tax_total += amt

        self["total_taxes_and_charges"] = round(tax_total, prec)
        self["grand_total"] = round(self["net_total"] + tax_total, prec)
        self["rounded_total"] = self["grand_total"]
        # Expose actual_indices for inspection
        self["_actual_indices"] = actual_indices

    def save(self, *args, **kwargs):
        self._saved += 1
        return self

    def insert(self, *args, **kwargs):
        if not self.get("name"):
            self["name"] = "POS-INV-NEW-1"
        return self

    def reload(self):
        return self

    def discard(self):
        self["docstatus"] = 2

    def db_set(self, field, value=None, **kwargs):
        if isinstance(field, dict):
            self.update(field)
        else:
            self[field] = value


class ValidationError(Exception):
    pass


class PermissionError_(Exception):
    pass


def _build_frappe_harness():
    f = ModuleType("frappe")
    f.ValidationError = ValidationError
    f.PermissionError = PermissionError_
    f.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
    f._ = lambda msg, *a, **kw: msg
    f.whitelist = lambda *a, **kw: (lambda fn: fn)
    f.flags = SimpleNamespace(in_patch=False, in_migrate=False, ury_offline_replay=False)
    f.session = SimpleNamespace(user="manager@example.com")
    f.response = {}
    f.local = SimpleNamespace(response=f.response)
    f._dict = lambda d=None, **kw: FakeDoc(d, **kw)
    f.msgprint = MagicMock()
    f.cache = MagicMock()

    def throw(msg, exc=None, **kw):
        raise (exc or ValidationError)(msg)

    f.throw = throw
    f.has_permission = MagicMock(return_value=True)
    f.get_roles = MagicMock(return_value=["URY Manager", "URY Cashier"])
    f.generate_hash = MagicMock(return_value="SPLITGRP1")

    docs: dict[tuple[str, str], FakeDoc] = {}
    accounts: dict[str, dict] = {
        "Service Charge Payable - TC": {
            "name": "Service Charge Payable - TC",
            "company": "Test Co",
            "root_type": "Liability",
            "is_group": 0,
            "disabled": 0,
        },
        "Service Charge Income - TC": {
            "name": "Service Charge Income - TC",
            "company": "Test Co",
            "root_type": "Income",
            "is_group": 0,
            "disabled": 0,
        },
        "Tips Payable - TC": {
            "name": "Tips Payable - TC",
            "company": "Test Co",
            "root_type": "Liability",
            "is_group": 0,
            "disabled": 0,
        },
        "Output VAT 18% - TC": {
            "name": "Output VAT 18% - TC",
            "company": "Test Co",
            "root_type": "Liability",
            "is_group": 0,
            "disabled": 0,
        },
        "COGS - TC": {
            "name": "COGS - TC",
            "company": "Test Co",
            "root_type": "Expense",
            "is_group": 0,
            "disabled": 0,
        },
        "Other Co Income - OC": {
            "name": "Other Co Income - OC",
            "company": "Other Co",
            "root_type": "Income",
            "is_group": 0,
            "disabled": 0,
        },
        "Group Liability - TC": {
            "name": "Group Liability - TC",
            "company": "Test Co",
            "root_type": "Liability",
            "is_group": 1,
            "disabled": 0,
        },
        "Disabled SC - TC": {
            "name": "Disabled SC - TC",
            "company": "Test Co",
            "root_type": "Income",
            "is_group": 0,
            "disabled": 1,
        },
    }
    tax_templates: dict[str, list[dict]] = {
        "Standard VAT 18%": [
            {
                "charge_type": "On Net Total",
                "account_head": "Output VAT 18% - TC",
                "rate": 18.0,
                "description": "Output VAT 18%",
                "included_in_print_rate": 0,
            }
        ],
        "Diplomatic Exempt": [
            {
                "charge_type": "On Net Total",
                "account_head": "Output VAT 18% - TC",
                "rate": 0.0,
                "description": "VAT Exempt 0%",
                "included_in_print_rate": 0,
            }
        ],
    }
    tax_rules = [
        {"tax_type": "Sales", "company": "Test Co", "tax_category": "Diplomatic Exempt", "sales_tax_template": "Diplomatic Exempt"}
    ]
    reasons = [
        {"code": "VIP_COMP", "description": "VIP guest waiver", "applies_to": "service_charge_waive,tax_exempt", "requires_note": 0, "disabled": 0}
    ]
    auth_logs: list[FakeDoc] = []

    db = SimpleNamespace()
    db.table_exists = lambda dt: True
    db.has_column = lambda dt, col: True
    db.commit = lambda: None
    db.rollback = lambda **kw: None
    db.savepoint = lambda name: None

    def exists(dt, name=None):
        if isinstance(name, dict):
            return False
        if dt == "Account":
            return name in accounts
        if dt == "Sales Taxes and Charges Template":
            return name in tax_templates
        if dt == "Tax Category":
            return name in ("Diplomatic Exempt",)
        if dt == "DocType":
            return True
        return (dt, name) in docs

    def get_value(dt, filters, fieldname=None, as_dict=False, **kw):
        if dt == "Account":
            acct = accounts.get(filters)
            if not acct:
                return None
            if isinstance(fieldname, (list, tuple)):
                return FakeDoc({k: acct.get(k) for k in fieldname}) if as_dict else tuple(acct.get(k) for k in fieldname)
            return acct.get(fieldname)
        if dt == "URY Restaurant":
            return "Standard VAT 18%" if fieldname == "default_tax_template" else None
        if dt == "Tax Category" and fieldname == "custom_efris_tax_category":
            return "03" if filters == "Diplomatic Exempt" else ""
        if dt == "POS Profile" and isinstance(filters, str):
            prof = docs.get(("POS Profile", filters))
            if not prof:
                return None
            if isinstance(fieldname, (list, tuple)):
                return FakeDoc({k: prof.get(k) for k in fieldname}) if as_dict else tuple(prof.get(k) for k in fieldname)
            return prof.get(fieldname)
        if isinstance(filters, str) and (dt, filters) in docs:
            d = docs[(dt, filters)]
            if isinstance(fieldname, (list, tuple)):
                return FakeDoc({k: d.get(k) for k in fieldname}) if as_dict else tuple(d.get(k) for k in fieldname)
            return d.get(fieldname)
        return None

    db.exists = exists
    db.get_value = get_value
    db.set_value = lambda dt, name, field, val=None, **kw: None
    db.sql = lambda q, p=(), as_dict=False, **kw: []

    def get_doc(arg, name=None, **kw):
        if isinstance(arg, dict):
            d = FakeDoc(arg)
            if d.get("doctype") == "POS Authorisation Log":
                d.insert = lambda **ikw: (auth_logs.append(d), d)[1]
            return d
        if arg == "Sales Taxes and Charges Template" and name in tax_templates:
            t = FakeDoc(name=name)
            t.set("taxes", [dict(r) for r in tax_templates[name]])
            return t
        if (arg, name) in docs:
            return docs[(arg, name)]
        raise f.DoesNotExistError(f"{arg} {name}")

    def get_all(dt, filters=None, fields=None, pluck=None, **kw):
        if dt == "Sales Taxes and Charges" and isinstance(filters, dict):
            tpl_name = filters.get("parent")
            return [dict(r) for r in tax_templates.get(tpl_name, [])]
        if dt == "Tax Rule":
            return list(tax_rules)
        if dt == "POS Reason":
            return list(reasons)
        if dt == "POS Privilege Rule":
            return []
        if dt == "POS Invoice" and isinstance(filters, dict) and filters.get("is_return") == 1:
            ra = filters.get("return_against")
            matches = [
                d for (k_dt, _), d in docs.items()
                if k_dt == "POS Invoice"
                and int(d.get("is_return") or 0) == 1
                and int(d.get("docstatus") or 0) == 1
                and d.get("return_against") == ra
            ]
            if isinstance(filters.get("name"), list) and filters["name"][0] == "!=":
                excl = filters["name"][1]
                matches = [m for m in matches if m.get("name") != excl]
            return [m.get(pluck) for m in matches] if pluck else matches
        return []

    f.db = db
    f.get_doc = get_doc
    f.get_all = get_all
    f.new_doc = lambda dt: FakeDoc(doctype=dt, items=[], taxes=[], payments=[])
    f.get_meta = lambda dt: SimpleNamespace(has_field=lambda fn: True, default_print_format=None)

    utils = ModuleType("frappe.utils")
    utils.cint = lambda v: int(float(v or 0))
    utils.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
    utils.cstr = lambda v: "" if v is None else str(v)
    utils.getdate = lambda v=None: "2026-11-24"
    utils.nowdate = lambda: "2026-11-24"
    utils.now = lambda: "2026-11-24 12:00:00"
    utils.now_datetime = lambda: "2026-11-24 12:00:00"
    utils.get_datetime = lambda v=None: v
    utils.get_system_timezone = lambda: "Africa/Kampala"
    utils.nestedset = MagicMock()
    utils.validate_phone_number = MagicMock(return_value=True)
    f.utils = utils

    cf_mod = ModuleType("frappe.custom.doctype.custom_field.custom_field")
    cf_mod.create_custom_fields = MagicMock()

    ss_mod = ModuleType("frappe.core.doctype.server_script.server_script_utils")
    ss_mod.get_server_script_map = MagicMock(return_value={})

    modules = {
        "frappe": f,
        "frappe.utils": utils,
        "frappe.model": ModuleType("frappe.model"),
        "frappe.model.document": SimpleNamespace(Document=object),
        "frappe.custom": ModuleType("frappe.custom"),
        "frappe.custom.doctype": ModuleType("frappe.custom.doctype"),
        "frappe.custom.doctype.custom_field": ModuleType("frappe.custom.doctype.custom_field"),
        "frappe.custom.doctype.custom_field.custom_field": cf_mod,
        "frappe.core": ModuleType("frappe.core"),
        "frappe.core.doctype": ModuleType("frappe.core.doctype"),
        "frappe.core.doctype.server_script": ModuleType("frappe.core.doctype.server_script"),
        "frappe.core.doctype.server_script.server_script_utils": ss_mod,
    }
    return f, docs, modules, auth_logs


def _load(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestServiceChargeSchemaAndFields(unittest.TestCase):
    """Verify POS Service Charge Rule child DocType & custom_pos_charge_kind schema."""

    def test_child_doctype_pos_service_charge_rule_exists_with_exact_fields(self):
        json_path = ROOT / "ury/ury_pos/doctype/pos_service_charge_rule/pos_service_charge_rule.json"
        py_path = ROOT / "ury/ury_pos/doctype/pos_service_charge_rule/pos_service_charge_rule.py"
        self.assertTrue(json_path.is_file(), f"Missing child DocType JSON: {json_path}")
        self.assertTrue(py_path.is_file(), f"Missing child DocType controller: {py_path}")

        meta = json.loads(json_path.read_text())
        self.assertEqual(meta["name"], "POS Service Charge Rule")
        self.assertEqual(meta["module"], "URY POS")
        self.assertEqual(meta["istable"], 1)

        fields = {f["fieldname"]: f for f in meta["fields"]}
        required = {
            "order_type",
            "enabled",
            "charge_mode",
            "rate",
            "fixed_amount",
            "min_amount_floor",
            "min_pax",
            "calculation_basis",
            "taxable",
            "account_head",
            "description",
            "efris_item_code",
        }
        self.assertTrue(required.issubset(fields.keys()), f"Missing fields: {required - set(fields.keys())}")
        self.assertIn("*", fields["order_type"]["options"])
        self.assertIn("Room Service", fields["order_type"]["options"])
        self.assertIn("Percentage", fields["charge_mode"]["options"])
        self.assertIn("Fixed Amount", fields["charge_mode"]["options"])
        self.assertIn("Post-Discount Subtotal", fields["calculation_basis"]["options"])
        self.assertIn("Pre-Discount Subtotal", fields["calculation_basis"]["options"])
        self.assertEqual(fields["efris_item_code"]["fieldtype"], "Link")
        self.assertEqual(fields["efris_item_code"]["options"], "Item")

    def test_custom_fields_include_service_charge_rules_table_and_pos_charge_kind(self):
        f, _docs, modules, _logs = _build_frappe_harness()
        with patch.dict(sys.modules, modules):
            pf = _load("ury.ury_pos.privileged_fields", "ury/ury_pos/privileged_fields.py")
            fields = pf.get_privileged_action_custom_fields()

        prof_fields = {r["fieldname"]: r for r in fields.get("POS Profile", [])}
        self.assertIn("custom_service_charge_rules", prof_fields)
        self.assertEqual(prof_fields["custom_service_charge_rules"]["fieldtype"], "Table")
        self.assertEqual(prof_fields["custom_service_charge_rules"]["options"], "POS Service Charge Rule")

        tax_fields = {r["fieldname"]: r for r in fields.get("Sales Taxes and Charges", [])}
        self.assertIn("custom_pos_charge_kind", tax_fields)
        kind = tax_fields["custom_pos_charge_kind"]
        self.assertEqual(kind["fieldtype"], "Select")
        self.assertEqual(kind.get("hidden"), 1)
        for opt in ("Service Charge", "Service Charge Tax", "Charged Tip"):
            self.assertIn(opt, kind["options"])

    def test_order_type_options_include_room_service_in_setup_fixtures_and_migration(self):
        # 1. setup_customizations.py must include Room Service on POS Invoice & Sales Invoice
        setup_src = (ROOT / "ury/setup_customizations.py").read_text()
        self.assertIn(
            "Room Service",
            setup_src,
            "ury/setup_customizations.py must include Room Service in order_type Select options",
        )

        # 2. fixtures/custom_field.json must include Room Service on POS Invoice & Sales Invoice
        cf_rows = json.loads((ROOT / "ury/fixtures/custom_field.json").read_text())
        by_name = {row["name"]: row for row in cf_rows if isinstance(row, dict) and "name" in row}
        for cf_name in ("POS Invoice-order_type", "Sales Invoice-order_type"):
            self.assertIn(cf_name, by_name)
            opts = (by_name[cf_name].get("options") or "").splitlines()
            self.assertIn(
                "Room Service",
                opts,
                f"fixtures/custom_field.json {cf_name} options missing Room Service: {opts}",
            )

        # 3. ensure_privileged_action_custom_fields() upgrades pre-existing sites' Custom Field options
        f, docs, modules, _logs = _build_frappe_harness()
        existing_cf = {
            "POS Invoice-order_type": "\nDine In\nPhone In\nTake Away\nDelivery\nAggregators",
            "Sales Invoice-order_type": "\nDine In\nTake Away\nDelivery\nPhone In\nAggregators",
        }
        orig_exists = f.db.exists
        orig_get_value = f.db.get_value

        def cf_exists(dt, name=None):
            if dt == "Custom Field" and name in existing_cf:
                return name
            return orig_exists(dt, name)

        def cf_get_value(dt, name, fieldname=None, as_dict=False, **kw):
            if dt == "Custom Field" and name in existing_cf and fieldname == "options":
                return existing_cf[name]
            return orig_get_value(dt, name, fieldname, as_dict=as_dict, **kw)

        def cf_set_value(dt, name, fieldname, val=None, **kw):
            if dt == "Custom Field" and name in existing_cf and fieldname == "options":
                existing_cf[name] = val

        f.db.exists = cf_exists
        f.db.get_value = cf_get_value
        f.db.set_value = cf_set_value
        f.clear_cache = MagicMock()

        with patch.dict(sys.modules, modules):
            sys.modules.pop("ury.ury_pos.privileged_fields", None)
            pf = _load("ury.ury_pos.privileged_fields", "ury/ury_pos/privileged_fields.py")
            pf.ensure_privileged_action_custom_fields()

        for cf_name, opts_str in existing_cf.items():
            self.assertIn(
                "Room Service",
                opts_str.splitlines(),
                f"ensure_privileged_action_custom_fields did not add Room Service to {cf_name}",
            )


class TestServiceChargeEngineAndHooks(unittest.TestCase):
    def setUp(self):
        self.f, self.docs, self.modules, self.logs = _build_frappe_harness()
        self.enterContext(patch.dict(sys.modules, self.modules))
        for name in (
            "ury.ury_pos.privileged_auth",
            "ury.ury_pos.pricing_actions",
            "ury.ury_pos.floor_actions",
            "ury.ury.hooks.ury_pos_profile",
        ):
            sys.modules.pop(name, None)
        self.pricing = _load("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")
        self.pos_profile_hook = _load("ury.ury.hooks.ury_pos_profile", "ury/ury/hooks/ury_pos_profile.py")

    def _make_profile(self, rules=None, **extra):
        defaults = {
            "doctype": "POS Profile",
            "name": "Main Till",
            "company": "Test Co",
            "branch": "Main Branch",
            "currency": "UGX",
            "cost_center": "Main - TC",
            "custom_tips_payable_account": "Tips Payable - TC",
            "custom_service_charge_rate": 0.0,
            "custom_service_charge_account": "",
            "custom_tax_exempt_category": "Diplomatic Exempt",
        }
        defaults.update(extra)
        prof = FakeDoc(defaults)
        prof.set("custom_service_charge_rules", rules or [])
        self.docs[("POS Profile", prof.name)] = prof
        return prof

    def _make_invoice(self, name="INV-1", items=None, taxes=None, **extra):
        defaults = {
            "doctype": "POS Invoice",
            "name": name,
            "docstatus": 0,
            "is_return": 0,
            "company": "Test Co",
            "branch": "Main Branch",
            "pos_profile": "Main Till",
            "restaurant": "Main Resto",
            "currency": "UGX",
            "cost_center": "Main - TC",
            "order_type": "Dine In",
            "no_of_pax": 6,
            "customer": "Walk In",
            "additional_discount_percentage": 0.0,
            "discount_amount": 0.0,
            "custom_tax_exempt": 0,
            "custom_is_complimentary": 0,
            "custom_service_charge_waived": 0,
        }
        defaults.update(extra)
        inv = FakeDoc(defaults)
        inv.set("items", items or [{"name": "L1", "item_code": "STEAK", "qty": 2, "rate": 50000}])
        inv.set("taxes", taxes if taxes is not None else [
            {
                "charge_type": "On Net Total",
                "account_head": "Output VAT 18% - TC",
                "rate": 18.0,
                "description": "Output VAT 18%",
                "included_in_print_rate": 0,
            }
        ])
        inv.calculate_taxes_and_totals()
        self.docs[("POS Invoice", name)] = inv
        return inv

    def test_profile_validation_rejects_overlap_and_bad_accounts(self):
        # Overlapping enabled rules for same (order_type, min_pax)
        prof = self._make_profile(rules=[
            {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 10, "min_pax": 6, "account_head": "Service Charge Payable - TC"},
            {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 12, "min_pax": 6, "account_head": "Service Charge Income - TC"},
        ])
        with self.assertRaisesRegex(ValidationError, "SERVICE-CHARGE-RULE-OVERLAP"):
            self.pos_profile_hook.validate_service_charge_rules(prof)

        # Same account as Tips Payable refused
        prof2 = self._make_profile(rules=[
            {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 10, "min_pax": 0, "account_head": "Tips Payable - TC"},
        ])
        with self.assertRaisesRegex(ValidationError, "SERVICE-CHARGE-ACCOUNT"):
            self.pos_profile_hook.validate_service_charge_rules(prof2)

        # Expense / Group / Disabled / Other-company account refused
        for bad_acct in ("COGS - TC", "Group Liability - TC", "Disabled SC - TC", "Other Co Income - OC"):
            prof_bad = self._make_profile(rules=[
                {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 10, "min_pax": 0, "account_head": bad_acct},
            ])
            with self.subTest(bad_acct=bad_acct):
                with self.assertRaisesRegex(ValidationError, "SERVICE-CHARGE-ACCOUNT"):
                    self.pos_profile_hook.validate_service_charge_rules(prof_bad)

        # Valid multi-rule config passes cleanly
        prof_ok = self._make_profile(rules=[
            {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 10, "min_pax": 6, "account_head": "Service Charge Payable - TC"},
            {"order_type": "Room Service", "enabled": 1, "charge_mode": "Fixed Amount", "fixed_amount": 10000, "min_pax": 0, "account_head": "Service Charge Income - TC"},
        ])
        self.pos_profile_hook.validate_service_charge_rules(prof_ok)

    def test_multi_rule_order_type_and_pax_precedence_and_clear_on_switch(self):
        prof = self._make_profile(rules=[
            {"order_type": "*", "enabled": 1, "charge_mode": "Percentage", "rate": 3, "min_pax": 0, "account_head": "Service Charge Income - TC", "description": "Default Fee"},
            {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 5, "min_pax": 0, "account_head": "Service Charge Payable - TC", "description": "Dine In SC 5%"},
            {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 10, "min_pax": 6, "account_head": "Service Charge Payable - TC", "description": "Large Party SC 10%"},
            {"order_type": "Room Service", "enabled": 1, "charge_mode": "Fixed Amount", "fixed_amount": 10000, "min_pax": 0, "account_head": "Service Charge Income - TC", "description": "Tray Fee"},
        ])
        inv = self._make_invoice(order_type="Dine In", no_of_pax=6)
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()

        sc_rows = [t for t in inv.taxes if t.get("custom_pos_charge_kind") == "Service Charge"]
        self.assertEqual(len(sc_rows), 1)
        self.assertEqual(sc_rows[0].charge_type, "On Net Total")
        self.assertEqual(sc_rows[0].rate, 10.0)
        self.assertEqual(sc_rows[0].tax_amount, 10000)

        # Lower pax drops from 10% tier to 5% tier
        inv.no_of_pax = 2
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()
        sc_rows = [t for t in inv.taxes if t.get("custom_pos_charge_kind") == "Service Charge"]
        self.assertEqual(sc_rows[0].rate, 5.0)
        self.assertEqual(sc_rows[0].tax_amount, 5000)

        # Room Service gets Fixed Amount Actual UGX 10,000
        inv.order_type = "Room Service"
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()
        sc_rows = [t for t in inv.taxes if t.get("custom_pos_charge_kind") == "Service Charge"]
        self.assertEqual(sc_rows[0].charge_type, "Actual")
        self.assertEqual(sc_rows[0].tax_amount, 10000)

        # Switching to Take Away when no wildcard rule matches clears SC completely
        prof_no_wild = self._make_profile(rules=[
            {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 10, "min_pax": 6, "account_head": "Service Charge Payable - TC"},
        ])
        inv.order_type = "Take Away"
        self.pricing.sync_service_charge(inv, prof_no_wild)
        self.assertEqual([t for t in inv.taxes if t.get("custom_pos_charge_kind") == "Service Charge"], [])

    def test_pre_discount_vs_post_discount_and_floor_amount(self):
        # Pre-Discount 10% uses Actual so 20% discount NEVER erodes SC
        prof_pre = self._make_profile(rules=[
            {
                "order_type": "Dine In",
                "enabled": 1,
                "charge_mode": "Percentage",
                "rate": 10,
                "min_pax": 0,
                "calculation_basis": "Pre-Discount Subtotal",
                "account_head": "Service Charge Payable - TC",
                "description": "Pre-Discount SC",
            }
        ])
        inv_pre = self._make_invoice(name="INV-PRE", additional_discount_percentage=20.0)
        self.pricing.sync_service_charge(inv_pre, prof_pre)
        inv_pre.calculate_taxes_and_totals()
        sc_pre = [t for t in inv_pre.taxes if t.get("custom_pos_charge_kind") == "Service Charge"][0]
        self.assertEqual(sc_pre.charge_type, "Actual")
        self.assertEqual(sc_pre.tax_amount, 10000)  # 10% of 100,000 untouched by 20% discount!

        # Post-Discount 10% uses On Net Total so 20% discount scales SC to 8,000
        prof_post = self._make_profile(rules=[
            {
                "order_type": "Dine In",
                "enabled": 1,
                "charge_mode": "Percentage",
                "rate": 10,
                "min_pax": 0,
                "calculation_basis": "Post-Discount Subtotal",
                "account_head": "Service Charge Payable - TC",
                "description": "Post-Discount SC",
            }
        ])
        inv_post = self._make_invoice(name="INV-POST", additional_discount_percentage=20.0)
        self.pricing.sync_service_charge(inv_post, prof_post)
        inv_post.calculate_taxes_and_totals()
        sc_post = [t for t in inv_post.taxes if t.get("custom_pos_charge_kind") == "Service Charge"][0]
        self.assertEqual(sc_post.charge_type, "On Net Total")
        self.assertEqual(sc_post.tax_amount, 8000)  # 10% of 80,000 discounted net!

        # Simphony Option 17 Floor: switches Post-Discount % to Actual when computed < floor
        prof_floor = self._make_profile(rules=[
            {
                "order_type": "Dine In",
                "enabled": 1,
                "charge_mode": "Percentage",
                "rate": 10,
                "min_amount_floor": 15000,
                "min_pax": 0,
                "calculation_basis": "Post-Discount Subtotal",
                "account_head": "Service Charge Payable - TC",
            }
        ])
        inv_floor = self._make_invoice(name="INV-FLOOR")
        self.pricing.sync_service_charge(inv_floor, prof_floor)
        inv_floor.calculate_taxes_and_totals()
        sc_floor = [t for t in inv_floor.taxes if t.get("custom_pos_charge_kind") == "Service Charge"][0]
        self.assertEqual(sc_floor.charge_type, "Actual")
        self.assertEqual(sc_floor.tax_amount, 15000)

        # Floor never fires on a complimentary check
        inv_comp = self._make_invoice(name="INV-COMP", custom_is_complimentary=1)
        self.pricing.sync_service_charge(inv_comp, prof_floor)
        self.assertEqual([t for t in inv_comp.taxes if t.get("custom_pos_charge_kind") == "Service Charge"], [])

    def test_taxable_service_charge_chain_ordering_before_tip_and_tax_exempt_toggle(self):
        prof = self._make_profile(rules=[
            {
                "order_type": "Dine In",
                "enabled": 1,
                "charge_mode": "Percentage",
                "rate": 10,
                "min_pax": 0,
                "calculation_basis": "Post-Discount Subtotal",
                "taxable": 1,
                "account_head": "Service Charge Payable - TC",
                "description": "Service Charge",
            }
        ])
        inv = self._make_invoice(
            taxes=[
                {
                    "charge_type": "On Net Total",
                    "account_head": "Output VAT 18% - TC",
                    "rate": 18.0,
                    "description": "Output VAT 18%",
                    "included_in_print_rate": 0,
                },
                {
                    "charge_type": "Actual",
                    "account_head": "Tips Payable - TC",
                    "tax_amount": 5000,
                    "description": "Charged Tip",
                    "included_in_print_rate": 0,
                },
            ]
        )
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()

        # Order MUST be: [Item VAT (idx 1), Service Charge (idx 2), Service Charge Tax (idx 3, row_id="2"), Charged Tip (idx 4)]
        self.assertEqual(len(inv.taxes), 4)
        self.assertEqual(inv.taxes[0].charge_type, "On Net Total")  # item VAT untouched!
        self.assertEqual(inv.taxes[0].tax_amount, 18000)

        sc = inv.taxes[1]
        self.assertEqual(sc.idx, 2)
        self.assertEqual(sc.custom_pos_charge_kind, "Service Charge")
        self.assertEqual(sc.tax_amount, 10000)

        sc_tax = inv.taxes[2]
        self.assertEqual(sc_tax.idx, 3)
        self.assertEqual(sc_tax.custom_pos_charge_kind, "Service Charge Tax")
        self.assertEqual(sc_tax.charge_type, "On Previous Row Amount")
        self.assertEqual(str(sc_tax.row_id), "2")
        self.assertEqual(sc_tax.rate, 18.0)
        self.assertEqual(sc_tax.account_head, "Output VAT 18% - TC")
        self.assertEqual(sc_tax.tax_amount, 1800)  # 18% of 10,000

        tip = inv.taxes[3]
        self.assertEqual(tip.idx, 4)
        self.assertEqual(tip.custom_pos_charge_kind, "Charged Tip")
        self.assertEqual(tip.tax_amount, 5000)

        # Applying tax exemption removes Service Charge Tax while keeping Service Charge & Tip
        self.pricing.apply_tax_exemption(
            invoice=inv.name,
            pos_profile=prof.name,
            tax_category="Diplomatic Exempt",
            certificate_reference="DIP-001",
            reason="VIP_COMP",
        )
        kinds = [t.get("custom_pos_charge_kind") for t in inv.taxes]
        self.assertIn("Service Charge", kinds)
        self.assertNotIn("Service Charge Tax", kinds)
        self.assertIn("Charged Tip", kinds)

        # Clearing tax exemption restores Service Charge Tax cleanly
        self.pricing.clear_tax_exemption(invoice=inv.name, pos_profile=prof.name)
        kinds_after = [t.get("custom_pos_charge_kind") for t in inv.taxes]
        self.assertIn("Service Charge", kinds_after)
        self.assertIn("Service Charge Tax", kinds_after)
        self.assertIn("Charged Tip", kinds_after)

    def test_waive_stays_sticky_across_item_additions_until_restored(self):
        prof = self._make_profile(rules=[
            {
                "order_type": "Dine In",
                "enabled": 1,
                "charge_mode": "Percentage",
                "rate": 10,
                "min_pax": 0,
                "taxable": 1,
                "account_head": "Service Charge Payable - TC",
            }
        ])
        inv = self._make_invoice()
        self.pricing.sync_service_charge(inv, prof)
        self.assertEqual(len([t for t in inv.taxes if t.get("custom_pos_charge_kind") in ("Service Charge", "Service Charge Tax")]), 2)

        self.pricing.adjust_service_charge(invoice=inv.name, action="waive", reason="VIP_COMP")
        self.assertEqual(inv.custom_service_charge_waived, 1)
        self.assertEqual([t for t in inv.taxes if t.get("custom_pos_charge_kind") in ("Service Charge", "Service Charge Tax")], [])

        # Subsequent item addition + sync_service_charge must NOT re-apply SC
        inv.append("items", {"name": "L2", "item_code": "WINE", "qty": 1, "rate": 40000})
        self.pricing.sync_service_charge(inv, prof)
        self.assertEqual([t for t in inv.taxes if t.get("custom_pos_charge_kind") in ("Service Charge", "Service Charge Tax")], [])

        # Explicit restore re-applies SC + SC Tax
        self.pricing.adjust_service_charge(invoice=inv.name, action="restore")
        self.assertEqual(inv.custom_service_charge_waived, 0)
        self.assertEqual(len([t for t in inv.taxes if t.get("custom_pos_charge_kind") in ("Service Charge", "Service Charge Tax")]), 2)

    def test_partial_return_prorates_and_caps_actual_service_charge(self):
        prof = self._make_profile(rules=[
            {
                "order_type": "Room Service",
                "enabled": 1,
                "charge_mode": "Fixed Amount",
                "fixed_amount": 10000,
                "taxable": 1,
                "account_head": "Service Charge Income - TC",
                "description": "Tray Fee",
            }
        ])
        orig = self._make_invoice(
            name="INV-ORIG",
            docstatus=1,
            order_type="Room Service",
            items=[
                {"name": "L1", "item_code": "STEAK", "qty": 1, "rate": 60000},
                {"name": "L2", "item_code": "PASTA", "qty": 1, "rate": 40000},
            ],
        )
        self.pricing.sync_service_charge(orig, prof)
        orig.calculate_taxes_and_totals()

        # Partial return #1: returning 40,000 / 100,000 (40%) -> refunds 4,000 SC (NOT full 10,000!)
        ret1 = self._make_invoice(
            name="RET-1",
            docstatus=0,
            is_return=1,
            return_against="INV-ORIG",
            order_type="Room Service",
            items=[{"name": "RL1", "item_code": "PASTA", "qty": -1, "rate": 40000}],
            taxes=[
                {"charge_type": "On Net Total", "account_head": "Output VAT 18% - TC", "rate": 18.0, "description": "Output VAT 18%"},
                {"charge_type": "Actual", "account_head": "Service Charge Income - TC", "tax_amount": -10000, "description": "Tray Fee", "custom_pos_charge_kind": "Service Charge"},
                {"charge_type": "On Previous Row Amount", "row_id": "2", "account_head": "Output VAT 18% - TC", "rate": 18.0, "description": "Output VAT 18% on Tray Fee", "custom_pos_charge_kind": "Service Charge Tax"},
            ],
        )
        self.pricing.prorate_return_service_charge(ret1)
        sc1 = [t for t in ret1.taxes if t.get("custom_pos_charge_kind") == "Service Charge"][0]
        sc_tax1 = [t for t in ret1.taxes if t.get("custom_pos_charge_kind") == "Service Charge Tax"][0]
        self.assertEqual(sc1.tax_amount, -4000)
        self.assertEqual(sc_tax1.tax_amount, -720)
        ret1.docstatus = 1  # mark submitted

        # Partial return #2: returning remaining 60,000 / 100,000 (60%) -> refunds remaining 6,000 (total 10,000 = 100%)
        ret2 = self._make_invoice(
            name="RET-2",
            docstatus=0,
            is_return=1,
            return_against="INV-ORIG",
            order_type="Room Service",
            items=[{"name": "RL2", "item_code": "STEAK", "qty": -1, "rate": 60000}],
            taxes=[
                {"charge_type": "On Net Total", "account_head": "Output VAT 18% - TC", "rate": 18.0, "description": "Output VAT 18%"},
                {"charge_type": "Actual", "account_head": "Service Charge Income - TC", "tax_amount": -10000, "description": "Tray Fee", "custom_pos_charge_kind": "Service Charge"},
            ],
        )
        self.pricing.prorate_return_service_charge(ret2)
        sc2 = [t for t in ret2.taxes if t.get("custom_pos_charge_kind") == "Service Charge"][0]
        self.assertEqual(sc2.tax_amount, -6000)
        ret2.docstatus = 1

        # Partial return #3 attempting to refund more -> capped to 0!
        ret3 = self._make_invoice(
            name="RET-3",
            docstatus=0,
            is_return=1,
            return_against="INV-ORIG",
            order_type="Room Service",
            items=[{"name": "RL3", "item_code": "STEAK", "qty": -1, "rate": 60000}],
            taxes=[
                {"charge_type": "Actual", "account_head": "Service Charge Income - TC", "tax_amount": -10000, "description": "Tray Fee", "custom_pos_charge_kind": "Service Charge"},
            ],
        )
        self.pricing.prorate_return_service_charge(ret3)
        self.assertEqual(ret3.taxes[0].tax_amount, 0)

    def test_validate_blocks_direct_rest_stripping_of_service_charge_row(self):
        prof = self._make_profile(rules=[
            {"order_type": "Dine In", "enabled": 1, "charge_mode": "Percentage", "rate": 10, "min_pax": 0, "account_head": "Service Charge Payable - TC"},
        ])
        inv = self._make_invoice()
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()

        before = FakeDoc(inv.as_dict())
        before.set("items", [dict(r) for r in inv.get("items") or []])
        before.set("taxes", [dict(r) for r in inv.get("taxes") or []])
        inv._before_save = before

        # Direct REST edit tries to strip the Service Charge row without Manager PIN
        inv.set("taxes", [t for t in inv.taxes if t.get("custom_pos_charge_kind") != "Service Charge"])
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.pricing.validate_privileged_pricing_mutations(inv)

    def test_backward_compatible_scalar_fallback_still_works(self):
        prof = self._make_profile(
            rules=[],
            custom_service_charge_rate=10.0,
            custom_service_charge_account="Service Charge Payable - TC",
            custom_service_charge_min_pax=4,
            custom_service_charge_order_types="Dine In",
            custom_service_charge_description="Legacy SC",
        )
        inv = self._make_invoice(order_type="Dine In", no_of_pax=4)
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()
        sc_rows = [t for t in inv.taxes if t.get("custom_pos_charge_kind") == "Service Charge"]
        self.assertEqual(len(sc_rows), 1)
        self.assertEqual(sc_rows[0].rate, 10.0)
        self.assertEqual(sc_rows[0].tax_amount, 10000)

    def test_new_invoice_with_empty_taxes_loads_template_before_chaining_taxable_service_charge(self):
        """Live ERPNext sync_order creates a new POS Invoice with taxes=[] and taxes_and_charges set.

        sync_service_charge MUST hydrate base VAT rows from taxes_and_charges before appending
        Service Charge + Service Charge Tax so ERPNext set_taxes() is never suppressed and
        taxable SC chains cleanly via row_id.
        """
        prof = self._make_profile(rules=[
            {
                "order_type": "Dine In",
                "enabled": 1,
                "charge_mode": "Percentage",
                "rate": 10,
                "min_pax": 6,
                "calculation_basis": "Post-Discount Subtotal",
                "taxable": 1,
                "account_head": "Service Charge Payable - TC",
                "description": "Service Charge",
            }
        ])
        inv = self._make_invoice(
            name="INV-BRAND-NEW",
            taxes=[],
            taxes_and_charges="Standard VAT 18%",
            order_type="Dine In",
            no_of_pax=6,
        )
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()

        self.assertEqual(len(inv.taxes), 3, f"Expected [VAT, SC, SC Tax], got {inv.taxes}")
        self.assertEqual(inv.taxes[0].account_head, "Output VAT 18% - TC")
        self.assertEqual(inv.taxes[0].tax_amount, 18000)
        self.assertEqual(inv.taxes[1].custom_pos_charge_kind, "Service Charge")
        self.assertEqual(inv.taxes[1].tax_amount, 10000)
        self.assertEqual(inv.taxes[2].custom_pos_charge_kind, "Service Charge Tax")
        self.assertEqual(str(inv.taxes[2].row_id), "2")
        self.assertEqual(inv.taxes[2].tax_amount, 1800)
        self.assertEqual(inv.grand_total, 129800)

    def test_mariadb_zero_default_override_fields_never_trigger_false_override(self):
        """Real MariaDB DECIMAL(21,9) NOT NULL DEFAULT 0.0 loads override fields as 0.0 on saved docs.

        sync_service_charge MUST treat 0.0 override_rate / override_amount as unset (not as an
        active 0% override!) so second saves preserve the matched rule and order-type switches
        clear Service Charge rows completely.
        """
        prof = self._make_profile(rules=[
            {
                "order_type": "Dine In",
                "enabled": 1,
                "charge_mode": "Percentage",
                "rate": 10,
                "min_pax": 6,
                "calculation_basis": "Post-Discount Subtotal",
                "taxable": 1,
                "account_head": "Service Charge Payable - TC",
                "description": "Service Charge",
            }
        ])
        inv = self._make_invoice(
            name="INV-MARIADB-ZERO",
            order_type="Dine In",
            no_of_pax=6,
            custom_service_charge_override_rate=0.0,
            custom_service_charge_override_amount=0.0,
        )
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()

        sc_rows = [t for t in inv.taxes if t.get("custom_pos_charge_kind") == "Service Charge"]
        self.assertEqual(len(sc_rows), 1)
        self.assertEqual(sc_rows[0].rate, 10.0)
        self.assertEqual(sc_rows[0].tax_amount, 10000)

        status = self.pricing.get_service_charge_status(inv.name, prof.name)
        self.assertIsNone(status["override_rate"])
        self.assertIsNone(status["override_amount"])

        # Switching to Take Away (no rule) must clear SC & SC Tax completely even with 0.0 DB columns
        inv.order_type = "Take Away"
        self.pricing.sync_service_charge(inv, prof)
        inv.calculate_taxes_and_totals()
        self.assertEqual(
            [t for t in inv.taxes if t.get("custom_pos_charge_kind") in ("Service Charge", "Service Charge Tax")],
            [],
        )

        # Explicit change to 0 rate/amount behaves as a clean waive (no phantom 0% row)
        inv.order_type = "Dine In"
        self.pricing.sync_service_charge(inv, prof)
        self.pricing.adjust_service_charge(invoice=inv.name, action="change", new_rate=0.0, reason="VIP_COMP")
        self.assertEqual(inv.custom_service_charge_waived, 1)
        self.assertEqual(
            [t for t in inv.taxes if t.get("custom_pos_charge_kind") in ("Service Charge", "Service Charge Tax")],
            [],
        )


class TestServiceChargeLifecycleAndPrintTemplates(unittest.TestCase):
    """Static + contract checks for unconditional lifecycle triggers, API payload, and print formats."""

    def test_ury_order_and_floor_actions_call_sync_service_charge_unconditionally(self):
        ury_order_src = (ROOT / "ury/ury/doctype/ury_order/ury_order.py").read_text()
        floor_src = (ROOT / "ury/ury_pos/floor_actions.py").read_text()

        # Must NOT gate sync_service_charge on legacy scalar custom_service_charge_rate > 0
        self.assertNotIn(
            'if flt(posprofile.get("custom_service_charge_rate")) > 0',
            ury_order_src,
            "sync_order still gates sync_service_charge on legacy scalar rate > 0!",
        )
        self.assertNotIn(
            'if flt(target_profile.get("custom_service_charge_rate")) > 0',
            floor_src,
            "transfer_check_outlet still gates sync_service_charge on legacy scalar rate > 0!",
        )

        # All required lifecycle entry points call sync_service_charge
        for fn_name in ("def sync_order(", "def make_invoice(", "def table_transfer(", "def split_bill(", "def consolidate_checks("):
            idx = ury_order_src.index(fn_name)
            next_def = ury_order_src.find("\ndef ", idx + 10)
            body = ury_order_src[idx: next_def if next_def != -1 else len(ury_order_src)]
            self.assertIn(
                "sync_service_charge",
                body,
                f"{fn_name} in ury_order.py does not call sync_service_charge!",
            )

    def test_print_formats_classify_service_charge_by_custom_pos_charge_kind(self):
        for rel in (
            "ury/ury/print_format/merged_pos_invoice_format/merged_pos_invoice_format.json",
            "ury/ury/print_format/ury_bill_copy/ury_bill_copy.json",
        ):
            data = json.loads((ROOT / rel).read_text())
            html = data["html"]
            self.assertIn(
                "custom_pos_charge_kind",
                html,
                f"{rel} must inspect custom_pos_charge_kind before falling back to description substring!",
            )
            self.assertIn("Service Charge Tax", html, f"{rel} must keep Service Charge Tax in tax_rows!")

    def test_api_exposes_rules_and_tax_charge_kind(self):
        api_src = (ROOT / "ury/ury_pos/api.py").read_text()
        self.assertIn("custom_service_charge_rules", api_src)
        self.assertIn("custom_pos_charge_kind", api_src)


if __name__ == "__main__":
    unittest.main()
