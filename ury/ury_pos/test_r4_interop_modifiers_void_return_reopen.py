"""Site-less RED/GREEN tests for POS-PR-8 R4 (#150 x #156 interoperability).

Covers all 3 Hydrogen R3 blocking P1 defects:
1. P1 #1: `reopen_closed_check` and `adjust_closed_check` preserve `#156` modifier/bundle
   fields and `#152` privileged pricing overrides across `insert`/`validate`/`submit`
   (including `custom_bundle_overrides_json` on the reversal return), reconcile `packed_items`,
   clear transient bypass flags in `finally`, and continue blocking forged saves with
   `[POS-AUTH-REQUIRED]` / `[POS-MODIFIER-RATE-MISMATCH]`.
2. P1 #2: `_expand_stock_waste_items`, `reduce_order_item_qty`, `ury_order.sync_order`, and
   `ury_order.cancel_order` resolve `custom_bundle_overrides_json` via `ury_bundle_resolver`
   and include proportionally scaled linked stock child modifiers (excluding billing-only
   `Combo Upcharge` rows) so Waste Stock Entries match Cancel KOTs.
3. P1 #3: `get_returnable_check`, `preview_pos_return`, `create_pos_return`, and
   `validate_pos_return_doc` expose and preserve `custom_bundle_overrides_json` and
   parent-child modifier linkage (reconciling return `packed_items` to delivered substitutes)
   and proportionally include / enforce linked child modifiers on partial parent returns so
   linked priced modifiers can never be orphaned.
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


class Row(dict):
    """Dict + attribute row double matching Frappe child table rows."""

    def __getattr__(self, name):
        if name in self:
            return self[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self[name] = value

    def as_dict(self, **kwargs):
        return dict(self)


class Doc:
    """Lightweight Frappe Document double without dict.items() shadowing."""

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

    def set(self, field, value):
        self._d[field] = value

    def append(self, field, row):
        entry = Row(row) if isinstance(row, dict) and not isinstance(row, Row) else row
        self._d.setdefault(field, []).append(entry)
        return entry

    def remove(self, row):
        for field in ("items", "packed_items", "taxes", "payments"):
            lst = self._d.get(field)
            if isinstance(lst, list) and row in lst:
                lst.remove(row)
                return

    def as_dict(self, **kwargs):
        return dict(self._d)

    def is_new(self):
        return not bool(self._d.get("name")) or bool(self._d.get("_is_new"))

    def precision(self, fieldname=None):
        return 0 if (self._d.get("currency") or "UGX") == "UGX" else 2

    def get_doc_before_save(self):
        return self._d.get("_before_save")


def _load_fresh(mod_name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_module(name: str, **attrs) -> ModuleType:
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


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
        for row in self._h.reasons:
            if int(row.get("disabled") or 0):
                continue
            targets = [p.strip() for p in str(row.get("applies_to") or "").replace(",", "\n").splitlines() if p.strip()]
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
            if r.get("role") in roles and int(r.get("may_perform") or 0) == 1:
                return True
        return False

    def require(self, privilege, pos_profile, reference_name, claim, *, token=None, percent=None, amount=None, privilege_group=0):
        if self.may_perform(self._h.frappe.session.user, pos_profile, privilege, percent=percent, amount=amount):
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


class InteropHarness:
    """In-memory Frappe environment wiring #150 void/return/reopen + #156 modifiers/bundles + #152 pricing guard."""

    def __init__(self):
        self.user_roles = {
            "cashier@example.test": ["URY Cashier"],
            "waiter1@example.test": ["URY Captain"],
            "manager@example.test": ["URY Manager", "URY Cashier"],
            "manager2@example.test": ["URY Manager", "URY Cashier"],
            "Administrator": ["System Manager", "URY Admin"],
        }
        self.user_branch = {
            "cashier@example.test": "Main Branch",
            "waiter1@example.test": "Main Branch",
            "manager@example.test": "Main Branch",
            "manager2@example.test": "Main Branch",
            "Administrator": "Main Branch",
        }
        self.profiles = {
            "Main Till": Doc(
                name="Main Till",
                company="Maximus Hospitality",
                currency="UGX",
                branch="Main Branch",
                warehouse="Kitchen Store - MH",
                cost_center="Main - MH",
                selling_price_list="Standard Selling",
                custom_kot_naming_series="KOT-.#####",
                custom_qty_reduction_allowed_order_types="Dine In,Take Away",
                role_allowed_for_billing=[Row(role="URY Cashier"), Row(role="URY Manager")],
                transfer_role_permissions=[Row(role="URY Manager")],
                role_restricted_for_table_order=[],
                applicable_for_users=[
                    Row(user="cashier@example.test"),
                    Row(user="manager@example.test"),
                    Row(user="manager2@example.test"),
                ],
                payments=[
                    Row(mode_of_payment="Cash", default=1),
                    Row(mode_of_payment="Card", default=0),
                ],
                ury_enable_hold_fire=1,
                remove_items=0,
            )
        }
        self.mops = {
            "Cash": Doc(name="Cash", type="Cash"),
            "Card": Doc(name="Card", type="Bank"),
        }
        self.tables = {"T1": Doc(name="T1", occupied=0)}
        self.open_shifts = {"Main Till": "POS-OPEN-1"}
        self.items_master = {
            "RIBEYE": Row(name="RIBEYE", item_code="RIBEYE", item_name="Ribeye Steak", item_group="Mains", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "EXTRA-CHEESE": Row(name="EXTRA-CHEESE", item_code="EXTRA-CHEESE", item_name="Aged Cheddar", item_group="Modifiers", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "EXTRA-BACON": Row(name="EXTRA-BACON", item_code="EXTRA-BACON", item_name="Smoked Bacon", item_group="Modifiers", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "BURGER-COMBO": Row(name="BURGER-COMBO", item_code="BURGER-COMBO", item_name="Burger Combo Meal", item_group="Combos", is_stock_item=0, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "CLASSIC-BURGER": Row(name="CLASSIC-BURGER", item_code="CLASSIC-BURGER", item_name="Classic Burger", item_group="Mains", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "FRIES": Row(name="FRIES", item_code="FRIES", item_name="French Fries", item_group="Sides", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "ONION-RINGS": Row(name="ONION-RINGS", item_code="ONION-RINGS", item_name="Onion Rings", item_group="Sides", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "COLA": Row(name="COLA", item_code="COLA", item_name="Cola", item_group="Beverages", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "MANGO-JUICE": Row(name="MANGO-JUICE", item_code="MANGO-JUICE", item_name="Fresh Mango Juice", item_group="Beverages", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "COMBO-UPGRADE-FEE": Row(name="COMBO-UPGRADE-FEE", item_code="COMBO-UPGRADE-FEE", item_name="Side Upgrade Fee", item_group="Services", is_stock_item=0, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "COMBO-DRINK-FEE": Row(name="COMBO-DRINK-FEE", item_code="COMBO-DRINK-FEE", item_name="Drink Upgrade Fee", item_group="Services", is_stock_item=0, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
            "SOUP": Row(name="SOUP", item_code="SOUP", item_name="Tomato Soup", item_group="Starters", is_stock_item=1, stock_uom="Nos", disabled=0, custom_is_open_priced=0),
        }
        self.item_prices = [
            Row(name="IP-RIB", item_code="RIBEYE", price_list="Standard Selling", price_list_rate=45000.0, currency="UGX", selling=1, valid_from="2026-01-01", valid_upto=None, customer=None),
            Row(name="IP-CHZ", item_code="EXTRA-CHEESE", price_list="Standard Selling", price_list_rate=4000.0, currency="UGX", selling=1, valid_from="2026-01-01", valid_upto=None, customer=None),
            Row(name="IP-BCN", item_code="EXTRA-BACON", price_list="Standard Selling", price_list_rate=5000.0, currency="UGX", selling=1, valid_from="2026-01-01", valid_upto=None, customer=None),
            Row(name="IP-CMB", item_code="BURGER-COMBO", price_list="Standard Selling", price_list_rate=25000.0, currency="UGX", selling=1, valid_from="2026-01-01", valid_upto=None, customer=None),
            Row(name="IP-UF1", item_code="COMBO-UPGRADE-FEE", price_list="Standard Selling", price_list_rate=3000.0, currency="UGX", selling=1, valid_from="2026-01-01", valid_upto=None, customer=None),
            Row(name="IP-UF2", item_code="COMBO-DRINK-FEE", price_list="Standard Selling", price_list_rate=2000.0, currency="UGX", selling=1, valid_from="2026-01-01", valid_upto=None, customer=None),
            Row(name="IP-SOUP", item_code="SOUP", price_list="Standard Selling", price_list_rate=15000.0, currency="UGX", selling=1, valid_from="2026-01-01", valid_upto=None, customer=None),
        ]
        self.bundles = {
            "BURGER-COMBO": [
                Row(parent="BURGER-COMBO", parenttype="Product Bundle", item_code="CLASSIC-BURGER", qty=1.0, uom="Nos", description="Classic Burger", idx=1),
                Row(parent="BURGER-COMBO", parenttype="Product Bundle", item_code="FRIES", qty=1.0, uom="Nos", description="French Fries", idx=2),
                Row(parent="BURGER-COMBO", parenttype="Product Bundle", item_code="COLA", qty=1.0, uom="Nos", description="Cola", idx=3),
            ]
        }
        self.modifier_groups = {
            "Cooking Temperature": Row(
                name="Cooking Temperature",
                group_name="Cooking Temperature",
                display_title="Cooking Temperature",
                group_type="Condiment",
                required=1,
                min_selections=1,
                max_selections=1,
                allow_prefixes=0,
                allowed_prefixes="",
                inherit_parent_routing=1,
                hide_unpriced_on_receipt=1,
                disabled=0,
            ),
            "Steak Extras": Row(
                name="Steak Extras",
                group_name="Steak Extras",
                display_title="Steak Extras",
                group_type="Condiment",
                required=0,
                min_selections=0,
                max_selections=2,
                allow_prefixes=1,
                allowed_prefixes="Regular,Extra,No,Light,On Side",
                inherit_parent_routing=1,
                hide_unpriced_on_receipt=0,
                disabled=0,
            ),
            "Burger Side": Row(
                name="Burger Side",
                group_name="Burger Side",
                display_title="Burger Side",
                group_type="Combo Choice",
                required=1,
                min_selections=1,
                max_selections=1,
                allow_prefixes=0,
                allowed_prefixes="",
                inherit_parent_routing=1,
                hide_unpriced_on_receipt=0,
                disabled=0,
            ),
            "Burger Drink": Row(
                name="Burger Drink",
                group_name="Burger Drink",
                display_title="Burger Drink",
                group_type="Combo Choice",
                required=1,
                min_selections=1,
                max_selections=1,
                allow_prefixes=0,
                allowed_prefixes="",
                inherit_parent_routing=0,
                hide_unpriced_on_receipt=0,
                disabled=0,
            ),
        }
        self.modifier_options = [
            Row(parent="Cooking Temperature", option_code="MED-RARE", label="Medium Rare", item="", price_mode="Unpriced", upcharge_amount=0, upcharge_item="", replaces_bundle_item="", qty=1, is_default=0, sort_order=1, disabled=0),
            Row(parent="Steak Extras", option_code="EXTRA-CHEESE", label="Aged Cheddar", item="EXTRA-CHEESE", price_mode="Menu Item Price", upcharge_amount=0, upcharge_item="", replaces_bundle_item="", qty=1, is_default=0, sort_order=1, disabled=0),
            Row(parent="Steak Extras", option_code="EXTRA-BACON", label="Smoked Bacon", item="EXTRA-BACON", price_mode="Menu Item Price", upcharge_amount=0, upcharge_item="", replaces_bundle_item="", qty=1, is_default=0, sort_order=2, disabled=0),
            Row(parent="Burger Side", option_code="FRIES", label="French Fries", item="FRIES", price_mode="Unpriced", upcharge_amount=0, upcharge_item="", replaces_bundle_item="FRIES", qty=1, is_default=1, sort_order=1, disabled=0),
            Row(parent="Burger Side", option_code="ONION-RINGS", label="Onion Rings", item="ONION-RINGS", price_mode="Combo Upcharge", upcharge_amount=3000, upcharge_item="COMBO-UPGRADE-FEE", replaces_bundle_item="FRIES", qty=1, is_default=0, sort_order=2, disabled=0),
            Row(parent="Burger Drink", option_code="COLA", label="Cola", item="COLA", price_mode="Unpriced", upcharge_amount=0, upcharge_item="", replaces_bundle_item="COLA", qty=1, is_default=1, sort_order=1, disabled=0),
            Row(parent="Burger Drink", option_code="MANGO-JUICE", label="Fresh Mango Juice", item="MANGO-JUICE", price_mode="Combo Upcharge", upcharge_amount=2000, upcharge_item="COMBO-DRINK-FEE", replaces_bundle_item="COLA", qty=1, is_default=0, sort_order=2, disabled=0),
        ]
        self.item_modifier_links = [
            Row(parenttype="Item", parent="RIBEYE", modifier_group="Cooking Temperature", required_override=0, min_override=0, max_override=0, sort_order=1),
            Row(parenttype="Item", parent="RIBEYE", modifier_group="Steak Extras", required_override=0, min_override=0, max_override=0, sort_order=2),
            Row(parenttype="Item", parent="BURGER-COMBO", modifier_group="Burger Side", required_override=0, min_override=0, max_override=0, sort_order=1),
            Row(parenttype="Item", parent="BURGER-COMBO", modifier_group="Burger Drink", required_override=0, min_override=0, max_override=0, sort_order=2),
        ]
        self.privilege_rules = [
            Row(company="Maximus Hospitality", pos_profile="", privilege=p, role="URY Manager", may_perform=1, may_authorise=1, requires_reason=1)
            for p in ("void_sent_item", "reduce_sent_qty", "cancel_check", "return", "reopen_check", "adjust_closed_check")
        ]
        self.reasons = [
            Row(code="WASTE-SPOILED", description="Spoiled or dropped item (waste)", applies_to="void_sent_item\nreduce_sent_qty\ncancel_check", requires_note=0, sort_order=10, disabled=0),
            Row(code="RET-QUALITY", description="Item quality complaint", applies_to="return", requires_note=0, sort_order=20, disabled=0),
            Row(code="REOPEN-TENDER", description="Wrong payment method", applies_to="reopen_check\nadjust_closed_check", requires_note=0, sort_order=30, disabled=0),
        ]
        self.stock_entry_types = {"Wastage": "Material Issue", "Material Issue": "Material Issue"}
        self.wastage_accounts = ["Wastage and Spoilage - MH"]
        self.invoices: dict[str, Doc] = {}
        self.kots: list[Doc] = []
        self.stock_entries: list[Doc] = []
        self.auth_logs: list[Doc] = []
        self.comments: list[dict] = []
        self.on_validate_invoice = None
        self.on_before_submit_invoice = None
        self.custom_fields_present = {
            ("Stock Entry", "custom_wastage_reason"),
            ("URY KOT", "custom_waste_stock_entry"),
            ("URY KOT", "custom_void_reason_code"),
            ("POS Invoice", "custom_waste_stock_entry"),
            ("POS Invoice", "custom_reopened_from"),
            ("POS Invoice", "custom_return_approver"),
            ("POS Invoice", "cancel_reason"),
            ("POS Invoice", "custom_charged_tip"),
            ("POS Invoice", "custom_tip_server"),
            ("POS Invoice Item", "custom_bundle_overrides_json"),
            ("POS Invoice Item", "custom_modifiers_json"),
            ("POS Invoice Item", "custom_parent_line_key"),
            ("POS Invoice Item", "custom_is_modifier"),
        }
        self.frappe = self._build_frappe()
        self.override = ContractOverrideStub(self)

    def make_combo_overrides_json(self) -> str:
        return json.dumps([
            {
                "group": "Burger Side",
                "slot_item": "FRIES",
                "chosen_item": "ONION-RINGS",
                "option_code": "ONION-RINGS",
                "label": "Onion Rings",
                "qty": 1,
                "inherit_parent_routing": 1,
                "hide_on_receipt": 0,
            },
            {
                "group": "Burger Drink",
                "slot_item": "COLA",
                "chosen_item": "MANGO-JUICE",
                "option_code": "MANGO-JUICE",
                "label": "Fresh Mango Juice",
                "qty": 1,
                "inherit_parent_routing": 0,
                "hide_on_receipt": 0,
            },
        ])

    def _populate_default_packed_items(self, doc: Doc) -> None:
        """Simulate ERPNext make_packing_list() populating default Product Bundle packed_items."""
        doc.packed_items = []
        for idx, row in enumerate(doc.get("items") or [], start=1):
            if not row.get("name"):
                row["name"] = f"PII-{doc.get('name') or 'NEW'}-{idx}"
            code = row.get("item_code")
            if code in self.bundles:
                for b in self.bundles[code]:
                    doc.append(
                        "packed_items",
                        Row(
                            parent_item=code,
                            parent_detail_docname=row["name"],
                            item_code=b["item_code"],
                            item_name=self.items_master[b["item_code"]]["item_name"],
                            qty=float(row.get("qty") or 0) * float(b["qty"]),
                            uom=b["uom"],
                            warehouse=row.get("warehouse") or "Kitchen Store - MH",
                        ),
                    )

    def _build_frappe(self):
        h = self
        fw = ModuleType("frappe")
        fw.ValidationError = type("ValidationError", (Exception,), {})
        fw.PermissionError = type("PermissionError", (Exception,), {})
        fw.DoesNotExistError = type("DoesNotExistError", (Exception,), {})
        fw.UniqueValidationError = type("UniqueValidationError", (Exception,), {})
        fw._ = lambda msg: msg
        fw._dict = lambda d=None, **kw: Row(d or {}, **kw)
        fw.whitelist = lambda *a, **kw: (lambda fn: fn)
        fw.session = SimpleNamespace(user="manager2@example.test")
        fw.flags = SimpleNamespace()
        fw.form_dict = {}

        def _throw(msg, exc=None, **kw):
            raise (exc or fw.ValidationError)(msg)

        fw.throw = _throw
        fw.get_roles = lambda user=None: list(h.user_roles.get(user or fw.session.user, []))
        fw.has_permission = lambda *a, **kw: True
        fw.get_precision = lambda *a, **kw: 0
        fw.publish_realtime = MagicMock()
        fw.log_error = MagicMock()

        def _get_meta(dt):
            m = MagicMock()
            m.has_field = lambda fn: (dt, fn) in h.custom_fields_present
            m.get_field = lambda fn: SimpleNamespace(length=140, options="") if fn == "cancel_reason" else None
            return m

        fw.get_meta = _get_meta

        def _table_exists(dt):
            return dt in {
                "POS Reason", "POS Privilege Rule", "POS Authorisation Log",
                "POS Invoice", "POS Opening Entry", "POS Closing Entry",
                "POS Closing Entry Detail", "POS Invoice Merge Log",
                "POS Invoice Reference", "URY Table", "URY Modifier Group",
                "URY Modifier Option", "URY Item Modifier Group",
                "URY Item Availability", "Stock Entry", "Stock Entry Type",
                "Product Bundle", "Product Bundle Item", "Item", "Item Price",
                "URY KOT",
            }

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
            if dt == "Stock Entry Type":
                return filt if isinstance(filt, str) and filt in h.stock_entry_types else None
            if dt == "Product Bundle":
                if isinstance(filt, dict):
                    code = filt.get("new_item_code")
                    return code if code in h.bundles else None
                return filt if isinstance(filt, str) and filt in h.bundles else None
            if dt == "Item":
                return filt if isinstance(filt, str) and filt in h.items_master else None
            if dt == "Mode of Payment":
                return filt if isinstance(filt, str) and filt in h.mops else None
            return None

        def _get_value(dt, filters=None, fieldname=None, as_dict=False, **kw):
            if dt == "Company" and isinstance(filters, str):
                vals = {
                    "default_currency": "UGX",
                    "stock_adjustment_account": "Stock Adjustment - MH",
                    "cost_center": "Main - MH",
                }
                return vals.get(fieldname) if isinstance(fieldname, str) else Row(vals)
            if dt == "Branch" and isinstance(filters, str):
                if fieldname == "company":
                    return "Maximus Hospitality"
                return Row(company="Maximus Hospitality")
            if dt == "POS Profile" and isinstance(filters, str):
                p = h.profiles.get(filters)
                if not p:
                    return None
                return p.get(fieldname) if isinstance(fieldname, str) else p
            if dt == "Price List" and fieldname == "currency":
                return "UGX"
            if dt == "Currency" and fieldname == "fraction_units":
                return 0
            if dt == "Mode of Payment" and isinstance(filters, str):
                m = h.mops.get(filters)
                return m.get(fieldname) if m and isinstance(fieldname, str) else None
            if dt == "URY Table" and isinstance(filters, str):
                t = h.tables.get(filters)
                return t.get(fieldname) if t and isinstance(fieldname, str) else None
            if dt == "Product Bundle":
                code = filters.get("new_item_code") if isinstance(filters, dict) else filters
                return code if code in h.bundles else None
            if dt == "Item" and isinstance(filters, str):
                it = h.items_master.get(filters)
                if not it:
                    return None
                if isinstance(fieldname, str):
                    return it.get(fieldname)
                if isinstance(fieldname, (list, tuple)):
                    return Row({f: it.get(f) for f in fieldname}) if as_dict else tuple(it.get(f) for f in fieldname)
                return it
            if dt == "Item Price" and isinstance(filters, dict):
                for ip in h.item_prices:
                    if ip["item_code"] == filters.get("item_code") and ip["price_list"] == filters.get("price_list"):
                        return ip.get(fieldname) if isinstance(fieldname, str) else ip
                return None
            if dt == "POS Invoice" and isinstance(filters, str):
                inv = h.invoices.get(filters)
                if not inv:
                    return None
                return inv.get(fieldname) if isinstance(fieldname, str) else inv
            return None

        def _get_all(dt, filters=None, fields=None, pluck=None, order_by=None, **kw):
            if dt == "Product Bundle Item":
                parent = (filters or {}).get("parent") if isinstance(filters, dict) else None
                rows = list(h.bundles.get(parent, []))
                return [r.get(pluck) for r in rows] if pluck else rows
            if dt == "Item":
                rows = list(h.items_master.values())
                if isinstance(filters, dict) and "name" in filters:
                    fval = filters["name"]
                    if isinstance(fval, list) and fval[0] == "in":
                        rows = [r for r in rows if r["name"] in fval[1]]
                    else:
                        rows = [r for r in rows if r["name"] == fval]
                return [r.get(pluck) for r in rows] if pluck else rows
            if dt == "Item Price":
                rows = list(h.item_prices)
                if isinstance(filters, dict):
                    if "item_code" in filters:
                        v = filters["item_code"]
                        if isinstance(v, list) and v[0] == "in":
                            rows = [r for r in rows if r["item_code"] in v[1]]
                        else:
                            rows = [r for r in rows if r["item_code"] == v]
                    if "price_list" in filters:
                        rows = [r for r in rows if r["price_list"] == filters["price_list"]]
                return [r.get(pluck) for r in rows] if pluck else rows
            if dt == "URY Item Modifier Group":
                rows = list(h.item_modifier_links)
                if isinstance(filters, dict):
                    if "parenttype" in filters:
                        rows = [r for r in rows if r["parenttype"] == filters["parenttype"]]
                    if "parent" in filters:
                        v = filters["parent"]
                        if isinstance(v, list) and v[0] == "in":
                            rows = [r for r in rows if r["parent"] in v[1]]
                        else:
                            rows = [r for r in rows if r["parent"] == v]
                return rows
            if dt == "URY Modifier Group":
                rows = list(h.modifier_groups.values())
                if isinstance(filters, dict) and "name" in filters:
                    v = filters["name"]
                    if isinstance(v, list) and v[0] == "in":
                        rows = [r for r in rows if r["name"] in v[1]]
                return rows
            if dt == "URY Modifier Option":
                rows = list(h.modifier_options)
                if isinstance(filters, dict) and "parent" in filters:
                    v = filters["parent"]
                    if isinstance(v, list) and v[0] == "in":
                        rows = [r for r in rows if r["parent"] in v[1]]
                return rows
            if dt == "Account":
                if isinstance(filters, dict) and filters.get("account_name") == "Wastage and Spoilage":
                    return list(h.wastage_accounts)
                return []
            if dt == "POS Reason":
                return [r for r in h.reasons if int(r.get("disabled") or 0) == 0]
            if dt == "POS Invoice":
                out = []
                for inv in h.invoices.values():
                    if isinstance(filters, dict) and not all(inv.get(k) == v for k, v in filters.items()):
                        continue
                    out.append(inv.get(pluck) if pluck else inv)
                return out
            if dt == "URY KOT":
                return [k.get(pluck) if pluck else k for k in h.kots]
            return []

        def _sql(query, params=None, as_dict=False):
            q = " ".join(str(query).lower().split())
            orig_name = params[0] if isinstance(params, (list, tuple)) and params else params
            if "from `tabpos invoice`" in q and "return_against" in q:
                matches = [
                    inv for inv in h.invoices.values()
                    if inv.get("return_against") == orig_name
                    and int(inv.get("is_return") or 0) == 1
                    and int(inv.get("docstatus") or 0) == 1
                ]
                return [{"name": m.name} for m in matches] if as_dict else [(m.name,) for m in matches]
            return []

        def _wrap_pos_invoice(d: Doc):
            d._is_new = True
            d.docstatus = 0
            d.setdefault("items", [])
            d.setdefault("packed_items", [])
            d.setdefault("taxes", [])
            d.setdefault("payments", [])

            def _insert(ignore_permissions=False):
                prefix = "POS-REV-" if int(d.get("is_return") or 0) == 1 else "POS-REP-"
                d.name = d.get("name") or f"{prefix}{len(h.invoices) + 1}"
                h._populate_default_packed_items(d)
                if callable(h.on_validate_invoice):
                    h.on_validate_invoice(d)
                d._is_new = False
                gross = sum(float(r.get("qty") or 0) * float(r.get("rate") or 0) for r in d.items)
                disc = float(d.get("discount_amount") or 0)
                excl = sum(float(t.get("tax_amount") or 0) for t in d.taxes if int(t.get("included_in_print_rate") or 0) == 0)
                d.total = gross
                d.net_total = gross - disc
                d.total_taxes_and_charges = excl
                d.grand_total = d.net_total + excl
                d.rounded_total = d.grand_total
                d.paid_amount = sum(float(p.get("amount") or 0) for p in d.payments)
                d.add_comment = lambda ctype, text: h.comments.append({"invoice": d.name, "type": ctype, "text": text})
                h.invoices[d.name] = d
                return d

            def _save(ignore_permissions=False):
                h._populate_default_packed_items(d)
                if callable(h.on_validate_invoice):
                    h.on_validate_invoice(d)
                h.invoices[d.name] = d
                return d

            def _submit():
                if callable(h.on_before_submit_invoice):
                    h.on_before_submit_invoice(d)
                d.docstatus = 1
                d.status = "Return" if int(d.get("is_return") or 0) == 1 else "Paid"
                return d

            d.insert = _insert
            d.save = _save
            d.submit = _submit
            return d

        def _wrap_stock_entry(d: Doc):
            d.setdefault("items", [])

            def _insert(ignore_permissions=False):
                d.name = f"MAT-STE-2026-{len(h.stock_entries) + 1:05d}"
                h.stock_entries.append(d)
                return d

            def _submit():
                d.docstatus = 1
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
                if d.get("doctype") == "Stock Entry":
                    return _wrap_stock_entry(d)
                return _wrap_pos_invoice(d)
            if dt == "POS Invoice":
                if name not in h.invoices:
                    raise fw.DoesNotExistError(name)
                return h.invoices[name]
            if dt == "POS Profile":
                if name not in h.profiles:
                    raise fw.DoesNotExistError(name)
                return h.profiles[name]
            raise fw.DoesNotExistError(f"{dt}:{name}")

        def _new_doc(dt):
            if dt == "POS Invoice":
                return _wrap_pos_invoice(Doc(doctype="POS Invoice"))
            if dt == "Stock Entry":
                return _wrap_stock_entry(Doc(doctype="Stock Entry"))
            raise NotImplementedError(dt)

        def _set_value(dt, name, field, value=None, **kw):
            if dt == "URY Table" and name in h.tables:
                if isinstance(field, dict):
                    h.tables[name]._d.update(field)
                else:
                    h.tables[name][field] = value

        fw.db = SimpleNamespace(
            exists=_exists,
            table_exists=_table_exists,
            get_value=_get_value,
            get_list=_get_all,
            set_value=_set_value,
            sql=_sql,
            has_column=lambda dt, fn: False,
        )
        fw.get_doc = _get_doc
        fw.get_cached_doc = _get_doc
        fw.get_cached_value = _get_value
        fw.new_doc = _new_doc
        fw.get_all = _get_all
        fw.cache = lambda: SimpleNamespace(get_value=lambda *a, **k: None, set_value=lambda *a, **k: None, delete_value=lambda *a, **k: None)
        return fw


class TestR4InteropModifiersVoidReturnReopen(unittest.TestCase):
    def setUp(self):
        self.h = InteropHarness()
        self.utils = ModuleType("frappe.utils")
        self.utils.cint = lambda v: int(v or 0)
        self.utils.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
        self.utils.now_datetime = lambda: datetime(2026, 11, 25, 10, 0, 0)
        self.utils.nowdate = lambda: "2026-11-25"
        self.utils.today = lambda: "2026-11-25"
        self.utils.nowtime = lambda: "10:00:00"
        self.utils.now = lambda: "2026-11-25 10:00:00"
        self.utils.get_time = lambda v=None: v
        self.utils.get_first_day = lambda v=None: v
        self.utils.get_last_day = lambda v=None: v
        self.utils.get_datetime = lambda v=None: (
            v if isinstance(v, datetime) else datetime.strptime(str(v or "2026-11-25 10:00:00")[:19], "%Y-%m-%d %H:%M:%S")
        )
        self.utils.getdate = lambda v=None: datetime.strptime(str(v or "2026-11-25")[:10], "%Y-%m-%d").date()
        self.utils.get_system_timezone = lambda: "Africa/Kampala"
        self.h.frappe.utils = self.utils
        self.h.frappe.__path__ = []

        self.pos_api = _stub_module(
            "ury.ury_pos.api",
            getBranch=lambda: self.h.user_branch.get(self.h.frappe.session.user, "Main Branch"),
            getBranchRoom=MagicMock(return_value=None),
            getRoom=MagicMock(return_value=None),
            posOpening=MagicMock(return_value=None),
            getRestaurantMenu=MagicMock(return_value="Main Menu"),
            getOrderInvoice=MagicMock(),
            getPosProfile=MagicMock(return_value={"pos_profile": "Main Till"}),
            get_branch_cashier_roles=MagicMock(return_value=["URY Cashier", "URY Manager"]),
        )
        self.base_modules = {
            "frappe": self.h.frappe,
            "frappe.utils": self.utils,
            "ury.ury_pos.override": self.h.override,
            "ury.ury_pos.api": self.pos_api,
            "ury.ury_pos.privileged_auth": _stub_module(
                "ury.ury_pos.privileged_auth",
                DEFAULT_MANAGER_ROLES=("URY Manager", "URY Admin"),
                assert_not_efris_fiscalised=MagicMock(),
                assert_not_offline_replay=MagicMock(),
                authorize_or_perform=MagicMock(),
                is_company_policy_active=MagicMock(return_value=False),
                is_valid_str=lambda v: isinstance(v, str) and bool(v.strip()),
            ),
            "ury.ury.api.ury_availability": _stub_module(
                "ury.ury.api.ury_availability",
                apply_count_delta=MagicMock(),
            ),
            "ury.ury.api.ury_stock_policy": _stub_module(
                "ury.ury.api.ury_stock_policy",
                get_branch_stock_policy=MagicMock(return_value=Row(reservation_control_enabled=False)),
            ),
            "ury.ury.api.ury_order_reservation_service": _stub_module(
                "ury.ury.api.ury_order_reservation_service",
                _line_key=lambda row: row.get("reservation_line_key"),
                _line_ref=lambda row: row.get("reservation_line_key") or row.get("name"),
                reconcile_order_reservations=MagicMock(),
                release_order_reservations=MagicMock(),
                ensure_invoice_reservation_ref=MagicMock(),
                _warehouse_for_context=MagicMock(return_value=None),
                resolve_production_context=MagicMock(return_value=None),
            ),
            "ury.ury.api.ury_kot_generate": _stub_module(
                "ury.ury.api.ury_kot_generate",
                _line_keyed_items=lambda rows: {
                    (r.get("reservation_line_key") or r.get("item_code")): dict(r) for r in rows
                },
                _execute_kot=MagicMock(),
                process_items_for_cancel_kot=MagicMock(return_value=["CNCL-KOT-001"]),
                cancel_kot=MagicMock(return_value=["CNCL-KOT-002"]),
                kot_execute=MagicMock(),
            ),
            "ury.ury.doctype.ury_order.ury_order": _stub_module(
                "ury.ury.doctype.ury_order.ury_order",
                _kitchen_snapshot=lambda items: [
                    {
                        "item_code": r.get("item_code") or r.get("item"),
                        "item_name": r.get("item_name"),
                        "qty": float(r.get("qty") or 0),
                        "comments": r.get("comment") or "",
                        "reservation_line_key": r.get("reservation_line_key"),
                        "custom_parent_line_key": r.get("custom_parent_line_key"),
                        "custom_line_role": r.get("custom_line_role") or "Standard",
                        "custom_is_modifier": int(r.get("custom_is_modifier") or 0),
                        "custom_bundle_overrides_json": r.get("custom_bundle_overrides_json") or "",
                    }
                    for r in items
                    if int(r.get("ury_held") or 0) == 0
                ],
                _previous_line_snapshot=lambda items: [],
                release_merge_cluster_tables=MagicMock(),
            ),
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub_module(
                "ury.ury.doctype.staff_discount_policy.staff_discount_policy",
                get_applicable_policy=MagicMock(return_value=None),
            ),
            "ury.ury_pos.discount": _stub_module(
                "ury.ury_pos.discount",
                prorate_return_discount=MagicMock(),
                validate_pos_discount=MagicMock(),
            ),
        }

    def _add_modified_and_combo_check(self, name: str, *, docstatus: int = 1, ribeye_qty: float = 1.0, combo_qty: float = 1.0, cheese_rate: float = 1500.0, cheese_overridden: int = 1) -> Doc:
        combo_ov = self.h.make_combo_overrides_json()
        items = [
            Row(
                name=f"{name}-ROW-RIB",
                item_code="RIBEYE",
                item_name="Ribeye Steak",
                qty=ribeye_qty,
                rate=45000.0,
                price_list_rate=45000.0,
                base_price_list_rate=45000.0,
                amount=45000.0 * ribeye_qty,
                uom="Nos",
                warehouse="Kitchen Store - MH",
                income_account="Sales - MH",
                cost_center="Main - MH",
                ury_held=0,
                comment="Medium Rare",
                reservation_line_key=f"{name}-k-rib",
                custom_parent_line_key=None,
                custom_line_role="Standard",
                custom_is_modifier=0,
                custom_modifier_prefix="",
                custom_modifier_group=None,
                custom_modifier_option=None,
                custom_modifiers_json=json.dumps([
                    {"group": "Cooking Temperature", "option_code": "MED-RARE", "label": "Medium Rare", "hide_on_receipt": 1}
                ]),
                custom_bundle_overrides_json="",
                custom_inherit_parent_routing=1,
                custom_hide_unpriced_on_receipt=1,
            ),
            Row(
                name=f"{name}-ROW-CHZ",
                item_code="EXTRA-CHEESE",
                item_name="Aged Cheddar",
                qty=ribeye_qty,
                rate=cheese_rate,
                price_list_rate=cheese_rate,
                base_price_list_rate=cheese_rate,
                amount=cheese_rate * ribeye_qty,
                uom="Nos",
                warehouse="Kitchen Store - MH",
                income_account="Sales - MH",
                cost_center="Main - MH",
                ury_held=0,
                reservation_line_key=f"{name}-k-chz",
                custom_parent_line_key=f"{name}-k-rib",
                custom_line_role="Modifier",
                custom_is_modifier=1,
                custom_modifier_prefix="Extra",
                custom_modifier_group="Steak Extras",
                custom_modifier_option="EXTRA-CHEESE",
                custom_modifiers_json="",
                custom_bundle_overrides_json="",
                custom_inherit_parent_routing=1,
                custom_hide_unpriced_on_receipt=0,
                ignore_pricing_rule=1 if cheese_overridden else 0,
                custom_price_overridden=cheese_overridden,
                custom_original_price_list_rate=4000.0 if cheese_overridden else 0.0,
                custom_price_override_reason="PRICE-VIP: Loyalty adjustment" if cheese_overridden else None,
                custom_price_override_approver="manager@example.test" if cheese_overridden else None,
            ),
            Row(
                name=f"{name}-ROW-CMB",
                item_code="BURGER-COMBO",
                item_name="Burger Combo Meal",
                qty=combo_qty,
                rate=25000.0,
                price_list_rate=25000.0,
                base_price_list_rate=25000.0,
                amount=25000.0 * combo_qty,
                uom="Nos",
                warehouse="Kitchen Store - MH",
                income_account="Sales - MH",
                cost_center="Main - MH",
                ury_held=0,
                comment="Onion Rings, Fresh Mango Juice",
                reservation_line_key=f"{name}-k-cmb",
                custom_parent_line_key=None,
                custom_line_role="Combo Header",
                custom_is_modifier=0,
                custom_modifier_prefix="",
                custom_modifier_group=None,
                custom_modifier_option=None,
                custom_modifiers_json="",
                custom_bundle_overrides_json=combo_ov,
                custom_inherit_parent_routing=1,
                custom_hide_unpriced_on_receipt=0,
            ),
            Row(
                name=f"{name}-ROW-UF1",
                item_code="COMBO-UPGRADE-FEE",
                item_name="Onion Rings",
                qty=combo_qty,
                rate=3000.0,
                price_list_rate=3000.0,
                base_price_list_rate=3000.0,
                amount=3000.0 * combo_qty,
                uom="Nos",
                warehouse="Kitchen Store - MH",
                income_account="Sales - MH",
                cost_center="Main - MH",
                ury_held=0,
                reservation_line_key=f"{name}-k-uf1",
                custom_parent_line_key=f"{name}-k-cmb",
                custom_line_role="Combo Upcharge",
                custom_is_modifier=1,
                custom_modifier_prefix="",
                custom_modifier_group="Burger Side",
                custom_modifier_option="ONION-RINGS",
                custom_modifiers_json="",
                custom_bundle_overrides_json="",
                custom_inherit_parent_routing=1,
                custom_hide_unpriced_on_receipt=0,
            ),
            Row(
                name=f"{name}-ROW-UF2",
                item_code="COMBO-DRINK-FEE",
                item_name="Fresh Mango Juice",
                qty=combo_qty,
                rate=2000.0,
                price_list_rate=2000.0,
                base_price_list_rate=2000.0,
                amount=2000.0 * combo_qty,
                uom="Nos",
                warehouse="Kitchen Store - MH",
                income_account="Sales - MH",
                cost_center="Main - MH",
                ury_held=0,
                reservation_line_key=f"{name}-k-uf2",
                custom_parent_line_key=f"{name}-k-cmb",
                custom_line_role="Combo Upcharge",
                custom_is_modifier=1,
                custom_modifier_prefix="",
                custom_modifier_group="Burger Drink",
                custom_modifier_option="MANGO-JUICE",
                custom_modifiers_json="",
                custom_bundle_overrides_json="",
                custom_inherit_parent_routing=0,
                custom_hide_unpriced_on_receipt=0,
            ),
        ]
        gross = sum(r["qty"] * r["rate"] for r in items)
        inv = Doc(
            doctype="POS Invoice",
            name=name,
            company="Maximus Hospitality",
            currency="UGX",
            pos_profile="Main Till",
            branch="Main Branch",
            customer="Walk-in",
            customer_name="Walk-in",
            order_type="Dine In",
            selling_price_list="Standard Selling",
            posting_date="2026-11-25",
            posting_time="09:30:00",
            waiter="waiter1@example.test",
            cashier="cashier@example.test",
            owner="waiter1@example.test",
            docstatus=docstatus,
            status="Paid" if docstatus == 1 else "Draft",
            is_return=0,
            return_against=None,
            invoice_printed=1,
            restaurant_table="T1",
            total=gross,
            net_total=gross,
            discount_amount=0.0,
            total_taxes_and_charges=0.0,
            grand_total=gross,
            rounded_total=gross,
            paid_amount=gross if docstatus == 1 else 0.0,
            change_amount=0.0,
            outstanding_amount=0.0,
            items=items,
            packed_items=[],
            taxes=[],
            payments=[Row(mode_of_payment="Cash", amount=gross if docstatus == 1 else 0.0)],
            creation=datetime(2026, 11, 25, 9, 30, 0),
        )
        inv.add_comment = lambda ctype, text: self.h.comments.append({"invoice": inv.name, "type": ctype, "text": text})
        inv.save = MagicMock()
        inv.discard = MagicMock()
        inv.db_set = MagicMock()
        inv.calculate_taxes_and_totals = MagicMock()
        inv.set_missing_values = MagicMock()
        self.h.invoices[name] = inv
        return inv

    # ------------------------------------------------------------------
    # P1 #1: reopen_closed_check & adjust_closed_check
    # ------------------------------------------------------------------

    def test_p1_1_reopen_and_adjust_preserve_modifiers_combos_and_overrides_and_block_forged_saves(self):
        with patch.dict(sys.modules, self.base_modules):
            _load_fresh("ury.brand", "ury/brand.py")
            _load_fresh("ury.ury.api.item_price", "ury/ury/api/item_price.py")
            bundle = _load_fresh("ury.ury.api.ury_bundle_resolver", "ury/ury/api/ury_bundle_resolver.py")
            mods = _load_fresh("ury.ury.api.ury_modifiers", "ury/ury/api/ury_modifiers.py")
            pricing = _load_fresh("ury.ury_pos.pricing_actions", "ury/ury_pos/pricing_actions.py")
            vr = _load_fresh("ury.ury_pos.void_return_reopen", "ury/ury_pos/void_return_reopen.py")

            def _validate_hook(doc):
                if int(doc.get("is_return") or 0) == 1:
                    vr.validate_pos_return_doc(doc)
                pricing.validate_privileged_pricing_mutations(doc)
                mods.validate_pos_invoice_modifiers(doc)
                bundle.apply_bundle_overrides_to_packed_items(doc)

            def _before_submit_hook(doc):
                mods.validate_invoice_modifiers_before_submit(doc)
                if int(doc.get("is_return") or 0) == 1:
                    vr.validate_pos_return_doc(doc, on_submit_check=True)

            self.h.on_validate_invoice = _validate_hook
            self.h.on_before_submit_invoice = _before_submit_hook

            # 1) reopen_closed_check on modified + overridden + combo check
            orig_reopen = self._add_modified_and_combo_check("KB-REOPEN-1", docstatus=1, cheese_rate=1500.0, cheese_overridden=1)
            res_reopen = vr.reopen_closed_check(orig_reopen.name, reason="REOPEN-TENDER")

            reopened = self.h.invoices[res_reopen["reopened_invoice"]]
            rev_reopen = self.h.invoices[res_reopen["reversal_return"]]
            self.assertEqual(reopened.docstatus, 0)
            self.assertFalse(getattr(self.h.frappe.flags, "ury_reopen_adjust_copy", False))
            self.assertIsNone(getattr(reopened.flags, "ury_verified_privileged_child_rates", None))

            # Reopened draft preserves ALL #156 & #152 fields
            rib_row = next(r for r in reopened.items if r["item_code"] == "RIBEYE")
            chz_row = next(r for r in reopened.items if r["item_code"] == "EXTRA-CHEESE")
            cmb_row = next(r for r in reopened.items if r["item_code"] == "BURGER-COMBO")
            self.assertEqual(rib_row.get("reservation_line_key"), "KB-REOPEN-1-k-rib")
            self.assertIn("MED-RARE", rib_row.get("custom_modifiers_json") or "")
            self.assertEqual(chz_row.get("custom_is_modifier"), 1)
            self.assertEqual(chz_row.get("custom_parent_line_key"), "KB-REOPEN-1-k-rib")
            self.assertEqual(chz_row.get("custom_modifier_prefix"), "Extra")
            self.assertEqual(chz_row.get("custom_modifier_group"), "Steak Extras")
            self.assertEqual(chz_row.get("custom_modifier_option"), "EXTRA-CHEESE")
            self.assertEqual(chz_row.get("rate"), 1500.0)
            self.assertEqual(chz_row.get("custom_price_overridden"), 1)
            self.assertEqual(chz_row.get("custom_original_price_list_rate"), 4000.0)
            self.assertEqual(chz_row.get("custom_price_override_reason"), "PRICE-VIP: Loyalty adjustment")
            self.assertEqual(chz_row.get("custom_price_override_approver"), "manager@example.test")
            self.assertEqual(chz_row.get("ignore_pricing_rule"), 1)
            self.assertIn("ONION-RINGS", cmb_row.get("custom_bundle_overrides_json") or "")
            self.assertEqual(
                [(p["item_code"], p["qty"]) for p in reopened.packed_items],
                [("CLASSIC-BURGER", 1.0), ("ONION-RINGS", 1.0), ("MANGO-JUICE", 1.0)],
            )
            # Reversal return MUST ALSO preserve combo overrides so EOD credit-note SLE matches orig sale!
            rev_cmb = next(r for r in rev_reopen.items if r["item_code"] == "BURGER-COMBO")
            self.assertIn("ONION-RINGS", rev_cmb.get("custom_bundle_overrides_json") or "")
            self.assertEqual(
                [(p["item_code"], p["qty"]) for p in rev_reopen.packed_items],
                [("CLASSIC-BURGER", -1.0), ("ONION-RINGS", -1.0), ("MANGO-JUICE", -1.0)],
            )

            # Subsequent forged save on reopened check MUST be blocked with [POS-AUTH-REQUIRED]
            snapshot_before = Doc(
                **{
                    k: ([Row(r) for r in v] if k == "items" else v)
                    for k, v in reopened._d.items()
                }
            )
            forged_row = Row(
                name="FORGED-ROW-1",
                item_code="EXTRA-BACON",
                item_name="Smoked Bacon",
                qty=1.0,
                rate=100.0,
                price_list_rate=100.0,
                custom_price_overridden=1,
                reservation_line_key="forged-bcn",
                custom_parent_line_key="KB-REOPEN-1-k-rib",
                custom_is_modifier=1,
                custom_line_role="Modifier",
                custom_modifier_group="Steak Extras",
                custom_modifier_option="EXTRA-BACON",
            )
            reopened._before_save = snapshot_before
            reopened.items.append(forged_row)
            with self.assertRaises(self.h.frappe.PermissionError) as ctx_auth:
                reopened.save()
            self.assertIn("[POS-AUTH-REQUIRED]", str(ctx_auth.exception))
            reopened.items.pop()

            # 2) adjust_closed_check (Cash -> Card) on identical modified + overridden + combo check
            orig_adj = self._add_modified_and_combo_check("KB-ADJUST-1", docstatus=1, cheese_rate=1500.0, cheese_overridden=1)
            res_adj = vr.adjust_closed_check(
                orig_adj.name,
                payments=[{"mode_of_payment": "Card", "amount": orig_adj.grand_total}],
                reason="REOPEN-TENDER",
            )
            rep = self.h.invoices[res_adj["replacement_invoice"]]
            self.assertEqual(rep.docstatus, 1)
            self.assertFalse(getattr(self.h.frappe.flags, "ury_reopen_adjust_copy", False))
            self.assertIsNone(getattr(rep.flags, "ury_verified_privileged_child_rates", None))
            rep_chz = next(r for r in rep.items if r["item_code"] == "EXTRA-CHEESE")
            self.assertEqual(rep_chz.get("rate"), 1500.0)
            self.assertEqual(rep_chz.get("custom_price_overridden"), 1)
            self.assertEqual(rep_chz.get("custom_original_price_list_rate"), 4000.0)
            self.assertEqual(
                [(p["item_code"], p["qty"]) for p in rep.packed_items],
                [("CLASSIC-BURGER", 1.0), ("ONION-RINGS", 1.0), ("MANGO-JUICE", 1.0)],
            )

    # ------------------------------------------------------------------
    # P1 #2: Waste Stock Entry uses bundle overrides & linked stock modifiers
    # ------------------------------------------------------------------

    def test_p1_2_waste_stock_entry_resolves_bundle_overrides_and_linked_stock_modifiers(self):
        with patch.dict(sys.modules, self.base_modules):
            _load_fresh("ury.brand", "ury/brand.py")
            _load_fresh("ury.ury.api.ury_bundle_resolver", "ury/ury/api/ury_bundle_resolver.py")
            vr = _load_fresh("ury.ury_pos.void_return_reopen", "ury/ury_pos/void_return_reopen.py")
            qty_red = _load_fresh("ury.ury.api.ury_pos_invoice_qty_reduction", "ury/ury/api/ury_pos_invoice_qty_reduction.py")

            # 1) Direct _expand_stock_waste_items honors custom_bundle_overrides_json
            expanded = vr._expand_stock_waste_items(
                [
                    {
                        "item_code": "BURGER-COMBO",
                        "qty": 1.0,
                        "warehouse": "Kitchen Store - MH",
                        "uom": "Nos",
                        "custom_bundle_overrides_json": self.h.make_combo_overrides_json(),
                    }
                ],
                "Kitchen Store - MH",
            )
            self.assertEqual(
                [(r["item_code"], r["qty"]) for r in expanded],
                [("CLASSIC-BURGER", 1.0), ("ONION-RINGS", 1.0), ("MANGO-JUICE", 1.0)],
            )

            # 2) reduce_order_item_qty: voiding 1 of 2 BURGER-COMBO (swapped) wastes ONION-RINGS + MANGO-JUICE (NOT FRIES/COLA)
            inv = self._add_modified_and_combo_check(
                "KB-WASTE-1", docstatus=0, ribeye_qty=1.0, combo_qty=2.0, cheese_rate=4000.0, cheese_overridden=0
            )
            self.h.frappe.session.user = "manager@example.test"
            res_combo = qty_red.reduce_order_item_qty(
                inv.name,
                f"{inv.name}-ROW-CMB",
                1,
                reason="WASTE-SPOILED",
                is_waste=1,
            )
            self.assertTrue(res_combo["waste_stock_entry"])
            combo_se = next(se for se in self.h.stock_entries if se.name == res_combo["waste_stock_entry"])
            self.assertEqual(
                {(r["item_code"], r["qty"]) for r in combo_se.items},
                {("CLASSIC-BURGER", 1.0), ("ONION-RINGS", 1.0), ("MANGO-JUICE", 1.0)},
            )

            # 3) reduce_order_item_qty: voiding RIBEYE (1 -> 0) with linked stock EXTRA-CHEESE wastes BOTH!
            res_rib = qty_red.reduce_order_item_qty(
                inv.name,
                f"{inv.name}-ROW-RIB",
                0,
                reason="WASTE-SPOILED",
                is_waste=1,
            )
            self.assertTrue(res_rib["waste_stock_entry"])
            rib_se = next(se for se in self.h.stock_entries if se.name == res_rib["waste_stock_entry"])
            self.assertEqual(
                {(r["item_code"], r["qty"]) for r in rib_se.items},
                {("RIBEYE", 1.0), ("EXTRA-CHEESE", 1.0)},
            )

    def test_p1_2_cancel_order_and_sync_order_waste_forward_bundle_overrides_and_linked_modifiers(self):
        frappe_model = _stub_module("frappe.model")
        frappe_model.__path__ = []
        frappe_model_doc = _stub_module("frappe.model.document", Document=type("Document", (), {}))
        erpnext_pkg = _stub_module("erpnext")
        erpnext_pkg.__path__ = []
        erpnext_ctrl = _stub_module("erpnext.controllers")
        erpnext_ctrl.__path__ = []
        erpnext_queries = _stub_module("erpnext.controllers.queries", item_query=MagicMock())
        extra = {
            **self.base_modules,
            "frappe.model": frappe_model,
            "frappe.model.document": frappe_model_doc,
            "erpnext": erpnext_pkg,
            "erpnext.controllers": erpnext_ctrl,
            "erpnext.controllers.queries": erpnext_queries,
            "ury.ury.api.ury_stock_authority": _stub_module("ury.ury.api.ury_stock_authority"),
            "ury.ury.api.ury_kot_notification": _stub_module(
                "ury.ury.api.ury_kot_notification",
                create_system_notification=MagicMock(),
                get_users_with_role=MagicMock(return_value=[]),
            ),
            "ury.ury.doctype.alert_settings.alert_settings": _stub_module(
                "ury.ury.doctype.alert_settings.alert_settings",
                get_alert_rule=MagicMock(return_value=None),
            ),
            "ury.ury.hooks.ury_pos_tender": _stub_module(
                "ury.ury.hooks.ury_pos_tender",
                sync_charged_tip=MagicMock(),
            ),
            "ury.ury_pos.default_customer": _stub_module(
                "ury.ury_pos.default_customer",
                get_default_customer_config=MagicMock(return_value={}),
                resolve_default_customer=MagicMock(return_value="Walk-in"),
            ),
            "ury.ury_pos.fx": _stub_module(
                "ury.ury_pos.fx",
                build_offline_collision_plan=MagicMock(),
                ensure_local_change_payment_row=MagicMock(),
                prepare_settlement_payments=MagicMock(),
            ),
            "ury.ury_pos.discount": _stub_module(
                "ury.ury_pos.discount",
                prorate_return_discount=MagicMock(),
                validate_pos_discount=MagicMock(),
                apply_discount_to_invoice=MagicMock(),
                canonical_split_discount_claim=MagicMock(),
                currency_precision=lambda doc=None: 0,
                enforce_remove_discount_auth=MagicMock(),
                get_check_discounts=MagicMock(),
                list_discount_options=MagicMock(),
                parse_discount_request=MagicMock(),
                user_discount_limit=MagicMock(),
                with_currency_precision=MagicMock(),
                _above_limit=MagicMock(),
                _approver_discount_ceiling=MagicMock(),
                _compute_manual_discount_summary=MagicMock(return_value={}),
                _exceeds_approved_percent=MagicMock(),
                _get_override_module=MagicMock(),
                _is_auto_pricing_rule_row=MagicMock(),
                _read_meta=MagicMock(return_value={}),
                _resolve_reason=MagicMock(),
                _restore_doc_discount_state=MagicMock(),
                _set_row_field=MagicMock(),
                _snapshot_doc_discount_state=MagicMock(),
                _write_discount_audit_fields=MagicMock(),
            ),
            "ury.ury_pos.pricing_actions": _stub_module(
                "ury.ury_pos.pricing_actions",
                sync_service_charge=MagicMock(),
                validate_privileged_pricing_mutations=MagicMock(),
            ),
        }
        extra.pop("ury.ury.doctype.ury_order.ury_order", None)
        with patch.dict(sys.modules, extra):
            _load_fresh("ury.brand", "ury/brand.py")
            _load_fresh("ury.ury.api.item_price", "ury/ury/api/item_price.py")
            _load_fresh("ury.ury.api.ury_bundle_resolver", "ury/ury/api/ury_bundle_resolver.py")
            vr = _load_fresh("ury.ury_pos.void_return_reopen", "ury/ury_pos/void_return_reopen.py")
            ury_order = _load_fresh("ury.ury.doctype.ury_order.ury_order", "ury/ury/doctype/ury_order/ury_order.py")

            inv = self._add_modified_and_combo_check(
                "KB-CANCEL-WASTE", docstatus=0, ribeye_qty=1.0, combo_qty=1.0, cheese_rate=4000.0, cheese_overridden=0
            )
            self.h.kots.append(Doc(name="KOT-1", invoice=inv.name, type="New Order", docstatus=1))
            self.h.frappe.session.user = "manager@example.test"

            with (
                patch.object(ury_order, "_void_ops", vr, create=True),
                patch.object(ury_order, "cancel_kot", return_value=["CNCL-KOT-002"]),
            ):
                cancel_res = ury_order.cancel_order(inv.name, reason="WASTE-SPOILED", is_waste=1)
            self.assertTrue(self.h.stock_entries)
            se = self.h.stock_entries[-1]
            self.assertEqual(
                {(r["item_code"], r["qty"]) for r in se.items},
                {
                    ("RIBEYE", 1.0),
                    ("EXTRA-CHEESE", 1.0),
                    ("CLASSIC-BURGER", 1.0),
                    ("ONION-RINGS", 1.0),
                    ("MANGO-JUICE", 1.0),
                },
            )

    # ------------------------------------------------------------------
    # P1 #3: get_returnable_check, preview_pos_return, create_pos_return, validate_pos_return_doc
    # ------------------------------------------------------------------

    def test_p1_3_return_exposes_and_preserves_bundle_overrides_and_enforces_linked_modifiers(self):
        with patch.dict(sys.modules, self.base_modules):
            _load_fresh("ury.brand", "ury/brand.py")
            bundle = _load_fresh("ury.ury.api.ury_bundle_resolver", "ury/ury/api/ury_bundle_resolver.py")
            vr = _load_fresh("ury.ury_pos.void_return_reopen", "ury/ury_pos/void_return_reopen.py")

            def _validate_hook(doc):
                if int(doc.get("is_return") or 0) == 1:
                    vr.validate_pos_return_doc(doc)
                bundle.apply_bundle_overrides_to_packed_items(doc)

            self.h.on_validate_invoice = _validate_hook

            orig = self._add_modified_and_combo_check(
                "KB-RET-1", docstatus=1, ribeye_qty=2.0, combo_qty=1.0, cheese_rate=4000.0, cheese_overridden=0
            )
            self.h.frappe.session.user = "manager2@example.test"

            # 1) get_returnable_check exposes modifier linkage & bundle overrides
            info = vr.get_returnable_check(orig.name)
            by_code = {i["item_code"]: i for i in info["items"]}
            self.assertEqual(by_code["RIBEYE"]["reservation_line_key"], "KB-RET-1-k-rib")
            self.assertEqual(by_code["EXTRA-CHEESE"]["custom_is_modifier"], 1)
            self.assertEqual(by_code["EXTRA-CHEESE"]["custom_parent_line_key"], "KB-RET-1-k-rib")
            self.assertEqual(by_code["EXTRA-CHEESE"]["custom_modifier_prefix"], "Extra")
            self.assertIn("ONION-RINGS", by_code["BURGER-COMBO"]["custom_bundle_overrides_json"])

            # 2) Direct ORM/REST bypass returning parent RIBEYE (-1) WITHOUT linked EXTRA-CHEESE MUST be refused!
            forged_ret = self.h.frappe.new_doc("POS Invoice")
            forged_ret.is_return = 1
            forged_ret.return_against = orig.name
            forged_ret.pos_profile = "Main Till"
            forged_ret.company = "Maximus Hospitality"
            forged_ret.branch = "Main Branch"
            forged_ret.cancel_reason = "RET-QUALITY"
            forged_ret.append("items", {
                "item_code": "RIBEYE",
                "item_name": "Ribeye Steak",
                "qty": -1.0,
                "rate": 45000.0,
                "pos_invoice_item": f"{orig.name}-ROW-RIB",
            })
            forged_ret.append("payments", {"mode_of_payment": "Cash", "amount": -45000.0})
            with self.assertRaises(self.h.frappe.ValidationError) as ctx_orphan:
                vr.validate_pos_return_doc(forged_ret)
            self.assertIn("[POS-MODIFIER-ORPHAN]", str(ctx_orphan.exception))

            # 3) Partial parent return of RIBEYE (1 of 2) via preview_pos_return & create_pos_return
            #    proportionally includes EXTRA-CHEESE (1 of 2) so no child modifier is orphaned!
            prev = vr.preview_pos_return(orig.name, [{"item_row_name": f"{orig.name}-ROW-RIB", "qty": 1}])
            self.assertEqual(
                [(i["item_code"], i["qty"]) for i in prev["items"]],
                [("RIBEYE", 1.0), ("EXTRA-CHEESE", 1.0)],
            )
            self.assertEqual(prev["refund_total"], 49000.0)

            ret1_res = vr.create_pos_return(
                orig.name,
                [{"item_row_name": f"{orig.name}-ROW-RIB", "qty": 1}],
                reason="RET-QUALITY",
            )
            self.assertEqual(ret1_res["refund_total"], 49000.0)
            ret1_doc = self.h.invoices[ret1_res["return_invoice"]]
            self.assertEqual(
                [(r["item_code"], r["qty"]) for r in ret1_doc.items],
                [("RIBEYE", -1.0), ("EXTRA-CHEESE", -1.0)],
            )
            ret1_chz = ret1_doc.items[1]
            self.assertEqual(ret1_chz.get("custom_is_modifier"), 1)
            self.assertEqual(ret1_chz.get("custom_parent_line_key"), "KB-RET-1-k-rib")
            # Remaining check lines are balanced (1 RIBEYE + 1 EXTRA-CHEESE)
            after1 = {i["item_code"]: i["remaining_qty"] for i in vr.get_returnable_check(orig.name)["items"]}
            self.assertEqual(after1["RIBEYE"], 1.0)
            self.assertEqual(after1["EXTRA-CHEESE"], 1.0)

            # 4) Combo return preserves custom_bundle_overrides_json, includes linked upcharges,
            #    and reconciles packed_items to ONION-RINGS + MANGO-JUICE (NOT FRIES/COLA)!
            ret2_res = vr.create_pos_return(
                orig.name,
                [{"item_row_name": f"{orig.name}-ROW-CMB", "qty": 1}],
                reason="RET-QUALITY",
            )
            self.assertEqual(ret2_res["refund_total"], 30000.0)
            ret2_doc = self.h.invoices[ret2_res["return_invoice"]]
            self.assertEqual(
                {(r["item_code"], r["qty"]) for r in ret2_doc.items},
                {("BURGER-COMBO", -1.0), ("COMBO-UPGRADE-FEE", -1.0), ("COMBO-DRINK-FEE", -1.0)},
            )
            ret2_cmb = next(r for r in ret2_doc.items if r["item_code"] == "BURGER-COMBO")
            self.assertIn("ONION-RINGS", ret2_cmb.get("custom_bundle_overrides_json") or "")
            self.assertEqual(
                [(p["item_code"], p["qty"]) for p in ret2_doc.packed_items],
                [("CLASSIC-BURGER", -1.0), ("ONION-RINGS", -1.0), ("MANGO-JUICE", -1.0)],
            )

    def test_update_stock_preserved_for_bundle_packed_items_on_return_reopen_adjust(self):
        """Live MariaDB/ERPNext catch: ERPNext POSInvoice.update_packing_list() clears packed_items
        whenever update_stock != 1. create_pos_return, _build_full_reversal_return, reopen_closed_check,
        and adjust_closed_check MUST copy update_stock from orig so bundle packed_items stay reconciled.
        """
        with patch.dict(sys.modules, self.base_modules):
            _load_fresh("ury.brand", "ury/brand.py")
            _load_fresh("ury.ury.api.ury_bundle_resolver", "ury/ury/api/ury_bundle_resolver.py")
            vr = _load_fresh("ury.ury_pos.void_return_reopen", "ury/ury_pos/void_return_reopen.py")
            self.h.frappe.session.user = "manager2@example.test"

            # 1) Return check copies update_stock=1 from orig
            orig_ret = self._add_modified_and_combo_check(
                "KB-USTK-RET", docstatus=1, ribeye_qty=1.0, combo_qty=1.0, cheese_rate=4000.0, cheese_overridden=0
            )
            orig_ret.update_stock = 1
            ret_res = vr.create_pos_return(
                orig_ret.name,
                [{"item_row_name": f"{orig_ret.name}-ROW-CMB", "qty": 1}],
                reason="RET-QUALITY",
            )
            ret_doc = self.h.invoices[ret_res["return_invoice"]]
            self.assertEqual(
                int(getattr(ret_doc, "update_stock", 0) or 0),
                1,
                "create_pos_return must copy update_stock=1 from orig so ERPNext keeps return packed_items",
            )

            # 2) Reopen check copies update_stock=1 onto BOTH reversal_return AND reopened_invoice
            orig_reopen = self._add_modified_and_combo_check(
                "KB-USTK-REOPEN", docstatus=1, ribeye_qty=1.0, combo_qty=1.0, cheese_rate=4000.0, cheese_overridden=0
            )
            orig_reopen.update_stock = 1
            reopen_res = vr.reopen_closed_check(orig_reopen.name, reason="REOPEN-TENDER")
            rev_doc = self.h.invoices[reopen_res["reversal_return"]]
            reopened_doc = self.h.invoices[reopen_res["reopened_invoice"]]
            self.assertEqual(
                int(getattr(rev_doc, "update_stock", 0) or 0),
                1,
                "_build_full_reversal_return must copy update_stock=1 from orig",
            )
            self.assertEqual(
                int(getattr(reopened_doc, "update_stock", 0) or 0),
                1,
                "reopen_closed_check must copy update_stock=1 from orig",
            )

            # 3) Adjust closed check copies update_stock=1 onto replacement_invoice
            orig_adj = self._add_modified_and_combo_check(
                "KB-USTK-ADJ", docstatus=1, ribeye_qty=1.0, combo_qty=1.0, cheese_rate=4000.0, cheese_overridden=0
            )
            orig_adj.update_stock = 1
            adj_res = vr.adjust_closed_check(
                orig_adj.name,
                payments=[{"mode_of_payment": "Card", "amount": float(orig_adj.grand_total)}],
                reason="REOPEN-TENDER",
            )
            rep_doc = self.h.invoices[adj_res["replacement_invoice"]]
            self.assertEqual(
                int(getattr(rep_doc, "update_stock", 0) or 0),
                1,
                "adjust_closed_check must copy update_stock=1 from orig",
            )


if __name__ == "__main__":
    unittest.main()
