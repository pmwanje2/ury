"""Site-less RED/GREEN tests for B5a Slice 1: sent-item voids, qty reduction,
sync_order line reduction, whole-check cancel, cancel-KOT reason link, native
Material Issue waste Stock Entry, and mutation-level guards (matrix rows 5-8).
"""

from __future__ import annotations

import hashlib
import hmac
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
    """Minimal Frappe Document double supporting attribute & dict access without dict.items() shadowing."""

    def __init__(self, initial=None, **kwargs):
        object.__setattr__(self, "_d", {})
        if isinstance(initial, Doc):
            self._d.update(initial._d)
        elif isinstance(initial, dict):
            self._d.update(initial)
        self._d.update(kwargs)

    def __getattr__(self, name):
        d = object.__getattribute__(self, "_d")
        if name in d:
            return d[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name == "_d":
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

    def update(self, *args, **kwargs):
        self._d.update(*args, **kwargs)

    def append(self, field, row):
        self._d.setdefault(field, []).append(Doc(row) if isinstance(row, dict) else row)
        return self._d[field][-1]

    def remove(self, row):
        if row in self._d.get("items", []):
            self._d["items"].remove(row)

    def set(self, field, value):
        self._d[field] = value

    def as_dict(self):
        return dict(self._d)

    def is_new(self):
        return not bool(self._d.get("name")) or bool(self._d.get("_is_new"))


def _canonical_hash(claim):
    raw = json.dumps(claim or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ContractOverrideStub(ModuleType):
    """Contract-faithful in-memory stub of ury.ury_pos.override (B2a contract + Amendment 1)."""

    def __init__(self, harness):
        super().__init__("ury.ury_pos.override")
        self._h = harness
        self.PRIVILEGES = {
            "void_sent_item": ("Void Sent Item", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
            "reduce_sent_qty": ("Reduce Sent Item Quantity", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
            "cancel_check": ("Void / Cancel Check", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
            "return": ("Return / Refund Check", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
            "reopen_check": ("Reopen Closed Check", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
            "adjust_closed_check": ("Adjust Closed Check", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        }
        self._tokens = {}  # token -> metadata

    def is_policy_active(self, company):
        return bool(company and any(r.get("company") == company for r in self._h.privilege_rules))

    def requires_reason(self, pos_profile, privilege):
        profile = self._h.profiles.get(pos_profile)
        if not profile:
            return True
        rules = [
            r for r in self._h.privilege_rules
            if r.get("company") == profile.get("company") and r.get("privilege") == privilege
        ]
        if not rules:
            return True
        return any(int(r.get("requires_reason") or 0) == 1 for r in rules)

    def list_reasons(self, privilege=None):
        out = []
        for row in sorted(self._h.reasons, key=lambda r: (int(r.get("sort_order") or 0), r.get("code") or "")):
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
        if not profile or user in ("Administrator", "Guest", None, ""):
            return False
        roles = set(self._h.user_roles.get(user, []))
        if not self.is_policy_active(profile.get("company")):
            return bool(roles & {"URY Manager", "URY Admin", "Accounts Manager"})
        for rule in self._h.privilege_rules:
            if (
                rule.get("company") == profile.get("company")
                and rule.get("privilege") == privilege
                and rule.get("role") in roles
                and int(rule.get("may_perform") or 0) == 1
            ):
                return True
        return False

    def has_verified_approval(self, doc, privilege):
        proof = getattr(getattr(doc, "flags", None), f"pos_override_verified_{privilege}", None)
        return bool(proof and getattr(proof, "privilege", None) == privilege)

    def mint_token(self, requester, approver, pos_profile, privilege, reference_name, claim):
        token = f"tok-{len(self._tokens) + 1}"
        self._tokens[token] = {
            "requester": requester,
            "approver": approver,
            "pos_profile": pos_profile,
            "privilege": privilege,
            "reference_name": reference_name,
            "claim_hash": _canonical_hash(claim),
            "claim": dict(claim),
            "used": False,
        }
        return token

    def consume(self, token, privilege, pos_profile, reference_name, claim):
        entry = self._tokens.get(token)
        if not entry or entry["used"]:
            self._h.frappe.throw("This manager approval is missing or has expired. [POS-AUTH-EXPIRED]", self._h.frappe.ValidationError)
        if entry["requester"] != self._h.frappe.session.user:
            self._h.frappe.throw("This manager approval belongs to a different till session. [POS-AUTH-CHANGED]", self._h.frappe.ValidationError)
        if entry["approver"] == self._h.frappe.session.user:
            self._h.frappe.throw("You cannot approve your own request. [POS-AUTH-SELF]", self._h.frappe.PermissionError)
        if (
            entry["privilege"] != privilege
            or entry["pos_profile"] != pos_profile
            or entry["reference_name"] != reference_name
            or not hmac.compare_digest(entry["claim_hash"], _canonical_hash(claim))
        ):
            self._h.frappe.throw("The approved values changed after manager approval. [POS-AUTH-CHANGED]", self._h.frappe.ValidationError)
        entry["used"] = True
        self._h.auth_logs.append(Doc(
            doctype="POS Authorisation Log",
            requester=entry["requester"],
            approver=entry["approver"],
            privilege=privilege,
            pos_profile=pos_profile,
            reference_name=reference_name,
            reason=str(claim.get("reason") or ""),
            claim_values=json.dumps(claim, sort_keys=True, default=str),
            outcome="Consumed",
        ))
        return Doc(approver=entry["approver"], percent=claim.get("percent"), amount=claim.get("amount"), claim=dict(claim))

    def require(self, privilege, pos_profile, reference_name, claim, token=None, percent=None, amount=None, privilege_group=0):
        if token:
            return self.consume(token, privilege, pos_profile, reference_name, claim)
        if self.may_perform(self._h.frappe.session.user, pos_profile, privilege, percent=percent, amount=amount):
            return None
        self._h.frappe.throw("A manager must approve this action. [POS-AUTH-REQUIRED]", self._h.frappe.PermissionError)


def _load_fresh(module_name: str, rel_path: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


class VoidCancelHarness:
    """In-memory Frappe environment for site-less testing of Slice 1."""

    def __init__(self):
        self.user_roles = {
            "cashier@example.test": ["URY Cashier"],
            "waiter1@example.test": ["URY Captain"],
            "waiter2@example.test": ["URY Captain"],
            "manager@example.test": ["URY Manager", "URY Cashier"],
            "manager2@example.test": ["URY Manager", "URY Cashier"],
            "other_outlet_mgr@example.test": ["URY Manager"],
        }
        self.user_branches = {
            "cashier@example.test": "Branch A",
            "waiter1@example.test": "Branch A",
            "waiter2@example.test": "Branch A",
            "manager@example.test": "Branch A",
            "manager2@example.test": "Branch A",
            "other_outlet_mgr@example.test": "Branch B",
        }
        self.user_perms = {
            # (user, doctype, ptype) -> bool; default True except Cashier/Captain lack POS Invoice cancel
            ("cashier@example.test", "POS Invoice", "cancel"): False,
            ("waiter1@example.test", "POS Invoice", "cancel"): False,
            ("waiter2@example.test", "POS Invoice", "cancel"): False,
        }
        self.profiles = {
            "Till A": Doc(
                name="Till A",
                company="Hotel Co",
                currency="UGX",
                branch="Branch A",
                warehouse="Kitchen - HC",
                cost_center="Restaurant - HC",
                custom_kot_naming_series="KOT-.#####",
                custom_qty_reduction_allowed_order_types="Dine In,Take Away",
                role_allowed_for_billing=[Doc(role="URY Cashier"), Doc(role="URY Manager")],
                transfer_role_permissions=[Doc(role="URY Manager")],
                role_restricted_for_table_order=[],
                applicable_for_users=[
                    Doc(user="cashier@example.test"),
                    Doc(user="manager@example.test"),
                    Doc(user="manager2@example.test"),
                ],
                custom_enable_multiple_cashier=0,
                ury_enable_hold_fire=1,
                remove_items=1,  # Deliberately 1 to prove remove_items no longer bypasses reason/PIN!
                custom_void_reasons="",
            )
        }
        self.items_master = {
            "STEAK": Doc(item_code="STEAK", item_name="Steak", is_stock_item=1, stock_uom="Nos"),
            "SOUP": Doc(item_code="SOUP", item_name="Soup", is_stock_item=1, stock_uom="Nos"),
            "CORKAGE": Doc(item_code="CORKAGE", item_name="Corkage Fee", is_stock_item=0, stock_uom="Nos"),
        }
        self.stock_entry_types = {"Wastage": "Material Issue", "Material Issue": "Material Issue"}
        self.wastage_accounts = ["Wastage and Spoilage - HC"]
        self.company_defaults = {
            "Hotel Co": Doc(
                name="Hotel Co",
                stock_adjustment_account="Stock Adjustment - HC",
                cost_center="Main - HC",
            )
        }
        self.privilege_rules = [
            Doc(company="Hotel Co", pos_profile="", privilege=p, role="URY Manager", may_perform=1, may_authorise=1, requires_reason=1)
            for p in ("void_sent_item", "reduce_sent_qty", "cancel_check", "return", "reopen_check", "adjust_closed_check")
        ]
        self.reasons = [
            Doc(code="VOID-GUEST", description="Guest changed mind", applies_to="void_sent_item\nreduce_sent_qty\ncancel_check", requires_note=0, sort_order=10, disabled=0),
            Doc(code="VOID-KITCHEN", description="Kitchen error", applies_to="void_sent_item\nreduce_sent_qty\ncancel_check", requires_note=0, sort_order=20, disabled=0),
            Doc(code="WASTE-SPOILED", description="Spoiled or dropped item (waste)", applies_to="void_sent_item\nreduce_sent_qty\ncancel_check", requires_note=0, sort_order=30, disabled=0),
            Doc(code="OTHER", description="Other", applies_to="", requires_note=1, sort_order=999, disabled=0),
        ]
        self.invoices = {}
        self.kots = []
        self.stock_entries = []
        self.auth_logs = []
        self.comments = []
        self.custom_fields_present = {
            ("Stock Entry", "custom_wastage_reason"),
            ("URY KOT", "custom_waste_stock_entry"),
            ("URY KOT", "custom_void_reason_code"),
            ("POS Invoice", "custom_waste_stock_entry"),
            ("POS Invoice", "custom_return_approver"),
            ("POS Invoice", "cancel_reason"),
            ("POS Profile", "custom_void_reasons"),
        }

        self.frappe = self._build_frappe()
        self.override = ContractOverrideStub(self)

    def add_invoice(self, name="POS-INV-1", *, docstatus=0, invoice_printed=1, waiter="waiter1@example.test",
                    owner="waiter1@example.test", branch="Branch A", pos_profile="Till A", order_type="Dine In",
                    table="T1", items=None, payments=None, has_sent_kot=True, efris_irn=None):
        inv = Doc(
            doctype="POS Invoice",
            name=name,
            company="Hotel Co",
            currency="UGX",
            branch=branch,
            pos_profile=pos_profile,
            customer="Walk-in",
            customer_name="Walk-in",
            mobile_number="",
            order_type=order_type,
            restaurant_table=table,
            custom_merged_tables="",
            custom_merged_pos_invoice=None,
            waiter=waiter,
            owner=owner,
            cashier="cashier@example.test",
            docstatus=docstatus,
            status="Paid" if docstatus == 1 else "Draft",
            invoice_printed=invoice_printed,
            invoice_created=1,
            selling_price_list="Standard Selling",
            custom_ury_order_number="ORD-1",
            custom_comments="",
            modified="2026-11-24 10:00:00",
            creation=datetime(2026, 11, 24, 9, 50, 0),
            items=[],
            payments=[],
            grand_total=0,
            rounded_total=0,
            efris_irn=efris_irn,
            efris_status="Submitted" if efris_irn else "",
            flags=SimpleNamespace(),
        )
        for idx, row in enumerate(items or [
            {"name": "ROW-1", "item_code": "STEAK", "item_name": "Steak", "qty": 2, "rate": 35000, "amount": 70000, "ury_held": 0, "reservation_line_key": "line-1", "comment": ""},
            {"name": "ROW-2", "item_code": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "amount": 15000, "ury_held": 0, "reservation_line_key": "line-2", "comment": ""},
        ], start=1):
            r = Doc(row)
            r.setdefault("name", f"ROW-{idx}")
            r.setdefault("amount", float(r["qty"]) * float(r.get("rate", 10000)))
            r.setdefault("rate", 10000)
            r.setdefault("ury_held", 0)
            r.setdefault("comment", "")
            r.setdefault("warehouse", "Kitchen - HC")
            inv["items"].append(r)
        inv["grand_total"] = sum(float(r["qty"]) * float(r["rate"]) for r in inv["items"])
        inv["rounded_total"] = inv["grand_total"]
        for p in payments or []:
            inv["payments"].append(Doc(p))

        def _save(ignore_permissions=False):
            inv["grand_total"] = sum(float(r["qty"]) * float(r["rate"]) for r in inv["items"])
            inv["rounded_total"] = inv["grand_total"]
            inv["_saved"] = inv.get("_saved", 0) + 1

        def _discard():
            inv["docstatus"] = 2
            inv["status"] = "Cancelled"
            inv["_discarded"] = True

        def _cancel():
            inv["docstatus"] = 2
            inv["status"] = "Cancelled"
            inv["_cancelled"] = True

        def _db_set(field, value=None, **kwargs):
            if isinstance(field, dict):
                inv.update(field)
            else:
                inv[field] = value

        inv.save = MagicMock(side_effect=_save)
        inv.discard = MagicMock(side_effect=_discard)
        inv.cancel = MagicMock(side_effect=_cancel)
        inv.db_set = MagicMock(side_effect=_db_set)
        inv.reload = MagicMock(return_value=inv)
        inv.precision = MagicMock(return_value=0)
        inv.calculate_taxes_and_totals = MagicMock(side_effect=lambda: inv.update(
            grand_total=sum(float(r["qty"]) * float(r["rate"]) for r in inv["items"]),
            rounded_total=sum(float(r["qty"]) * float(r["rate"]) for r in inv["items"]),
        ))
        inv.set_missing_values = MagicMock()
        inv.add_comment = MagicMock(side_effect=lambda kind, text: self.comments.append((name, kind, text)))
        self.invoices[name] = inv
        if has_sent_kot:
            orig_kot = Doc(
                doctype="URY KOT",
                name=f"KOT-ORIG-{len(self.kots) + 1}",
                invoice=name,
                type="New Order",
                docstatus=1,
                production="Kitchen",
                kot_items=[
                    Doc(item=r["item_code"], item_name=r["item_name"], quantity=r["qty"], comments=r.get("comment", ""), reservation_line_key=r.get("reservation_line_key"))
                    for r in inv["items"] if not int(r.get("ury_held") or 0)
                ],
            )
            orig_kot.save = MagicMock()
            self.kots.append(orig_kot)
        return inv

    def _build_frappe(self):
        h = self
        fw = MagicMock()
        fw.PermissionError = type("PermissionError", (Exception,), {})
        fw.ValidationError = type("ValidationError", (Exception,), {})
        fw.UniqueValidationError = type("UniqueValidationError", (Exception,), {})
        fw._ = lambda msg, *a: msg
        fw.whitelist = lambda *a, **kw: (lambda fn: fn)
        fw.session = SimpleNamespace(user="cashier@example.test")
        fw.flags = SimpleNamespace()
        fw.form_dict = {}

        def _throw(msg, exc=None, **kw):
            raise (exc or fw.ValidationError)(msg)

        fw.throw = _throw
        fw.get_roles = lambda user=None: list(h.user_roles.get(user or fw.session.user, []))
        fw.has_permission = lambda dt, ptype="read", doc=None, user=None: h.user_perms.get((user or fw.session.user, dt, ptype), True)
        fw.get_precision = lambda *a, **kw: 3

        def _get_meta(dt):
            m = MagicMock()
            m.has_field = lambda fn: (dt, fn) in h.custom_fields_present
            m.get_field = lambda fn: SimpleNamespace(length=140) if fn == "cancel_reason" else None
            return m

        fw.get_meta = _get_meta

        def _exists(dt, filt=None):
            if dt == "POS Invoice":
                if isinstance(filt, str):
                    return filt if filt in h.invoices else None
                if isinstance(filt, dict):
                    for inv_name, inv_doc in h.invoices.items():
                        ok = True
                        for k, v in filt.items():
                            if inv_doc.get(k) != v:
                                ok = False
                                break
                        if ok:
                            return inv_name
                return None
            if dt == "POS Profile":
                return filt if isinstance(filt, str) and filt in h.profiles else None
            if dt == "POS Opening Entry":
                return "POS-OPEN-1"
            if dt == "Stock Entry Type":
                return filt if isinstance(filt, str) and filt in h.stock_entry_types else None
            if dt == "Product Bundle":
                return None
            if dt == "URY KOT":
                if isinstance(filt, dict):
                    for k in h.kots:
                        if filt.get("invoice") and k.get("invoice") != filt["invoice"]:
                            continue
                        types = filt.get("type")
                        if isinstance(types, (list, tuple)) and types[0] == "in":
                            if k.get("type") not in types[1]:
                                continue
                        ds = filt.get("docstatus")
                        if isinstance(ds, (list, tuple)) and ds[0] == "in":
                            if k.get("docstatus") not in ds[1]:
                                continue
                        elif ds is not None and k.get("docstatus") != ds:
                            continue
                        return k["name"]
                return None
            if dt == "POS Reason":
                if isinstance(filt, str):
                    return filt if any(r["code"] == filt for r in h.reasons) else None
                return bool(h.reasons)
            if dt == "DocType":
                return filt in ("POS Reason", "POS Privilege Rule", "POS Authorisation Log", "Stock Entry")
            return None

        def _table_exists(dt):
            return dt in ("POS Reason", "POS Privilege Rule", "POS Authorisation Log", "Stock Entry")

        def _get_value(dt, filters=None, fieldname=None, as_dict=False, **kw):
            if dt == "POS Profile" and isinstance(filters, str):
                prof = h.profiles.get(filters)
                if not prof:
                    return None
                if isinstance(fieldname, str):
                    return prof.get(fieldname)
                return prof
            if dt == "POS Invoice":
                inv = None
                if isinstance(filters, str):
                    inv = h.invoices.get(filters)
                elif isinstance(filters, dict):
                    for candidate in h.invoices.values():
                        if all(candidate.get(k) == v for k, v in filters.items()):
                            inv = candidate
                            break
                if not inv:
                    return None
                if isinstance(fieldname, str):
                    return inv.get(fieldname)
                return Doc({f: inv.get(f) for f in fieldname}) if as_dict else tuple(inv.get(f) for f in fieldname)
            if dt == "Item" and isinstance(filters, str):
                it = h.items_master.get(filters)
                if not it:
                    return None
                if isinstance(fieldname, str):
                    return it.get(fieldname)
                if isinstance(fieldname, (list, tuple)):
                    return Doc({f: it.get(f) for f in fieldname}) if as_dict else tuple(it.get(f) for f in fieldname)
                return it
            if dt == "Stock Entry Type" and isinstance(filters, str):
                return h.stock_entry_types.get(filters) if fieldname == "purpose" else None
            if dt == "Company" and isinstance(filters, str):
                comp = h.company_defaults.get(filters)
                return comp.get(fieldname) if comp and isinstance(fieldname, str) else None
            if dt == "URY Table":
                if fieldname == ["restaurant", "branch", "restaurant_room"]:
                    return ("Rest A", "Branch A", "Main Hall")
                if fieldname == ["branch", "restaurant_room"]:
                    return ("Branch A", "Main Hall")
                return "Branch A" if fieldname == "branch" else ("Rest A" if fieldname == "restaurant" else "Main Hall")
            if dt == "URY Restaurant":
                if fieldname == "room_wise_menu":
                    return 0
                if fieldname == "order_type_wise_menu":
                    return 0
                if fieldname == "default_tax_template":
                    return None
                return "Menu A"
            if dt == "Price List":
                return "Standard Selling"
            if dt == "URY Menu":
                return "Menu A"
            if dt == "URY Menu Item":
                return "Main"
            if dt == "E Invoice":
                return None
            return None

        def _get_all(dt, filters=None, fields=None, pluck=None, **kw):
            if dt == "POS Invoice":
                out = []
                for inv in h.invoices.values():
                    if isinstance(filters, dict):
                        ok = True
                        for k, v in filters.items():
                            if inv.get(k) != v:
                                ok = False
                                break
                        if not ok:
                            continue
                    or_f = kw.get("or_filters")
                    if isinstance(or_f, dict):
                        matched_or = False
                        for k, v in or_f.items():
                            if isinstance(v, list) and v[0] == "like":
                                needle = v[1].strip("%")
                                if needle and needle in str(inv.get(k) or ""):
                                    matched_or = True
                            elif inv.get(k) == v:
                                matched_or = True
                        if not matched_or:
                            continue
                    out.append(inv)
                return out
            if dt == "Account" and filters and filters.get("account_name") == "Wastage and Spoilage":
                return list(h.wastage_accounts)
            if dt == "URY Production Unit":
                return [Doc(name="Kitchen")]
            if dt == "URY Menu Item":
                return [Doc(item=k) for k in h.items_master]
            if dt == "URY KOT":
                out = []
                for k in h.kots:
                    if filters and filters.get("invoice") and k.get("invoice") != filters["invoice"]:
                        continue
                    out.append(k)
                return out
            if dt == "POS Reason":
                return [r for r in h.reasons if not int(r.get("disabled") or 0)]
            if dt == "Cost Center":
                return [Doc(name="Restaurant - HC")] if not pluck else ["Restaurant - HC"]
            return []

        def _new_doc_obj(data):
            doc = Doc(data)
            doc.setdefault("name", None)
            doc.flags = SimpleNamespace()
            doc.setdefault("items", [])
            doc.setdefault("kot_items", [])

            def _insert(ignore_permissions=False):
                doc.flags.ignore_permissions = ignore_permissions
                dt = doc.get("doctype")
                if dt == "Stock Entry":
                    doc["name"] = f"MAT-STE-{len(h.stock_entries) + 1:04d}"
                    doc["docstatus"] = 0
                    h.stock_entries.append(doc)
                elif dt == "URY KOT":
                    doc["name"] = f"CNCL-KOT-{len(h.kots) + 1:04d}"
                    doc["docstatus"] = 0
                    h.kots.append(doc)
                elif dt == "POS Authorisation Log":
                    doc["name"] = f"POS-AUTH-LOG-{len(h.auth_logs) + 1:04d}"
                    h.auth_logs.append(doc)
                return doc

            def _submit():
                doc["docstatus"] = 1
                return doc

            def _save(ignore_permissions=False):
                return doc

            doc.insert = MagicMock(side_effect=_insert)
            doc.submit = MagicMock(side_effect=_submit)
            doc.save = MagicMock(side_effect=_save)
            return doc

        def _get_doc(*args, **kwargs):
            if len(args) == 1 and isinstance(args[0], dict):
                return _new_doc_obj(args[0])
            dt, name = args[0], args[1]
            if dt == "POS Invoice":
                if name not in h.invoices:
                    raise KeyError(name)
                return h.invoices[name]
            if dt == "POS Profile":
                return h.profiles[name]
            if dt == "Customer":
                return Doc(name=name, customer_name=name, mobile_number="")
            if dt == "URY KOT":
                for k in h.kots:
                    if k["name"] == name:
                        return k
            return Doc(doctype=dt, name=name)

        fw.new_doc = lambda dt: _new_doc_obj({"doctype": dt})
        fw.get_doc = _get_doc
        fw.get_cached_doc = _get_doc
        fw.get_all = _get_all
        fw.get_value = _get_value
        fw.db = MagicMock()
        fw.db.exists.side_effect = _exists
        fw.db.table_exists.side_effect = _table_exists
        fw.db.get_value.side_effect = _get_value
        fw.db.get_all.side_effect = _get_all
        fw.db.get_list.side_effect = lambda dt, **kw: (
            [Doc(price_list_rate=35000)] if dt == "Item Price"
            else [k for k in h.kots if not kw.get("filters") or k.get("invoice") == kw["filters"].get("invoice")]
        )
        fw.db.set_value = MagicMock()
        fw.db.sql.return_value = []
        return fw

    def load_modules(self):
        fw = self.frappe
        utils = ModuleType("frappe.utils")
        utils.cint = lambda v: int(v or 0)
        utils.flt = lambda v, precision=None: round(float(v or 0), precision) if precision is not None else float(v or 0)
        utils.get_datetime = lambda v=None: datetime.fromisoformat(str(v)) if isinstance(v, str) else (v or datetime(2026, 11, 24, 10, 0, 0))
        utils.now_datetime = lambda: datetime(2026, 11, 24, 10, 5, 0)
        utils.nowdate = lambda: "2026-11-24"
        utils.nowtime = lambda: "10:05:00"
        utils.now = lambda: "2026-11-24 10:05:00"
        utils.getdate = lambda v=None: str(v or "2026-11-24")[:10]
        utils.get_time = lambda v=None: v
        utils.get_first_day = lambda v=None: "2026-11-01"
        utils.get_last_day = lambda v=None: "2026-11-30"
        utils.add_days = lambda d, n=0: d
        utils.add_to_date = lambda d, **kw: d
        utils.get_system_timezone = lambda: "Africa/Kampala"

        exc_mod = ModuleType("frappe.exceptions")
        exc_mod.ValidationError = fw.ValidationError
        fw.exceptions = exc_mod

        doc_mod = ModuleType("frappe.model.document")
        doc_mod.Document = object
        model_mod = ModuleType("frappe.model")
        model_mod.document = doc_mod

        queries = ModuleType("erpnext.controllers.queries")
        queries.item_query = MagicMock()
        controllers = ModuleType("erpnext.controllers")
        controllers.queries = queries
        erpnext = ModuleType("erpnext")
        erpnext.controllers = controllers

        pos_api = ModuleType("ury.ury_pos.api")
        pos_api.getBranch = MagicMock(side_effect=lambda: self.user_branches.get(fw.session.user, "Branch A"))
        pos_api.getBranchRoom = MagicMock(return_value=[{"name": "Main Hall", "branch": "Branch A"}])
        pos_api.getRoom = MagicMock(return_value=[{"name": "Main Hall"}])
        pos_api.posOpening = MagicMock(return_value=1)

        stock_policy = ModuleType("ury.ury.api.ury_stock_policy")
        stock_policy.get_branch_stock_policy = MagicMock(return_value=Doc(reservation_control_enabled=0))

        avail = ModuleType("ury.ury.api.ury_availability")
        avail.apply_count_delta = MagicMock()
        avail._resolve_production_config = MagicMock(return_value=None)
        avail.get_reservation_availability = MagicMock(return_value=None)

        printer = ModuleType("ury.ury.api.ury_waiter_print")
        printer.print_combined_waiter_order_slip = MagicMock()

        mto = ModuleType("ury.ury.api.ury_mto_work_order_service")
        mto.create_work_orders_for_kot = MagicMock()

        routing = ModuleType("ury.ury.api.ury_kot_routing")
        routing.resolve_production_units = MagicMock(return_value=["Kitchen"])

        prod_ctx = ModuleType("ury.ury.api.ury_production_context")
        prod_ctx.resolve_production_context = MagicMock(return_value=None)

        alerts = ModuleType("ury.ury.doctype.alert_settings.alert_settings")
        alerts.get_alert_rule = MagicMock(return_value=None)

        notif = ModuleType("ury.ury.api.ury_kot_notification")
        notif.create_system_notification = MagicMock()
        notif.get_users_with_role = MagicMock(return_value=[])

        def_cust = ModuleType("ury.ury_pos.default_customer")
        def_cust.get_default_customer_config = MagicMock(return_value={})
        def_cust.resolve_default_customer = MagicMock(return_value="Walk-in")

        staff_disc = ModuleType("ury.ury.doctype.staff_discount_policy.staff_discount_policy")
        staff_disc.get_applicable_policy = MagicMock(return_value=None)

        tbl_res = MagicMock()
        tbl_res.check_table_reservation = MagicMock(return_value=None)
        tbl_res.get_completed_reservation_for_table = MagicMock(return_value=None)
        tbl_res.get_seated_reservation_for_table = MagicMock(return_value=None)
        tbl_res.parse_to_datetime = MagicMock()
        tbl_res.reconcile_check_reservation_binding = MagicMock()
        tbl_res.validate_table_reservation_for_order = MagicMock()

        mods = {
            "frappe": fw,
            "frappe.utils": utils,
            "frappe.exceptions": exc_mod,
            "frappe.model": model_mod,
            "frappe.model.document": doc_mod,
            "erpnext": erpnext,
            "erpnext.controllers": controllers,
            "erpnext.controllers.queries": queries,
            "ury.ury_pos.api": pos_api,
            "ury.ury_pos.override": self.override,
            "ury.ury.api.ury_stock_policy": stock_policy,
            "ury.ury.api.ury_availability": avail,
            "ury.ury.api.ury_waiter_print": printer,
            "ury.ury.api.ury_mto_work_order_service": mto,
            "ury.ury.api.ury_kot_routing": routing,
            "ury.ury.api.ury_production_context": prod_ctx,
            "ury.ury.doctype.alert_settings.alert_settings": alerts,
            "ury.ury.api.ury_kot_notification": notif,
            "ury.ury_pos.default_customer": def_cust,
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy": staff_disc,
            "ury.ury.api.table_reservation": tbl_res,
        }
        return mods


class TestSentItemVoidAndCancelOrder(unittest.TestCase):
    def setUp(self):
        self.h = VoidCancelHarness()
        mods = self.h.load_modules()
        self.enterContext(patch.dict(sys.modules, mods))
        self.res_mod = _load_fresh("ury.ury.api.ury_order_reservation_service", "ury/ury/api/ury_order_reservation_service.py")
        self.kot_mod = _load_fresh("ury.ury.api.ury_kot_generate", "ury/ury/api/ury_kot_generate.py")
        self.order_mod = _load_fresh("ury.ury.doctype.ury_order.ury_order", "ury/ury/doctype/ury_order/ury_order.py")
        self.qty_mod = _load_fresh("ury.ury.api.ury_pos_invoice_qty_reduction", "ury/ury/api/ury_pos_invoice_qty_reduction.py")
        self.inv_hook = _load_fresh("ury.ury.hooks.ury_pos_invoice", "ury/ury/hooks/ury_pos_invoice.py")
        # Clear any previously loaded void_return_reopen so each test gets fresh module bindings.
        sys.modules.pop("ury.ury_pos.void_return_reopen", None)

    # --- 1. Unsent (held) items stay free (Simphony Error Correct, row 5) ---

    def test_unsent_held_item_reduction_in_reduce_order_item_qty_stays_free(self):
        self.h.frappe.session.user = "cashier@example.test"
        inv = self.h.add_invoice(
            items=[
                {"name": "ROW-HELD", "item_code": "STEAK", "item_name": "Steak", "qty": 2, "rate": 35000, "ury_held": 1, "reservation_line_key": "held-1"},
                {"name": "ROW-SENT", "item_code": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "sent-1"},
            ]
        )
        result = self.qty_mod.reduce_order_item_qty(inv.name, "ROW-HELD", 1)
        self.assertEqual(result["new_qty"], 1)
        self.assertEqual(result["cancel_kot_names"], [])
        self.assertEqual(self.h.stock_entries, [])
        self.assertEqual(self.h.auth_logs, [])

    def test_unsent_held_item_removal_in_sync_order_stays_free_for_waiter(self):
        self.h.frappe.session.user = "waiter1@example.test"
        inv = self.h.add_invoice(
            invoice_printed=0,
            items=[
                {"name": "ROW-HELD", "item_code": "STEAK", "item_name": "Steak", "qty": 2, "rate": 35000, "ury_held": 1, "reservation_line_key": "held-1"},
                {"name": "ROW-SENT", "item_code": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "sent-1"},
            ],
        )
        # Drop held item without reason or manager PIN; keep sent item untouched.
        self.order_mod.sync_order(
            items=[{"name": "ROW-SENT", "item": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "sent-1"}],
            cashier="cashier@example.test", owner="waiter1@example.test", mode_of_payment="Cash",
            customer="Walk-in", no_of_pax=2, last_invoice=inv.name, waiter="waiter1@example.test",
            pos_profile="Till A", last_modified_time=inv.modified, table="T1", invoice=inv.name,
            order_type="Dine In", room="Main Hall",
        )
        self.assertEqual(len(inv.items), 1)
        self.assertEqual(inv.items[0].item_code, "SOUP")
        self.assertEqual(self.h.stock_entries, [])

    # --- 2. Sent item reduction via reduce_order_item_qty (row 6) ---

    def test_reduce_sent_item_requires_reason_before_mutation(self):
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_invoice()
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-AUTH-REASON"):
            self.qty_mod.reduce_order_item_qty(inv.name, "ROW-1", 1, reason=None)
        self.assertEqual(inv.items[0].qty, 2)
        self.assertEqual(len(self.h.kots), 1)  # only original KOT

    def test_reduce_sent_item_requires_note_when_pos_reason_requires_note(self):
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_invoice()
        with self.assertRaisesRegex(self.h.frappe.ValidationError, r"note.*POS-AUTH-REASON"):
            self.qty_mod.reduce_order_item_qty(inv.name, "ROW-1", 1, reason="OTHER", note="ab")
        self.assertEqual(inv.items[0].qty, 2)

    def test_reduce_sent_item_cashier_needs_manager_pin_and_session_never_changes(self):
        self.h.frappe.session.user = "cashier@example.test"
        inv = self.h.add_invoice()
        # Without approval_token: refused with POS-AUTH-REQUIRED.
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.qty_mod.reduce_order_item_qty(inv.name, "ROW-1", 1, reason="VOID-GUEST")
        self.assertEqual(inv.items[0].qty, 2)

        # With manager PIN token bound to exact reduce_sent_qty claim: succeeds!
        claim = {
            "invoice": inv.name,
            "item_row_name": "ROW-1",
            "item_code": "STEAK",
            "previous_qty": 2.0,
            "new_qty": 1.0,
            "delta": -1.0,
            "reason": "VOID-GUEST",
            "note": "",
            "is_waste": 0,
        }
        token = self.h.override.mint_token("cashier@example.test", "manager@example.test", "Till A", "reduce_sent_qty", inv.name, claim)
        res = self.qty_mod.reduce_order_item_qty(
            inv.name, "ROW-1", 1, reason="VOID-GUEST", approval_token=token
        )
        self.assertEqual(res["new_qty"], 1)
        self.assertEqual(res["approver"], "manager@example.test")
        self.assertEqual(res["reason"], "VOID-GUEST")
        self.assertEqual(self.h.frappe.session.user, "cashier@example.test")
        cancel_kot = self.h.kots[-1]
        self.assertEqual(cancel_kot.type, "Partially cancelled")
        self.assertIn("Guest changed mind", cancel_kot.comments)
        self.assertEqual(cancel_kot.get("custom_void_reason_code"), "VOID-GUEST")

    def test_reduce_sent_item_to_zero_uses_void_sent_item_privilege(self):
        self.h.frappe.session.user = "cashier@example.test"
        inv = self.h.add_invoice()
        claim = {
            "invoice": inv.name,
            "item_row_name": "ROW-1",
            "item_code": "STEAK",
            "previous_qty": 2.0,
            "new_qty": 0.0,
            "delta": -2.0,
            "reason": "VOID-KITCHEN",
            "note": "",
            "is_waste": 0,
        }
        token = self.h.override.mint_token("cashier@example.test", "manager@example.test", "Till A", "void_sent_item", inv.name, claim)
        res = self.qty_mod.reduce_order_item_qty(
            inv.name, "ROW-1", 0, reason="VOID-KITCHEN", approval_token=token
        )
        self.assertEqual(res["new_qty"], 0)
        # ERPNext v16 validate_qty_is_not_zero refuses qty==0 rows on draft POS Invoices.
        self.assertEqual([r.name for r in inv.items], ["ROW-2"])
        self.assertEqual(res["approver"], "manager@example.test")

    def test_waste_reason_posts_native_material_issue_stock_entry_and_links_kot(self):
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_invoice()
        res = self.qty_mod.reduce_order_item_qty(
            inv.name, "ROW-1", 1, reason="WASTE-SPOILED"
        )
        self.assertTrue(res["is_waste"])
        self.assertIsNotNone(res["waste_stock_entry"])
        self.assertEqual(len(self.h.stock_entries), 1)
        se = self.h.stock_entries[0]
        self.assertEqual(se.purpose, "Material Issue")
        self.assertEqual(se.stock_entry_type, "Wastage")
        self.assertEqual(se.custom_wastage_reason, "Spoiled")
        self.assertEqual(se.company, "Hotel Co")
        self.assertEqual(se.branch, "Branch A")
        self.assertEqual(se.docstatus, 1)
        self.assertEqual(len(se.items), 1)
        self.assertEqual(se.items[0].item_code, "STEAK")
        self.assertEqual(se.items[0].qty, 1.0)
        self.assertEqual(se.items[0].s_warehouse, "Kitchen - HC")
        self.assertEqual(se.items[0].expense_account, "Wastage and Spoilage - HC")
        # Linked on cancel KOT and audit log written for solo manager use.
        cancel_kot = self.h.kots[-1]
        self.assertEqual(cancel_kot.get("custom_waste_stock_entry"), se.name)
        self.assertIn("[WASTE]", cancel_kot.comments)
        self.assertTrue(any(l.privilege == "reduce_sent_qty" and l.approver == "manager@example.test" for l in self.h.auth_logs))

    def test_waste_on_non_stock_item_skips_stock_entry_cleanly(self):
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_invoice(items=[
            {"name": "ROW-1", "item_code": "CORKAGE", "item_name": "Corkage Fee", "qty": 2, "rate": 20000, "ury_held": 0, "reservation_line_key": "c1"},
            {"name": "ROW-2", "item_code": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "s1"},
        ])
        res = self.qty_mod.reduce_order_item_qty(inv.name, "ROW-1", 1, reason="WASTE-SPOILED")
        self.assertTrue(res["is_waste"])
        self.assertIsNone(res["waste_stock_entry"])
        self.assertEqual(self.h.stock_entries, [])

    def test_no_privilege_rules_keeps_till_usable_manager_alone_cashier_needs_pin(self):
        self.h.privilege_rules.clear()
        inv = self.h.add_invoice()
        # Cashier without rule rows still needs PIN
        self.h.frappe.session.user = "cashier@example.test"
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.qty_mod.reduce_order_item_qty(inv.name, "ROW-1", 1, reason="VOID-GUEST")
        # Today's manager role may act alone when no rules are configured
        self.h.frappe.session.user = "manager@example.test"
        res = self.qty_mod.reduce_order_item_qty(inv.name, "ROW-1", 1, reason="VOID-GUEST")
        self.assertEqual(res["new_qty"], 1)
        self.assertTrue(len(self.h.auth_logs) >= 1)

    def test_reduce_sent_item_refuses_closed_other_outlet_and_partially_paid(self):
        # Closed check
        self.h.frappe.session.user = "manager@example.test"
        closed = self.h.add_invoice("POS-CLOSED", docstatus=1)
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-CLOSED-USE-RETURN"):
            self.qty_mod.reduce_order_item_qty(closed.name, "ROW-1", 1, reason="VOID-GUEST")

        # Other outlet's check
        self.h.frappe.session.user = "other_outlet_mgr@example.test"
        inv_a = self.h.add_invoice("POS-A-1", branch="Branch A")
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-OUTLET-MISMATCH"):
            self.qty_mod.reduce_order_item_qty(inv_a.name, "ROW-1", 1, reason="VOID-GUEST")

        # Partially paid open check: total 85,000, received tender 60,000; reducing Steak (35,000) drops to 50,000 < 60,000
        self.h.frappe.session.user = "manager@example.test"
        part = self.h.add_invoice(
            "POS-PART-1",
            payments=[{"mode_of_payment": "Cash", "amount": 60000, "custom_tender_received": 1}],
        )
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-PARTIAL-PAYMENT"):
            self.qty_mod.reduce_order_item_qty(part.name, "ROW-1", 1, reason="VOID-GUEST")

    # --- 3. sync_order line reduction + adversarial cases ---

    def test_sync_order_sent_reduction_requires_reason_and_pin_even_with_remove_items_on(self):
        self.h.frappe.session.user = "cashier@example.test"
        inv = self.h.add_invoice(invoice_printed=0)
        reduced_items = [
            {"name": "ROW-1", "item": "STEAK", "item_name": "Steak", "qty": 1, "rate": 35000, "ury_held": 0, "reservation_line_key": "line-1"},
            {"name": "ROW-2", "item": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "line-2"},
        ]
        # Cashier without token -> refused with POS-AUTH-REQUIRED even though remove_items == 1
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.order_mod.sync_order(
                items=reduced_items, cashier="cashier@example.test", owner="waiter1@example.test",
                mode_of_payment="Cash", customer="Walk-in", no_of_pax=2, last_invoice=inv.name,
                waiter="waiter1@example.test", pos_profile="Till A", last_modified_time=inv.modified,
                table="T1", invoice=inv.name, order_type="Dine In", room="Main Hall",
            )
        # Manager acting alone without reason -> refused with POS-AUTH-REASON even though remove_items == 1
        self.h.frappe.session.user = "manager@example.test"
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-AUTH-REASON"):
            self.order_mod.sync_order(
                items=reduced_items, cashier="cashier@example.test", owner="waiter1@example.test",
                mode_of_payment="Cash", customer="Walk-in", no_of_pax=2, last_invoice=inv.name,
                waiter="waiter1@example.test", pos_profile="Till A", last_modified_time=inv.modified,
                table="T1", invoice=inv.name, order_type="Dine In", room="Main Hall",
            )
        # Cashier with reason but no manager PIN -> refused with POS-AUTH-REQUIRED
        self.h.frappe.session.user = "cashier@example.test"
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.order_mod.sync_order(
                items=reduced_items, cashier="cashier@example.test", owner="waiter1@example.test",
                mode_of_payment="Cash", customer="Walk-in", no_of_pax=2, last_invoice=inv.name,
                waiter="waiter1@example.test", pos_profile="Till A", last_modified_time=inv.modified,
                table="T1", invoice=inv.name, order_type="Dine In", room="Main Hall",
                void_reason="WASTE-SPOILED",
            )
        # With reason + valid manager PIN -> succeeds, links waste Stock Entry & cancel KOT reason!
        claim = {
            "invoice": inv.name,
            "reductions": [{"delta": -1.0, "from_qty": 2.0, "item_code": "STEAK", "line_key": "line-1", "to_qty": 1.0}],
            "reason": "WASTE-SPOILED",
            "note": "",
            "is_waste": 1,
        }
        token = self.h.override.mint_token("cashier@example.test", "manager@example.test", "Till A", "reduce_sent_qty", inv.name, claim)
        self.order_mod.sync_order(
            items=reduced_items, cashier="cashier@example.test", owner="waiter1@example.test",
            mode_of_payment="Cash", customer="Walk-in", no_of_pax=2, last_invoice=inv.name,
            waiter="waiter1@example.test", pos_profile="Till A", last_modified_time=inv.modified,
            table="T1", invoice=inv.name, order_type="Dine In", room="Main Hall",
            void_reason="WASTE-SPOILED", approval_token=token,
        )
        self.assertEqual(len(self.h.stock_entries), 1)
        self.assertEqual(self.h.stock_entries[0].purpose, "Material Issue")
        self.assertIn("[WASTE]", self.h.kots[-1].comments)

    def test_adversarial_line_cut_to_zero_and_re_added(self):
        # Part A: Cut sent line ROW-1 (Steak x2) to 0 via reduce_order_item_qty with manager auth,
        # then cashier re-adds Steak x1 as a fresh line via sync_order -> succeeds with new KOT, no double void!
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_invoice(invoice_printed=0)
        self.qty_mod.reduce_order_item_qty(inv.name, "ROW-1", 0, reason="VOID-GUEST")
        self.assertEqual([r.name for r in inv.items], ["ROW-2"])
        kots_after_cut = len(self.h.kots)

        self.h.frappe.session.user = "cashier@example.test"
        self.order_mod.sync_order(
            items=[
                {"name": "ROW-2", "item": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "line-2"},
                {"item": "STEAK", "item_name": "Steak", "qty": 1, "rate": 35000, "ury_held": 0, "reservation_line_key": "line-3"},
            ],
            cashier="cashier@example.test", owner="waiter1@example.test", mode_of_payment="Cash",
            customer="Walk-in", no_of_pax=2, last_invoice=inv.name, waiter="waiter1@example.test",
            pos_profile="Till A", last_modified_time=inv.modified, table="T1", invoice=inv.name,
            order_type="Dine In", room="Main Hall",
        )
        new_kots = self.h.kots[kots_after_cut:]
        self.assertTrue(any(k.type in ("New Order", "Order Modified") for k in new_kots))
        self.assertFalse(any(k.type == "Partially cancelled" for k in new_kots))

        # Part B: Cutting line-2 to 0 while simultaneously adding a new SOUP line-4 in one sync_order
        # MUST still trigger the sent-item void gate (auth for cashier, reason for manager)!
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.order_mod.sync_order(
                items=[
                    {"item": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "line-4"},
                    {"item": "STEAK", "item_name": "Steak", "qty": 1, "rate": 35000, "ury_held": 0, "reservation_line_key": "line-3"},
                ],
                cashier="cashier@example.test", owner="waiter1@example.test", mode_of_payment="Cash",
                customer="Walk-in", no_of_pax=2, last_invoice=inv.name, waiter="waiter1@example.test",
                pos_profile="Till A", last_modified_time=inv.modified, table="T1", invoice=inv.name,
                order_type="Dine In", room="Main Hall",
            )
        self.h.frappe.session.user = "manager@example.test"
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-AUTH-REASON"):
            self.order_mod.sync_order(
                items=[
                    {"item": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "line-4"},
                    {"item": "STEAK", "item_name": "Steak", "qty": 1, "rate": 35000, "ury_held": 0, "reservation_line_key": "line-3"},
                ],
                cashier="cashier@example.test", owner="waiter1@example.test", mode_of_payment="Cash",
                customer="Walk-in", no_of_pax=2, last_invoice=inv.name, waiter="waiter1@example.test",
                pos_profile="Till A", last_modified_time=inv.modified, table="T1", invoice=inv.name,
                order_type="Dine In", room="Main Hall",
            )

    def test_adversarial_offline_replay_cannot_void_sent_lines(self):
        self.h.frappe.session.user = "manager@example.test"
        self.h.profiles["Till A"].ury_offline_capture = 1
        inv = self.h.add_invoice(invoice_printed=0)
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-OFFLINE-REFUSED"):
            self.order_mod.sync_order(
                items=[
                    {"name": "ROW-1", "item": "STEAK", "item_name": "Steak", "qty": 1, "rate": 35000, "ury_held": 0, "reservation_line_key": "line-1"},
                    {"name": "ROW-2", "item": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "line-2"},
                ],
                cashier="cashier@example.test", owner="waiter1@example.test", mode_of_payment="Cash",
                customer="Walk-in", no_of_pax=2, last_invoice=inv.name, waiter="waiter1@example.test",
                pos_profile="Till A", last_modified_time=inv.modified, table="T1", invoice=inv.name,
                order_type="Dine In", room="Main Hall",
                captured_at="2026-11-24T10:01:00Z", void_reason="VOID-GUEST",
            )

    def test_adversarial_sync_order_cannot_reduce_partially_paid_check_below_received_tender(self):
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_invoice(
            "POS-SYNC-PART",
            invoice_printed=0,
            payments=[{"mode_of_payment": "Cash", "amount": 60000, "custom_tender_received": 1}],
        )
        # Dropping Steak 2 -> 1 reduces total from 85,000 to 50,000 < 60,000 already received!
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-PARTIAL-PAYMENT"):
            self.order_mod.sync_order(
                items=[
                    {"name": "ROW-1", "item": "STEAK", "item_name": "Steak", "qty": 1, "rate": 35000, "ury_held": 0, "reservation_line_key": "line-1"},
                    {"name": "ROW-2", "item": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 0, "reservation_line_key": "line-2"},
                ],
                cashier="cashier@example.test", owner="waiter1@example.test", mode_of_payment="Cash",
                customer="Walk-in", no_of_pax=2, last_invoice=inv.name, waiter="waiter1@example.test",
                pos_profile="Till A", last_modified_time=inv.modified, table="T1", invoice=inv.name,
                order_type="Dine In", room="Main Hall",
                void_reason="VOID-GUEST",
            )

    # --- 4. Whole-check cancel (row 8) ---

    def test_cancel_sent_open_check_with_manager_pin_keeps_cashier_session_and_links_kot_and_waste(self):
        self.h.frappe.session.user = "cashier@example.test"
        inv = self.h.add_invoice(
            items=[
                {"name": "ROW-1", "item_code": "STEAK", "item_name": "Steak", "qty": 2, "rate": 35000, "ury_held": 0, "reservation_line_key": "line-1"},
                {"name": "ROW-HELD", "item_code": "SOUP", "item_name": "Soup", "qty": 1, "rate": 15000, "ury_held": 1, "reservation_line_key": "line-2"},
            ]
        )
        # Cashier lacks cancel DocPerm & cancel_check privilege -> needs manager PIN
        with self.assertRaises(self.h.frappe.PermissionError):
            self.order_mod.cancel_order(inv.name, "WASTE-SPOILED")

        claim = {"invoice": inv.name, "reason": "WASTE-SPOILED", "note": "", "is_waste": 1}
        token = self.h.override.mint_token("cashier@example.test", "manager@example.test", "Till A", "cancel_check", inv.name, claim)
        res = self.order_mod.cancel_order(inv.name, "WASTE-SPOILED", approval_token=token)

        self.assertEqual(inv.docstatus, 2)
        self.assertEqual(self.h.frappe.session.user, "cashier@example.test")
        self.assertEqual(res["approver"], "manager@example.test")
        # Cancel KOT carries waste reason + Stock Entry link; Stock Entry covers ONLY sent item (STEAK x2), NOT held SOUP!
        cancel_kot = self.h.kots[-1]
        self.assertEqual(cancel_kot.type, "Cancelled")
        self.assertIn("[WASTE]", cancel_kot.comments)
        self.assertEqual(len(self.h.stock_entries), 1)
        self.assertEqual([(r.item_code, r.qty) for r in self.h.stock_entries[0].items], [("STEAK", 2.0)])
        self.assertEqual(cancel_kot.get("custom_waste_stock_entry"), self.h.stock_entries[0].name)

    def test_cancel_unsent_all_held_open_check_stays_free_for_check_owner(self):
        self.h.frappe.session.user = "waiter1@example.test"
        inv = self.h.add_invoice(
            invoice_printed=0,
            has_sent_kot=False,
            items=[
                {"name": "ROW-HELD", "item_code": "STEAK", "item_name": "Steak", "qty": 1, "rate": 35000, "ury_held": 1, "reservation_line_key": "held-1"},
            ],
        )
        self.order_mod.cancel_order(inv.name, "VOID-GUEST")
        self.assertEqual(inv.docstatus, 2)
        self.assertEqual(self.h.stock_entries, [])
        self.assertEqual(self.h.kots, [])

    def test_cancel_closed_check_is_refused_must_use_return(self):
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_invoice(docstatus=1)
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-CLOSED-USE-RETURN"):
            self.order_mod.cancel_order(inv.name, "VOID-GUEST")
        self.assertEqual(inv.docstatus, 1)

    def test_cancel_partially_paid_open_check_is_refused(self):
        self.h.frappe.session.user = "manager@example.test"
        inv = self.h.add_invoice(payments=[{"mode_of_payment": "Cash", "amount": 20000, "custom_tender_received": 1}])
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-PARTIAL-PAYMENT"):
            self.order_mod.cancel_order(inv.name, "VOID-GUEST")
        self.assertEqual(inv.docstatus, 0)

    def test_cancel_another_servers_check_refused_for_plain_waiter(self):
        self.h.frappe.session.user = "waiter2@example.test"
        inv = self.h.add_invoice(invoice_printed=0, has_sent_kot=False, waiter="waiter1@example.test", owner="waiter1@example.test",
                                 items=[{"name": "ROW-H", "item_code": "STEAK", "item_name": "Steak", "qty": 1, "rate": 35000, "ury_held": 1}])
        with self.assertRaises(self.h.frappe.PermissionError):
            self.order_mod.cancel_order(inv.name, "VOID-GUEST")
        self.assertEqual(inv.docstatus, 0)

    # --- 5. Mutation-level hooks: REST / Desk bypass blocked ---

    def test_validate_invoice_blocks_direct_rest_reduction_of_sent_kot_item(self):
        self.h.frappe.session.user = "cashier@example.test"
        orig = self.h.add_invoice("POS-REST-1", invoice_printed=0, has_sent_kot=True)
        # Simulated direct REST save reducing STEAK qty 2 -> 1 with NO server void flag
        edited = Doc(orig)
        edited["items"] = [
            Doc(orig.items[0], qty=1),
            Doc(orig.items[1]),
        ]
        with self.assertRaisesRegex(self.h.frappe.PermissionError, "POS-AUTH-REQUIRED"):
            self.inv_hook.validate_invoice(edited, "validate")

    def test_before_cancel_blocks_direct_cancel_of_closed_and_fiscalised_pos_checks(self):
        self.h.frappe.session.user = "manager@example.test"
        fiscal = self.h.add_invoice("POS-FISC-1", docstatus=1, efris_irn="FDN-999")
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-FISCALISED-USE-RETURN"):
            self.inv_hook.before_cancel(fiscal, "before_cancel")

        plain_closed = self.h.add_invoice("POS-PLAIN-1", docstatus=1, efris_irn=None)
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-CLOSED-USE-RETURN"):
            self.inv_hook.before_cancel(plain_closed, "before_cancel")

    def test_void_return_reopen_has_zero_hardcoded_role_names_and_delegates_to_override(self):
        src = (ROOT / "ury/ury_pos/void_return_reopen.py").read_text(encoding="utf-8")
        self.assertNotIn("DEFAULT_MANAGER_ROLES", src)
        for forbidden_role in ("URY Manager", "URY Admin", "Accounts Manager", "URY Cashier", "System Manager"):
            self.assertNotIn(forbidden_role, src)

    def test_cancel_order_refuses_fiscalised_check_even_if_in_memory_efris_status_cleared(self):
        self.h.frappe.session.user = "manager@example.test"
        orig_before = Doc(efris_irn=None, efris_status="Submitted")
        fiscal = self.h.add_invoice("POS-FISC-CLEARED", docstatus=1, efris_irn=None)
        fiscal.efris_status = ""
        fiscal.get_doc_before_save = lambda: orig_before
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-FISCALISED-USE-RETURN"):
            self.order_mod.cancel_order(fiscal.name, "VOID-GUEST")
        with self.assertRaisesRegex(self.h.frappe.ValidationError, "POS-VOID-FISCALISED-USE-RETURN"):
            self.inv_hook.before_cancel(fiscal, "before_cancel")

    def test_select_token_claim_uses_isolated_db_when_main_tx_snapshot_is_stale(self):
        """Real MariaDB REPEATABLE READ keeps main frappe.db blind to isolated-DB Approved rows."""
        import hashlib
        import json
        import types

        from ury.ury_pos import void_return_reopen as vr

        token = "tok-isolated-147-r2"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        b5a_claim = {"invoice": "POS-ISO-1", "item_row_name": "ROW-1", "previous_qty": 2.0, "new_qty": 1.0}
        b2a_claim = {
            "action": "reduce_order_item_qty",
            "item_row_name": "ROW-1",
            "item_code": "STEAK",
            "from_qty": 2.0,
            "to_qty": 1.0,
            "amount": 25.0,
            "reason": "Guest changed mind",
        }
        stored_row = {
            "privilege": "void_sent_item",
            "claim_values": json.dumps(b2a_claim, sort_keys=True, separators=(",", ":")),
        }

        class FakeIsolatedDB:
            def __init__(self):
                self.queries = []

            def sql(self, query, values=None, as_dict=False):
                self.queries.append((query, values, as_dict))
                if values and values[0] == token_hash:
                    return [stored_row]
                return []

        iso_db = FakeIsolatedDB()
        ov_stub = types.SimpleNamespace(
            _token_digest=lambda t: hashlib.sha256(t.encode()).hexdigest(),
            _has_real_isolated_db=lambda: True,
            _with_isolated_db=lambda fn, commit=False: fn(iso_db),
        )
        # Main connection snapshot has no Approved row (simulates MariaDB REPEATABLE READ).
        chosen_priv, chosen_claim = vr._select_token_claim_and_privilege(
            ov_stub,
            token,
            "reduce_sent_qty",
            b5a_claim,
            ("void_sent_item",),
            b2a_claim,
        )
        self.assertEqual(chosen_priv, "void_sent_item")
        self.assertEqual(chosen_claim, b2a_claim)
        self.assertEqual(len(iso_db.queries), 1)


if __name__ == "__main__":
    unittest.main()
