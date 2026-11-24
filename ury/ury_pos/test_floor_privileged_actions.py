"""Site-less RED/GREEN unit + adversarial tests for Scope f floor privileged actions.

Covers matrix rows 16-22, 36, 37 and adversarial controls:
- Transfer check / table / server / cross-outlet (`transfer_check`) with PIN in place
  and legacy `transfer_role_permissions` fallback when no rule rows exist
- Adversarial: transfer onto an occupied table (both `occupied=1` and open draft check)
- Adversarial: two waiters transferring the same check at once (`FOR UPDATE` + conflict check)
- Pick up another server's check (`pickup_other_check`) keeping opener `waiter` sticky
- Merge bills (`merge_bills`) and merge tables (`merge_tables_batch`) via `merge_checks`
- Split check (`split_bill`) via `split_check` + Simphony Opt 102 `custom_max_splits_before_pin`
- Adversarial: split then reprint (parent keeps count, child starts at 0)
- Guest count (`guest_count_change`) and order type (`order_type_change`) changes
- Reprints: open bill free count (`custom_max_free_bill_prints`), closed receipt copies
  (`custom_max_free_closed_copies`), kitchen re-fire (`reprint_kot`), COPY / RE-FIRE marks
- Adversarial: reprint count across devices (DB-locked authoritative count)
- 86 availability: Sold Out stays easy; restore / count / reset_policy needs `item_86_restore` / PIN
- Reservation cancel / No Show: mandatory reason + `reservation_cancel` / PIN; Seated stays open
- Adversarial: offline replay refused (`POS-OFFLINE-PRIVILEGED-REFUSED`) for every Scope f action
"""

from __future__ import annotations

import copy
import datetime as dt
import importlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]  # ury/


class ValidationError(Exception):
    pass


class PermissionError_(Exception):
    pass


class DoesNotExistError(Exception):
    pass


class UniqueValidationError(Exception):
    pass


class FakeDoc(dict):
    def __init__(self, data=None, **kwargs):
        super().__init__(data or {})
        self.update(kwargs)
        object.__setattr__(self, "flags", SimpleNamespace())
        object.__setattr__(self, "_before_save", None)

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        if key in ("flags", "_before_save"):
            object.__setattr__(self, key, value)
        else:
            self[key] = value

    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value):
        self[key] = value

    def append(self, key, row):
        arr = self.setdefault(key, [])
        doc = row if isinstance(row, FakeDoc) else FakeDoc(row)
        arr.append(doc)
        return doc

    @property
    def items(self):
        return self.get("items") or []

    @items.setter
    def items(self, value):
        self["items"] = value

    def remove(self, item):
        for arr in dict.values(self):
            if isinstance(arr, list) and item in arr:
                arr.remove(item)
                return

    def as_dict(self, **kwargs):
        out = {}
        for k, v in dict.items(self):
            if isinstance(v, list):
                out[k] = [x.as_dict() if isinstance(x, FakeDoc) else copy.deepcopy(x) for x in v]
            elif not k.startswith("_"):
                out[k] = copy.deepcopy(v)
        return out

    def check_permission(self, ptype="write"):
        return True

    def is_new(self):
        return not bool(self.get("name"))

    def get_doc_before_save(self):
        return object.__getattribute__(self, "_before_save")

    def precision(self, field):
        return 2

    def set_missing_values(self, *args, **kwargs):
        pass

    def run_method(self, *args, **kwargs):
        pass

    def calculate_taxes_and_totals(self):
        net = sum(float(i.get("qty") or 0) * float(i.get("rate") or 0) for i in (self.get("items") or []))
        tax_total = sum(float(t.get("tax_amount") or 0) for t in (self.get("taxes") or []))
        self["net_total"] = round(net, 2)
        self["total"] = round(net, 2)
        self["total_taxes_and_charges"] = round(tax_total, 2)
        self["grand_total"] = round(net + tax_total, 2)
        self["rounded_total"] = round(net + tax_total, 2)

    def set_paid_amount(self):
        self["paid_amount"] = sum(float(p.get("amount") or 0) for p in (self.get("payments") or []))

    def set_outstanding_amount(self):
        self["outstanding_amount"] = max(0.0, float(self.get("grand_total") or 0) - float(self.get("paid_amount") or 0))

    def save(self, **kwargs):
        return self

    def insert(self, **kwargs):
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


class FakeOverride:
    """Contract-exact B2a `ury.ury_pos.override` double."""

    def __init__(self, harness):
        self.h = harness
        self.tokens = {}
        self.consumed = []
        self.require_calls = []

    def issue_token(self, approver, privilege, pos_profile, reference_name, claim, reason=""):
        token = f"TOK-{len(self.tokens) + 1}"
        self.tokens[token] = {
            "approver": approver,
            "privilege": privilege,
            "pos_profile": pos_profile,
            "reference_name": reference_name,
            "claim": claim,
            "reason": reason,
            "used": False,
        }
        return token

    def is_policy_active(self, company):
        if not isinstance(company, str) or not company.strip():
            return False
        return any(r.get("company") == company for r in self.h.rules)

    def requires_reason(self, pos_profile, privilege):
        company = self.h.profiles.get(pos_profile, {}).get("company") if pos_profile else None
        matched = [r for r in self.h.rules if r.get("company") == company and r.get("privilege") == privilege]
        if matched:
            return any(int(r.get("requires_reason") or 0) == 1 for r in matched)
        return privilege in {
            "price_override",
            "complimentary",
            "tax_exempt",
            "service_charge_waive",
            "tip_adjust",
            "reprint_bill",
            "reprint_kot",
            "reservation_cancel",
        }

    def may_perform(self, user, pos_profile, privilege, percent=None, amount=None, privilege_group=0):
        roles = set(self.h.roles.get(user, []))
        company = self.h.profiles.get(pos_profile, {}).get("company") if pos_profile else None
        rules = [
            r for r in self.h.rules
            if r.get("company") == company
            and r.get("privilege") == privilege
            and r.get("role") in roles
            and r.get("pos_profile") in (pos_profile, "", None)
        ]
        if not rules:
            return False
        for r in rules:
            if int(r.get("may_perform") or 0) != 1:
                continue
            grp = int(r.get("privilege_group") or 0)
            if privilege_group and grp != 0 and grp < privilege_group:
                continue
            return True
        return False

    def consume(self, token, privilege, pos_profile, reference_name, claim):
        rec = self.tokens.get(token)
        if not rec or rec["used"]:
            self.h.frappe.throw("Approval token expired or already used. [POS-AUTH-EXPIRED]", ValidationError)
        if rec["privilege"] != privilege or rec["reference_name"] != reference_name:
            self.h.frappe.throw("Approval details changed; authorise again. [POS-AUTH-CHANGED]", ValidationError)
        if rec["approver"] == self.h.frappe.session.user:
            self.h.frappe.throw("Self-approval is not allowed. [POS-AUTH-SELF]", PermissionError_)
        rec["used"] = True
        self.consumed.append(dict(rec, token=token, claim=claim))
        self.h.logs.append(
            FakeDoc(
                doctype="POS Authorisation Log",
                requester=self.h.frappe.session.user,
                approver=rec["approver"],
                privilege=privilege,
                pos_profile=pos_profile,
                reference_name=reference_name,
                outcome="Consumed",
                claim_values=json.dumps(claim, sort_keys=True),
                reason=rec.get("reason", ""),
            )
        )
        return {"approver": rec["approver"], "reason": rec.get("reason", "")}

    def require(self, privilege, pos_profile, reference_name, claim, token=None, percent=None, amount=None, privilege_group=0):
        self.require_calls.append((privilege, pos_profile, reference_name, claim, token))
        if token:
            return self.consume(token, privilege, pos_profile, reference_name, claim)
        user = self.h.frappe.session.user
        if self.may_perform(user, pos_profile, privilege, percent=percent, amount=amount, privilege_group=privilege_group):
            return None
        self.h.frappe.throw("A manager must approve this action. [POS-AUTH-REQUIRED]", PermissionError_)

    def mark_verified_approval(self, doc, privilege, approval=None):
        doc.flags.__dict__[f"pos_override_verified_{privilege}"] = ("VERIFIED", privilege, str(doc.get("name") or ""))

    def has_verified_approval(self, doc, privilege):
        val = getattr(doc, "flags", SimpleNamespace()).__dict__.get(f"pos_override_verified_{privilege}")
        return val == ("VERIFIED", privilege, str(doc.get("name") or ""))


class FloorHarness:
    """In-memory Frappe environment for Scope f floor privileged actions."""

    def __init__(self):
        self.company = "Grand Hotel Ltd"
        self.branch = "Main Restaurant"
        self.branch_2 = "Rooftop Bar"
        self.roles = {
            "captain_a@example.com": ["URY Captain"],
            "captain_b@example.com": ["URY Captain"],
            "cashier@example.com": ["URY Cashier"],
            "kitchen_lead@example.com": ["Kitchen User", "Kitchen Lead"],
            "manager@example.com": ["URY Manager"],
            "gm@example.com": ["URY Manager", "URY Admin"],
        }
        self.users = {
            u: FakeDoc(name=u, full_name=u.split("@")[0].replace("_", " ").title(), enabled=1)
            for u in self.roles
        }
        self.profiles = {
            "Main Till": FakeDoc(
                name="Main Till",
                company=self.company,
                branch=self.branch,
                restaurant="Main Resto",
                currency="UGX",
                disabled=0,
                print_format="POS Invoice",
                remove_items=0,
                custom_enable_multiple_cashier=0,
                custom_edit_order_type=0,
                custom_enable_kot_reprint=1,
                custom_reprint_kot_format="KOT Reprint",
                custom_table_order_printer="Kitchen Printer",
                custom_parcel_order_printer="Parcel Printer",
                custom_max_free_bill_prints=1,
                custom_max_free_closed_copies=0,
                custom_max_splits_before_pin=0,
                custom_service_charge_rate=0,
                custom_service_charge_account=None,
                transfer_role_permissions=[FakeDoc(role="URY Manager")],
                role_allowed_for_billing=[FakeDoc(role="URY Cashier"), FakeDoc(role="URY Manager")],
                role_restricted_for_table_order=[FakeDoc(role="Kitchen User")],
                applicable_for_users=[FakeDoc(user="cashier@example.com")],
                payments=[FakeDoc(mode_of_payment="Cash", default=1)],
            ),
            "Rooftop Till": FakeDoc(
                name="Rooftop Till",
                company=self.company,
                branch=self.branch_2,
                restaurant="Rooftop Resto",
                currency="UGX",
                disabled=0,
                print_format="POS Invoice",
                custom_max_free_bill_prints=1,
                custom_max_free_closed_copies=0,
                transfer_role_permissions=[FakeDoc(role="URY Manager")],
                role_allowed_for_billing=[FakeDoc(role="URY Cashier"), FakeDoc(role="URY Manager")],
                role_restricted_for_table_order=[],
                applicable_for_users=[FakeDoc(user="cashier@example.com")],
                payments=[FakeDoc(mode_of_payment="Cash", default=1)],
            ),
        }
        self.branches = {
            self.branch: FakeDoc(name=self.branch, company=self.company, custom_enable_reservation=1, custom_no_taxes=0),
            self.branch_2: FakeDoc(name=self.branch_2, company=self.company, custom_enable_reservation=1, custom_no_taxes=0),
        }
        self.restaurants = {
            "Main Resto": FakeDoc(name="Main Resto", branch=self.branch, active_menu="Main Menu", room_wise_menu=0, order_type_wise_menu=0),
            "Rooftop Resto": FakeDoc(name="Rooftop Resto", branch=self.branch_2, active_menu="Rooftop Menu", room_wise_menu=0, order_type_wise_menu=0),
        }
        self.items_master = {
            "STEAK": FakeDoc(name="STEAK", item_code="STEAK", item_name="Ribeye Steak", custom_allow_zero_price=0, custom_is_open_priced=0),
            "WINE": FakeDoc(name="WINE", item_code="WINE", item_name="House Red", custom_allow_zero_price=0, custom_is_open_priced=0),
            "SALAD": FakeDoc(name="SALAD", item_code="SALAD", item_name="Garden Salad", custom_allow_zero_price=0, custom_is_open_priced=0),
        }
        self.tables = {
            "T1": FakeDoc(name="T1", branch=self.branch, restaurant="Main Resto", restaurant_room="Main Hall", occupied=1, merged_with=None),
            "T2": FakeDoc(name="T2", branch=self.branch, restaurant="Main Resto", restaurant_room="Main Hall", occupied=0, merged_with=None),
            "T3": FakeDoc(name="T3", branch=self.branch, restaurant="Main Resto", restaurant_room="Main Hall", occupied=0, merged_with=None),
            "RT-1": FakeDoc(name="RT-1", branch=self.branch_2, restaurant="Rooftop Resto", restaurant_room="Sky Deck", occupied=0, merged_with=None),
            "RT-2": FakeDoc(name="RT-2", branch=self.branch_2, restaurant="Rooftop Resto", restaurant_room="Sky Deck", occupied=1, merged_with=None),
        }
        self.invoices = {}
        self.reservations = {}
        self.availability_rows = {}
        self.reprint_logs = []
        self.rules = []
        self.reasons = [
            FakeDoc(code="GUEST_REQUEST", description="Guest requested copy", applies_to="reprint_bill", requires_note=0, disabled=0, sort_order=1),
            FakeDoc(code="PRINTER_JAM", description="Printer jam / paper out", applies_to="reprint_bill,reprint_kot", requires_note=0, disabled=0, sort_order=2),
            FakeDoc(code="KITCHEN_REFIRE", description="Kitchen re-fire required", applies_to="reprint_kot", requires_note=0, disabled=0, sort_order=3),
            FakeDoc(code="GUEST_CANCELLED", description="Guest cancelled booking", applies_to="reservation_cancel", requires_note=0, disabled=0, sort_order=4),
            FakeDoc(code="NO_SHOW", description="Party did not arrive", applies_to="reservation_cancel", requires_note=0, disabled=0, sort_order=5),
            FakeDoc(code="SHIFT_HANDOVER", description="Shift handover", applies_to="transfer_check,pickup_other_check", requires_note=0, disabled=0, sort_order=6),
        ]
        self.logs = []
        self.locks = []
        self.printer_calls = []
        self.kot_transfer_calls = []
        self.kot_reprint_calls = []
        self.consolidate_calls = []
        self.customers = {
            "Walk-in Guest": FakeDoc(name="Walk-in Guest", customer_name="Walk-in Guest", mobile_number=""),
        }
        self.branch_users = [
            {"parent": self.branch, "parenttype": "Branch", "user": u, "room": "Main Hall"}
            for u in self.roles
        ]
        self.next_split_seq = 1
        self.frappe = self._build_frappe()
        self.override = FakeOverride(self)

    def add_invoice(self, name="INV-1", **overrides):
        inv = FakeDoc(
            doctype="POS Invoice",
            name=name,
            docstatus=0,
            status="Draft",
            company=self.company,
            branch=self.branch,
            restaurant="Main Resto",
            restaurant_table="T1",
            custom_restaurant_room="Main Hall",
            pos_profile="Main Till",
            waiter="captain_a@example.com",
            cashier="cashier@example.com",
            customer="Walk-in Guest",
            customer_name="Walk-in Guest",
            order_type="Dine In",
            no_of_pax=4,
            currency="UGX",
            conversion_rate=1.0,
            selling_price_list="Standard Selling",
            invoice_printed=0,
            invoice_created=1,
            custom_bill_print_count=0,
            custom_kot_reprint_count=0,
            custom_merged_pos_invoice=None,
            custom_split_from=None,
            custom_split_group=None,
            creation=dt.datetime(2026, 11, 24, 12, 0, 0),
            modified=dt.datetime(2026, 11, 24, 12, 0, 0),
            items=[
                FakeDoc(name=f"{name}-L1", item_code="STEAK", item_name="Ribeye Steak", qty=2, rate=50000, amount=100000, reservation_line_key="rk-1", ury_held=0),
                FakeDoc(name=f"{name}-L2", item_code="WINE", item_name="House Red", qty=1, rate=30000, amount=30000, reservation_line_key="rk-2", ury_held=0),
            ],
            taxes=[],
            payments=[FakeDoc(mode_of_payment="Cash", amount=0, custom_tender_received=0)],
        )
        inv.update(overrides)
        inv.calculate_taxes_and_totals()
        self.invoices[name] = inv
        return inv

    def add_reservation(self, name="RES-1", **overrides):
        res = FakeDoc(
            doctype="URY Table Reservation",
            name=name,
            branch=self.branch,
            reserved_table="T2",
            customer="Walk-in Guest",
            customer_name="Walk-in Guest",
            customer_phone="+256700000001",
            no_of_pax=4,
            status="Confirmed",
            reserved_at="2026-11-24 19:00:00",
            cancel_reason=None,
            custom_cancel_approver=None,
        )
        res.update(overrides)
        object.__setattr__(res, "_before_save", FakeDoc(dict(res)))
        self.reservations[name] = res
        return res

    def _build_frappe(self):
        h = self
        f = ModuleType("frappe")
        f.ValidationError = ValidationError
        f.PermissionError = PermissionError_
        f.DoesNotExistError = DoesNotExistError
        f.UniqueValidationError = UniqueValidationError
        f.STANDARD_USERS = ("Administrator", "Guest")
        f.session = SimpleNamespace(user="captain_a@example.com")
        f.flags = SimpleNamespace(ury_offline_replay=False, in_migrate=False, in_install=False)
        f._ = lambda msg, *args: msg
        f._dict = FakeDoc
        f.whitelist = lambda *a, **kw: (lambda fn: fn)
        f.parse_json = lambda x: json.loads(x) if isinstance(x, str) else x
        f.generate_hash = lambda length=10: f"HASH{h.next_split_seq:04d}"[:length]
        f.log_error = MagicMock()
        f.msgprint = MagicMock()
        f.publish_realtime = MagicMock()
        f.get_traceback = lambda: "traceback"
        f.cache = lambda: MagicMock()

        def throw(msg, exc=None, **kwargs):
            raise (exc or ValidationError)(msg)

        f.throw = throw
        f.get_roles = lambda user=None: list(h.roles.get(user or f.session.user, []))

        def has_permission(doctype, ptype="read", doc=None, user=None, **kwargs):
            u = user or f.session.user
            roles = set(h.roles.get(u, []))
            if doctype == "URY Table Reservation" and ptype in ("cancel_reservation", "mark_no_show"):
                return bool(roles & {"URY Manager", "URY Cashier", "URY Admin", "System Manager"})
            return u != "Guest"

        f.has_permission = has_permission

        def get_doc(arg, name=None, for_update=False, **kwargs):
            if for_update:
                h.locks.append((arg, name))
            if isinstance(arg, dict):
                dt_name = arg.get("doctype")
                doc = FakeDoc(arg)
                if dt_name == "POS Authorisation Log":
                    def _insert(**kw):
                        h.logs.append(doc)
                        return doc
                    object.__setattr__(doc, "insert", _insert)
                elif dt_name == "URY Bill Reprint Log":
                    def _insert(**kw):
                        h.reprint_logs.append(doc)
                        return doc
                    object.__setattr__(doc, "insert", _insert)
                return doc
            if arg == "POS Invoice":
                if name not in h.invoices:
                    raise DoesNotExistError(name)
                inv = h.invoices[name]
                object.__setattr__(inv, "_before_save", FakeDoc(inv.as_dict()))
                return inv
            if arg == "POS Profile":
                return h.profiles[name]
            if arg == "URY Table":
                return h.tables[name]
            if arg == "Branch":
                return h.branches[name]
            if arg == "Customer":
                return h.customers[name]
            if arg == "User":
                return h.users[name]
            if arg == "URY Table Reservation":
                res = h.reservations[name]
                object.__setattr__(res, "_before_save", FakeDoc(dict(res)))
                return res
            if arg == "URY Item Availability":
                return h.availability_rows[name]
            if arg == "Network Printer Settings":
                printer = SimpleNamespace(name=name)
                def _print_doc(**kw):
                    h.printer_calls.append({"printer": name, **kw})
                    return {"status": "Success", "extra_metadata": kw.get("extra_metadata")}
                printer.print_doc = _print_doc
                return printer
            raise DoesNotExistError(f"{arg}:{name}")

        f.get_doc = get_doc

        def new_doc(doctype):
            if doctype == "POS Invoice":
                doc = FakeDoc(doctype="POS Invoice", items=[], taxes=[], payments=[])
                def _insert(**kw):
                    doc["name"] = f"INV-SPLIT-{h.next_split_seq}"
                    h.next_split_seq += 1
                    h.invoices[doc["name"]] = doc
                    return doc
                object.__setattr__(doc, "insert", _insert)
                return doc
            if doctype == "URY Item Availability":
                doc = FakeDoc(doctype="URY Item Availability", status="Available")
                def _save(**kw):
                    doc["name"] = doc.get("name") or f"AVAIL-{doc.get('branch')}-{doc.get('item')}"
                    h.availability_rows[doc["name"]] = doc
                    return doc
                object.__setattr__(doc, "save", _save)
                return doc
            return FakeDoc(doctype=doctype)

        f.new_doc = new_doc

        def _match(row, filters):
            if not filters:
                return True
            for k, cond in filters.items():
                val = row.get(k)
                if isinstance(cond, (list, tuple)) and len(cond) == 2:
                    op, target = cond[0], cond[1]
                    if op == "in":
                        if val not in target:
                            return False
                    elif op == "!=":
                        if val == target:
                            return False
                    elif op == "like":
                        needle = str(target).strip("%")
                        if needle not in str(val or ""):
                            return False
                else:
                    if val != cond:
                        return False
            return True

        def _table_rows(doctype):
            if doctype == "POS Privilege Rule":
                return h.rules
            if doctype == "POS Reason":
                return h.reasons
            if doctype == "POS Invoice":
                return list(h.invoices.values())
            if doctype == "POS Profile":
                return list(h.profiles.values())
            if doctype == "URY Table":
                return list(h.tables.values())
            if doctype == "URY User":
                return h.branch_users
            if doctype == "URY Bill Reprint Log":
                return h.reprint_logs
            if doctype == "URY Item Availability":
                return list(h.availability_rows.values())
            if doctype == "URY Printer Settings":
                return [{"printer": "Bill Printer 1", "parent": "Main Till", "parenttype": "POS Profile", "bill": 1}]
            return []

        def get_all(doctype, filters=None, fields=None, pluck=None, limit=None, **kwargs):
            rows = [r for r in _table_rows(doctype) if _match(r, filters)]
            if limit:
                rows = rows[:limit]
            if pluck:
                return [r.get(pluck) for r in rows]
            if fields:
                return [FakeDoc({fld: r.get(fld) for fld in fields}) for r in rows]
            return [FakeDoc(dict(r)) for r in rows]

        f.get_all = get_all

        db = SimpleNamespace()
        db.has_column = lambda dt_name, col: True
        db.table_exists = lambda dt_name: dt_name in {
            "POS Privilege Rule",
            "POS Reason",
            "POS Authorisation Log",
            "POS Invoice",
            "POS Profile",
            "URY Table",
            "URY Table Reservation",
            "URY Bill Reprint Log",
            "URY Item Availability",
        }
        db.commit = MagicMock()
        db.rollback = MagicMock()

        def exists(doctype, filters=None):
            if isinstance(filters, dict):
                rows = [r for r in _table_rows(doctype) if _match(r, filters)]
                return rows[0].get("name") or True if rows else None
            if doctype == "POS Profile":
                return filters if filters in h.profiles else None
            if doctype == "POS Invoice":
                return filters if filters in h.invoices else None
            if doctype == "URY Table":
                return filters if filters in h.tables else None
            if doctype == "URY User":
                return True
            return None

        db.exists = exists

        def db_count(doctype, filters=None):
            return len([r for r in _table_rows(doctype) if _match(r, filters)])

        db.count = db_count

        def get_value(doctype, name=None, fieldname="name", as_dict=False, for_update=False, **kwargs):
            if for_update:
                h.locks.append((doctype, name))
            row = None
            if isinstance(name, dict):
                matches = [r for r in _table_rows(doctype) if _match(r, name)]
                row = matches[-1] if matches else None
            else:
                if doctype == "POS Invoice":
                    row = h.invoices.get(name)
                elif doctype == "POS Profile":
                    row = h.profiles.get(name)
                elif doctype == "URY Table":
                    row = h.tables.get(name)
                elif doctype == "Branch":
                    row = h.branches.get(name)
                elif doctype == "URY Restaurant":
                    row = h.restaurants.get(name)
                elif doctype == "Item":
                    row = h.items_master.get(name)
                elif doctype == "URY Menu":
                    row = FakeDoc(name="Main Menu", branch=h.branch)
                elif doctype == "URY Table Reservation":
                    row = h.reservations.get(name)
                elif doctype == "Customer":
                    row = h.customers.get(name)
                elif doctype == "User":
                    row = h.users.get(name)
                elif doctype == "URY Item Availability":
                    row = h.availability_rows.get(name)
            if row is None:
                return None
            if isinstance(fieldname, (list, tuple)):
                vals = FakeDoc({k: row.get(k) for k in fieldname})
                return vals if as_dict else tuple(vals[k] for k in fieldname)
            return row.get(fieldname)

        db.get_value = get_value
        f.get_value = get_value

        def set_value(doctype, name, field, val=None, **kwargs):
            target = None
            if doctype == "POS Invoice":
                target = h.invoices.get(name)
            elif doctype == "URY Table":
                target = h.tables.get(name)
            elif doctype == "URY Bill Reprint Log":
                target = next((r for r in h.reprint_logs if r.get("name") == name), None)
            if target is not None:
                if isinstance(field, dict):
                    target.update(field)
                else:
                    target[field] = val

        db.set_value = set_value

        def sql(query, params=(), as_dict=False, **kwargs):
            if "tabURY User" in query:
                branch, user = params
                return [{"room": r["room"]} for r in h.branch_users if r["parent"] == branch and r["user"] == user]
            if "tabPOS Opening Entry" in query:
                return [{"name": "OPN-1"}]
            return []

        db.sql = sql
        db.savepoint = lambda name: None
        db.rollback = lambda **kwargs: None
        f.db = db
        f.get_meta = lambda dt_name: SimpleNamespace(
            has_field=lambda fld: True,
            get_field=lambda fld: SimpleNamespace(length=140),
            default_print_format=None,
        )
        return f


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestFloorPrivilegedActions(unittest.TestCase):
    def setUp(self):
        self.h = FloorHarness()
        f = self.h.frappe

        utils_mod = ModuleType("frappe.utils")
        utils_mod.cint = lambda x: int(float(x or 0))
        utils_mod.flt = lambda x, p=None: round(float(x or 0), p) if p is not None else float(x or 0)
        utils_mod.cstr = lambda x: "" if x is None else str(x)
        utils_mod.now_datetime = lambda: dt.datetime(2026, 11, 24, 12, 30, 0)
        utils_mod.now = lambda: "2026-11-24 12:30:00"
        utils_mod.nowdate = lambda: "2026-11-24"
        utils_mod.today = lambda: "2026-11-24"
        utils_mod.getdate = lambda x=None: dt.date(2026, 11, 24)
        utils_mod.get_datetime = lambda x=None: dt.datetime(2026, 11, 24, 12, 30, 0)
        utils_mod.get_time = lambda x=None: dt.time(12, 30, 0)
        utils_mod.add_days = lambda d, days=0: d
        utils_mod.add_to_date = lambda d, **kw: d
        utils_mod.time_diff_in_minutes = lambda a, b: 0.0
        utils_mod.time_diff_in_seconds = lambda a, b: 0.0
        utils_mod.get_system_timezone = lambda: "Africa/Kampala"
        utils_mod.nestedset = MagicMock()
        utils_mod.validate_phone_number = MagicMock()
        f.utils = utils_mod

        model_doc = ModuleType("frappe.model.document")
        class _BaseDoc:
            pass
        model_doc.Document = _BaseDoc

        printview_mod = ModuleType("frappe.www.printview")
        printview_mod.validate_print_permission = MagicMock()

        erpnext_queries = ModuleType("erpnext.controllers.queries")
        erpnext_queries.item_query = MagicMock()

        # Stubs for non-Scope-f collaborators imported by ury_order / ury_print / ury_availability
        self.mock_submit_print = MagicMock(return_value={"status": "Success"})
        self.mock_copy_marker = object()

        stubs = {
            "frappe": f,
            "frappe.utils": utils_mod,
            "frappe.model": ModuleType("frappe.model"),
            "frappe.model.document": model_doc,
            "frappe.www": ModuleType("frappe.www"),
            "frappe.www.printview": printview_mod,
            "erpnext": ModuleType("erpnext"),
            "erpnext.controllers": ModuleType("erpnext.controllers"),
            "erpnext.controllers.queries": erpnext_queries,
            "ury.ury_pos.override": self.h.override,
            "ury.ury_pos.default_customer": SimpleNamespace(
                get_default_customer_config=lambda p: {},
                resolve_default_customer=lambda p, ot=None: "Walk-in Guest",
            ),
            "ury.ury.api.ury_kot_generate": SimpleNamespace(
                _execute_kot=MagicMock(),
                process_items_for_cancel_kot=MagicMock(),
            ),
            "ury.ury.api.ury_order_reservation_service": SimpleNamespace(
                _line_key=lambda *args, **kw: (args[1].get("reservation_line_key") if len(args) > 1 and hasattr(args[1], "get") else f"rk-{args[0]}"),
                _line_ref=lambda *args, **kw: (args[0].get("name") if args and hasattr(args[0], "get") else ""),
                reconcile_order_reservations=MagicMock(),
                release_order_reservations=MagicMock(),
                resolve_production_context=MagicMock(),
                _warehouse_for_context=MagicMock(),
            ),
            "ury.ury.api.ury_stock_policy": SimpleNamespace(
                get_branch_stock_policy=lambda **kw: SimpleNamespace(reservation_control_enabled=False),
            ),
            "ury.ury.doctype.alert_settings.alert_settings": SimpleNamespace(get_alert_rule=MagicMock(return_value=None)),
            "ury.ury.api.ury_kot_notification": SimpleNamespace(
                create_system_notification=MagicMock(),
                get_users_with_role=MagicMock(return_value=[]),
            ),
            "ury.ury.api.item_price": SimpleNamespace(
                item_price_release_states=MagicMock(return_value=(None, None)),
                price_valid_on=MagicMock(return_value=FakeDoc(price_list_rate=50000.0)),
            ),
            "ury.ury.api.ury_fulfilment_posting_service": SimpleNamespace(
                trigger_manufacture_on_bill_print=MagicMock(),
            ),
            "ury.ury.class_overrides": ModuleType("ury.ury.class_overrides"),
            "ury.ury.class_overrides.pos_invoice": SimpleNamespace(_COPY_RENDER_MARKER=self.mock_copy_marker),
            "ury.ury.printing": ModuleType("ury.ury.printing"),
            "ury.ury.printing.file_store": SimpleNamespace(save_job=MagicMock(return_value=True)),
            "ury.ury.printing.service": SimpleNamespace(submit_and_monitor_print_job=self.mock_submit_print),
            "ury.ury.api.ury_bom_compiler": MagicMock(),
            "ury.ury.api.ury_production_context": SimpleNamespace(resolve_production_context=MagicMock(return_value=None)),
            "ury.ury.api.ury_inventory_projection": MagicMock(),
            "ury.ury.api.ury_projection_service": MagicMock(),
            "ury.ury.api.ury_sales_plan_commit": MagicMock(),
            "ury.ury.api.ury_reservation_service": MagicMock(),
            "ury.ury.report.average_table_time.average_table_time": MagicMock(),
        }

        self.patcher = patch.dict(sys.modules, stubs)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        _load_module("ury.brand", "brand.py")

        self.auth = _load_module("ury.ury_pos.privileged_auth", "ury_pos/privileged_auth.py")
        self.pricing = _load_module("ury.ury_pos.pricing_actions", "ury_pos/pricing_actions.py")
        self.api = _load_module("ury.ury_pos.api", "ury_pos/api.py")
        self.api.getBranch = lambda *a, **kw: self.h.branch
        self.api.getRoom = lambda *a, **kw: [{"name": "Main Hall"}]
        self.api.getBranchRoom = lambda *a, **kw: [{"name": "Main Hall", "branch": self.h.branch}]
        self.api.posOpening = lambda *a, **kw: 0

        self.order = _load_module("ury.ury.doctype.ury_order.ury_order", "ury/doctype/ury_order/ury_order.py")
        self.order.getBranch = lambda *a, **kw: self.h.branch
        self.order.getRoom = lambda *a, **kw: [{"name": "Main Hall"}]
        self.order.change_table_in_kot = lambda inv, tbl, br: self.h.kot_transfer_calls.append((inv, tbl, br))
        self.order.require_offline_capture = MagicMock()
        self.order._validate_sync_items_against_menu = MagicMock()
        self.order._require_open_cashier_session = MagicMock()

        self.ury_print = _load_module("ury.ury.api.ury_print", "ury/api/ury_print.py")
        self.kot_reprint = _load_module("ury.ury.api.ury_kot_reprint", "ury/api/ury_kot_reprint.py")
        self.kot_reprint.getBranch = lambda *a, **kw: self.h.branch

        self.availability = _load_module("ury.ury.api.ury_availability", "ury/api/ury_availability.py")
        self.availability._branch_menu_items = lambda br: [{"item": "FISH", "name": "FISH"}]
        self.availability.compute_item_availability = lambda code, br, co: {
            "item": code,
            "status": self.h.availability_rows.get(f"AVAIL-{br}-{code}", {}).get("status", "Available"),
            "available_qty": self.h.availability_rows.get(f"AVAIL-{br}-{code}", {}).get("remaining_qty"),
        }
        self.availability.apply_count_delta = MagicMock()

        self.reservation_api = _load_module("ury.ury.api.table_reservation", "ury/api/table_reservation.py")
        self.reservation_api.check_table_reservation = lambda tbl: None
        self.reservation_api.validate_seated_reservation_transfer = MagicMock()
        self.reservation_api.transfer_seated_reservation = MagicMock()
        self.reservation_api.open_checks_on = lambda tables: []
        self.reservation_api._bound_check_or_split_is_open = lambda inv: False

        self.reservation_dt = _load_module(
            "ury.ury.doctype.ury_table_reservation.ury_table_reservation",
            "ury/doctype/ury_table_reservation/ury_table_reservation.py",
        )

        # Load floor_actions (does not exist yet -> genuine RED!)
        floor_path = ROOT / "ury_pos/floor_actions.py"
        if floor_path.exists():
            self.floor = _load_module("ury.ury_pos.floor_actions", "ury_pos/floor_actions.py")
        else:
            self.floor = None

    def _add_rule(self, privilege, role, may_perform=0, may_authorise=0, requires_reason=0):
        self.h.rules.append(
            FakeDoc(
                company=self.h.company,
                pos_profile="",
                privilege=privilege,
                role=role,
                may_perform=may_perform,
                may_authorise=may_authorise,
                requires_reason=requires_reason,
                privilege_group=0,
            )
        )

    # 1. Table transfer with POS Privilege Rule + Manager PIN in place
    def test_table_transfer_uses_privilege_rule_and_manager_pin_in_place(self):
        self.assertIsNotNone(self.floor, "ury.ury_pos.floor_actions module missing")
        inv = self.h.add_invoice("INV-1", restaurant_table="T1")
        self._add_rule("transfer_check", "URY Captain", may_perform=0, may_authorise=0)
        self._add_rule("transfer_check", "URY Manager", may_perform=1, may_authorise=1)

        self.h.frappe.session.user = "captain_a@example.com"
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.order.table_transfer("T1", "T2", "INV-1")
        self.assertEqual(inv.restaurant_table, "T1")

        token = self.h.override.issue_token(
            "manager@example.com",
            "transfer_check",
            "Main Till",
            "INV-1",
            {"action": "table_transfer", "from_table": "T1", "to_table": "T2", "invoice": "INV-1"},
        )
        self.order.table_transfer("T1", "T2", "INV-1", approval_token=token)
        self.assertEqual(inv.restaurant_table, "T2")
        self.assertEqual(self.h.tables["T1"].occupied, 0)
        self.assertEqual(self.h.tables["T2"].occupied, 1)
        self.assertEqual(self.h.frappe.session.user, "captain_a@example.com")
        self.assertEqual(len(self.h.logs), 1)
        self.assertEqual(self.h.logs[0].privilege, "transfer_check")

    # 2. Table transfer legacy fallback when no POS Privilege Rule rows exist
    def test_table_transfer_legacy_fallback_when_no_privilege_rules(self):
        inv = self.h.add_invoice("INV-1", restaurant_table="T1")
        # Manager in transfer_role_permissions succeeds solo and writes log
        self.h.frappe.session.user = "manager@example.com"
        self.order.table_transfer("T1", "T2", "INV-1")
        self.assertEqual(inv.restaurant_table, "T2")
        self.assertEqual(len(self.h.logs), 1)
        self.assertEqual(self.h.logs[0].outcome, "Approved")

        # Captain without transfer_role_permissions gets POS-AUTH-REQUIRED, then PIN works
        self.h.frappe.session.user = "captain_a@example.com"
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.order.table_transfer("T2", "T3", "INV-1")
        token = self.h.override.issue_token(
            "manager@example.com", "transfer_check", "Main Till", "INV-1",
            {"action": "table_transfer", "from_table": "T2", "to_table": "T3", "invoice": "INV-1"},
        )
        self.order.table_transfer("T2", "T3", "INV-1", approval_token=token)
        self.assertEqual(inv.restaurant_table, "T3")

    # 3. Adversarial: transfer onto an occupied table refused even with Manager PIN
    def test_adversarial_table_transfer_onto_occupied_table_refused_even_with_pin(self):
        inv = self.h.add_invoice("INV-1", restaurant_table="T1")
        self.h.tables["T2"].occupied = 1
        self.h.frappe.session.user = "captain_a@example.com"
        token = self.h.override.issue_token(
            "manager@example.com", "transfer_check", "Main Till", "INV-1",
            {"action": "table_transfer", "from_table": "T1", "to_table": "T2", "invoice": "INV-1"},
        )
        with self.assertRaisesRegex(ValidationError, "TRANSFER-TABLE-OCCUPIED"):
            self.order.table_transfer("T1", "T2", "INV-1", approval_token=token)
        self.assertEqual(inv.restaurant_table, "T1")
        self.assertEqual(len(self.h.override.consumed), 0)

        # Even if T2.occupied flag is stale (0), an open draft invoice on T2 blocks transfer
        self.h.tables["T2"].occupied = 0
        self.h.add_invoice("INV-OTHER", restaurant_table="T2", docstatus=0)
        with self.assertRaisesRegex(ValidationError, "TRANSFER-TABLE-OCCUPIED"):
            self.order.table_transfer("T1", "T2", "INV-1", approval_token=token)
        self.assertEqual(inv.restaurant_table, "T1")

    # 4. Adversarial: two waiters transferring the same check at once
    def test_adversarial_two_waiters_transferring_same_check_at_once(self):
        inv = self.h.add_invoice("INV-1", restaurant_table="T1", waiter="captain_a@example.com")
        self.h.frappe.session.user = "manager@example.com"

        # First transfer moves INV-1 from T1 -> T2
        self.order.table_transfer("T1", "T2", "INV-1")
        self.assertEqual(inv.restaurant_table, "T2")
        self.assertIn(("POS Invoice", "INV-1"), self.h.locks)

        # Competing second waiter still thinks INV-1 is on T1 and tries T1 -> T3
        with self.assertRaisesRegex(ValidationError, "TRANSFER-CONFLICT"):
            self.order.table_transfer("T1", "T3", "INV-1")
        self.assertEqual(inv.restaurant_table, "T2")
        self.assertEqual(self.h.tables["T3"].occupied, 0)

        # Competing captain transfer on stale currentCaptain
        self.order.captain_transfer("captain_a@example.com", "captain_b@example.com", "INV-1")
        self.assertEqual(inv.waiter, "captain_b@example.com")
        with self.assertRaisesRegex(ValidationError, "TRANSFER-CONFLICT"):
            self.order.captain_transfer("captain_a@example.com", "cashier@example.com", "INV-1")
        self.assertEqual(inv.waiter, "captain_b@example.com")

    # 5. Captain transfer governed by POS Privilege Rule + Manager PIN in place
    def test_captain_transfer_uses_privilege_rule_and_pin_in_place(self):
        inv = self.h.add_invoice("INV-1", waiter="captain_a@example.com")
        self._add_rule("transfer_check", "URY Captain", may_perform=0, may_authorise=0)
        self._add_rule("transfer_check", "URY Manager", may_perform=1, may_authorise=1)

        self.h.frappe.session.user = "captain_a@example.com"
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.order.captain_transfer("captain_a@example.com", "captain_b@example.com", "INV-1")
        self.assertEqual(inv.waiter, "captain_a@example.com")

        token = self.h.override.issue_token(
            "manager@example.com", "transfer_check", "Main Till", "INV-1",
            {"action": "captain_transfer", "from_waiter": "captain_a@example.com", "to_waiter": "captain_b@example.com", "invoice": "INV-1"},
        )
        self.order.captain_transfer("captain_a@example.com", "captain_b@example.com", "INV-1", approval_token=token)
        self.assertEqual(inv.waiter, "captain_b@example.com")
        self.assertEqual(self.h.frappe.session.user, "captain_a@example.com")
        self.assertEqual(len(self.h.logs), 1)

    # 6. Cross-outlet transfer (Simphony Opt 46)
    def test_transfer_check_outlet_moves_check_across_profiles_and_guards_occupied_table(self):
        self.assertIsNotNone(self.floor, "ury.ury_pos.floor_actions module missing")
        inv = self.h.add_invoice("INV-1", restaurant_table="T1", pos_profile="Main Till", branch=self.h.branch)
        self.h.frappe.session.user = "captain_a@example.com"

        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.floor.transfer_check_outlet("INV-1", "Rooftop Till", target_table="RT-1")

        # Occupied destination table refused even with Manager PIN
        tok_occ = self.h.override.issue_token(
            "manager@example.com", "transfer_check", "Main Till", "INV-1",
            {"action": "transfer_check_outlet", "invoice": "INV-1", "from_profile": "Main Till", "to_profile": "Rooftop Till", "to_table": "RT-2"},
        )
        with self.assertRaisesRegex(ValidationError, "TRANSFER-TABLE-OCCUPIED"):
            self.floor.transfer_check_outlet("INV-1", "Rooftop Till", target_table="RT-2", approval_token=tok_occ)

        # Free destination table with Manager PIN succeeds
        tok_ok = self.h.override.issue_token(
            "manager@example.com", "transfer_check", "Main Till", "INV-1",
            {"action": "transfer_check_outlet", "invoice": "INV-1", "from_profile": "Main Till", "to_profile": "Rooftop Till", "to_table": "RT-1"},
        )
        res = self.floor.transfer_check_outlet("INV-1", "Rooftop Till", target_table="RT-1", approval_token=tok_ok)
        self.assertEqual(res["pos_profile"], "Rooftop Till")
        self.assertEqual(inv.pos_profile, "Rooftop Till")
        self.assertEqual(inv.branch, self.h.branch_2)
        self.assertEqual(inv.restaurant_table, "RT-1")
        self.assertEqual(self.h.tables["T1"].occupied, 0)
        self.assertEqual(self.h.tables["RT-1"].occupied, 1)
        self.assertEqual(len(self.h.logs), 1)

    # 7. Pick up another server's check keeps waiter sticky + sync_order escalation
    def test_pickup_other_check_requires_privilege_or_pin_and_keeps_waiter_sticky(self):
        self.assertIsNotNone(self.floor, "ury.ury_pos.floor_actions module missing")
        inv = self.h.add_invoice("INV-1", waiter="captain_a@example.com")
        self._add_rule("pickup_other_check", "URY Captain", may_perform=0, may_authorise=0)
        self._add_rule("pickup_other_check", "URY Manager", may_perform=1, may_authorise=1)

        self.h.frappe.session.user = "captain_b@example.com"
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.floor.pickup_other_check("INV-1")

        tok = self.h.override.issue_token(
            "manager@example.com", "pickup_other_check", "Main Till", "INV-1",
            {"action": "pickup_other_check", "invoice": "INV-1", "owner_waiter": "captain_a@example.com"},
        )
        res = self.floor.pickup_other_check("INV-1", approval_token=tok)
        self.assertEqual(res["owner_waiter"], "captain_a@example.com")
        self.assertEqual(inv.waiter, "captain_a@example.com")
        self.assertEqual(self.h.frappe.session.user, "captain_b@example.com")
        self.assertEqual(len(self.h.logs), 1)

    # 8. Merge bills enforces merge_checks privilege and never swallows POS-AUTH errors
    def test_merge_bills_enforces_merge_checks_privilege_and_does_not_swallow_auth_errors(self):
        self.h.add_invoice("INV-1", waiter="captain_a@example.com")
        self.h.add_invoice("INV-2", waiter="captain_b@example.com", restaurant_table="T2")
        self._add_rule("merge_checks", "URY Captain", may_perform=0, may_authorise=0)
        self._add_rule("merge_checks", "URY Manager", may_perform=1, may_authorise=1)

        self.order.consolidate_checks = MagicMock()
        self.h.frappe.session.user = "captain_a@example.com"
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.api.merge_bills("INV-1", "INV-2")
        self.order.consolidate_checks.assert_not_called()

        tok = self.h.override.issue_token(
            "manager@example.com", "merge_checks", "Main Till", "INV-1",
            {"action": "merge_bills", "primary_invoice": "INV-1", "secondary_invoices": ["INV-2"]},
        )
        res = self.api.merge_bills("INV-1", "INV-2", approval_token=tok)
        self.assertEqual(res["status"], "success")
        self.order.consolidate_checks.assert_called_once()
        self.assertEqual(len(self.h.logs), 1)

    # 9. Merge tables batch enforces merge_checks privilege and Manager PIN
    def test_merge_tables_batch_enforces_merge_checks_privilege_and_pin(self):
        self.h.tables["T1"].occupied = 0
        self.h.tables["T2"].occupied = 0
        self._add_rule("merge_checks", "URY Captain", may_perform=0, may_authorise=0)
        self._add_rule("merge_checks", "URY Manager", may_perform=1, may_authorise=1)

        self.order._sync_active_order_with_merge_cluster = MagicMock()
        self.order._reconcile_open_invoices_for_tables = MagicMock()

        self.h.frappe.session.user = "captain_a@example.com"
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.order.merge_tables_batch("T1", ["T2"])

        tok = self.h.override.issue_token(
            "manager@example.com", "merge_checks", "Main Till", "T1",
            {"action": "merge_tables", "anchor_table": "T1", "tables": ["T2"]},
        )
        self.assertTrue(self.order.merge_tables_batch("T1", ["T2"], approval_token=tok))
        self.assertEqual(self.h.tables["T1"].merged_with, "T2")
        self.assertEqual(len(self.h.logs), 1)

    # 10. Split bill enforces split_check privilege + Simphony Opt 102 max_splits_before_pin
    def test_split_bill_enforces_split_check_privilege_and_max_splits_opt_102(self):
        inv = self.h.add_invoice("INV-1", waiter="captain_a@example.com")
        inv.items = [
            FakeDoc(name="L1", item_code="STEAK", item_name="Steak", qty=1, rate=50000, reservation_line_key="rk-1"),
            FakeDoc(name="L2", item_code="WINE", item_name="Wine", qty=1, rate=30000, reservation_line_key="rk-2"),
            FakeDoc(name="L3", item_code="SALAD", item_name="Salad", qty=1, rate=20000, reservation_line_key="rk-3"),
        ]
        self.h.profiles["Main Till"].custom_max_splits_before_pin = 1
        self._add_rule("split_check", "URY Captain", may_perform=1, may_authorise=0)
        self._add_rule("split_check", "URY Manager", may_perform=1, may_authorise=1)

        self.h.frappe.session.user = "captain_a@example.com"
        # First split allowed solo
        res1 = self.order.split_bill("INV-1", [{"name": "L2", "qty": 1}])
        self.assertIn("new_invoice", res1)
        self.assertEqual(len(self.h.logs), 1)

        # Second split on the same group exceeds custom_max_splits_before_pin=1 -> needs PIN
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.order.split_bill("INV-1", [{"name": "L3", "qty": 1}])

        tok = self.h.override.issue_token(
            "manager@example.com", "split_check", "Main Till", "INV-1",
            {"action": "split_bill", "source_invoice": "INV-1", "move_items": [["L3", 1.0]]},
        )
        res2 = self.order.split_bill("INV-1", [{"name": "L3", "qty": 1}], approval_token=tok)
        self.assertIn("new_invoice", res2)
        self.assertEqual(len(self.h.logs), 2)

    # 11. Adversarial: split then reprint preserves parent print count and resets child count
    def test_adversarial_split_then_reprint_preserves_parent_count_and_resets_child(self):
        inv = self.h.add_invoice("INV-1", waiter="captain_a@example.com", invoice_printed=0, custom_bill_print_count=0)
        self.h.profiles["Main Till"].custom_max_free_bill_prints = 1
        self._add_rule("split_check", "URY Captain", may_perform=1, may_authorise=0)
        self._add_rule("reprint_bill", "URY Captain", may_perform=0, may_authorise=0, requires_reason=1)
        self._add_rule("reprint_bill", "URY Manager", may_perform=1, may_authorise=1, requires_reason=1)

        self.h.frappe.session.user = "captain_a@example.com"
        # 1st print of INV-1 is free
        self.ury_print.select_network_printer("Main Till", "INV-1")
        self.assertEqual(inv.custom_bill_print_count, 1)

        # Split INV-1 -> INV-1 (parent) + child
        res = self.order.split_bill("INV-1", [{"name": "INV-1-L2", "qty": 1}])
        child_name = res["new_invoice"]
        child = self.h.invoices[child_name]
        self.assertEqual(inv.custom_bill_print_count, 1)
        self.assertEqual(child.custom_bill_print_count, 0)

        # Parent INV-1 reprint must require Manager PIN + reason
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.ury_print.select_network_printer("Main Till", "INV-1", reason="GUEST_REQUEST")

        # Child's 1st print is free
        self.ury_print.select_network_printer("Main Till", child_name)
        self.assertEqual(child.custom_bill_print_count, 1)

        # Child's 2nd print requires Manager PIN + reason
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.ury_print.select_network_printer("Main Till", child_name, reason="GUEST_REQUEST")

    # 12. Guest count & order type change privilege gates in sync_order
    def test_guest_count_and_order_type_change_require_privilege_or_pin(self):
        inv = self.h.add_invoice("INV-1", waiter="captain_a@example.com", no_of_pax=4, order_type="Dine In")
        self._add_rule("guest_count_change", "URY Captain", may_perform=0, may_authorise=0)
        self._add_rule("guest_count_change", "URY Manager", may_perform=1, may_authorise=1)
        self._add_rule("order_type_change", "URY Captain", may_perform=0, may_authorise=0)
        self._add_rule("order_type_change", "URY Manager", may_perform=1, may_authorise=1)

        self.h.frappe.session.user = "captain_a@example.com"
        base_items = [
            {"name": "INV-1-L1", "item": "STEAK", "item_name": "Ribeye Steak", "qty": 2, "reservation_line_key": "rk-1"},
            {"name": "INV-1-L2", "item": "WINE", "item_name": "House Red", "qty": 1, "reservation_line_key": "rk-2"},
        ]

        # Changing pax from 4 -> 2 without PIN fails
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.order.sync_order(
                items=base_items,
                cashier="cashier@example.com",
                owner="captain_a@example.com",
                mode_of_payment="Cash",
                customer="Walk-in Guest",
                no_of_pax=2,
                last_invoice="INV-1",
                waiter="captain_a@example.com",
                pos_profile="Main Till",
                last_modified_time=inv.modified,
                table="T1",
                invoice="INV-1",
                order_type="Dine In",
            )
        self.assertEqual(inv.no_of_pax, 4)

        # With Manager PIN token for guest_count_change -> succeeds
        tok_pax = self.h.override.issue_token(
            "manager@example.com", "guest_count_change", "Main Till", "INV-1",
            {"action": "guest_count_change", "invoice": "INV-1", "from_pax": 4, "to_pax": 2},
        )
        self.order.sync_order(
            items=base_items,
            cashier="cashier@example.com",
            owner="captain_a@example.com",
            mode_of_payment="Cash",
            customer="Walk-in Guest",
            no_of_pax=2,
            last_invoice="INV-1",
            waiter="captain_a@example.com",
            pos_profile="Main Till",
            last_modified_time=inv.modified,
            table="T1",
            invoice="INV-1",
            order_type="Dine In",
            guest_count_approval_token=tok_pax,
        )
        self.assertEqual(inv.no_of_pax, 2)

        # Changing order_type Dine In -> Take Away without PIN fails
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.order.sync_order(
                items=base_items,
                cashier="cashier@example.com",
                owner="captain_a@example.com",
                mode_of_payment="Cash",
                customer="Walk-in Guest",
                no_of_pax=2,
                last_invoice="INV-1",
                waiter="captain_a@example.com",
                pos_profile="Main Till",
                last_modified_time=inv.modified,
                table="T1",
                invoice="INV-1",
                order_type="Take Away",
            )
        self.assertEqual(inv.order_type, "Dine In")

        # With Manager PIN token for order_type_change -> succeeds
        tok_ot = self.h.override.issue_token(
            "manager@example.com", "order_type_change", "Main Till", "INV-1",
            {"action": "order_type_change", "invoice": "INV-1", "from_order_type": "Dine In", "to_order_type": "Take Away"},
        )
        self.order.sync_order(
            items=base_items,
            cashier="cashier@example.com",
            owner="captain_a@example.com",
            mode_of_payment="Cash",
            customer="Walk-in Guest",
            no_of_pax=2,
            last_invoice="INV-1",
            waiter="captain_a@example.com",
            pos_profile="Main Till",
            last_modified_time=inv.modified,
            table="T1",
            invoice="INV-1",
            order_type="Take Away",
            order_type_approval_token=tok_ot,
        )
        self.assertEqual(inv.order_type, "Take Away")

    # 13. Open bill print free count, then PIN + reason + COPY mark
    def test_reprint_open_bill_free_then_pin_and_reason_with_copy_mark(self):
        inv = self.h.add_invoice("INV-1", waiter="captain_a@example.com", custom_bill_print_count=0)
        self.h.profiles["Main Till"].custom_max_free_bill_prints = 1
        self._add_rule("reprint_bill", "URY Cashier", may_perform=0, may_authorise=0, requires_reason=1)
        self._add_rule("reprint_bill", "URY Manager", may_perform=1, may_authorise=1, requires_reason=1)

        self.h.frappe.session.user = "cashier@example.com"
        # Print #1 is free
        self.ury_print.select_network_printer("Main Till", "INV-1")
        self.assertEqual(inv.custom_bill_print_count, 1)
        self.assertFalse(self.h.printer_calls[-1]["extra_metadata"].get("is_reprint"))

        # Print #2 without reason -> POS-REASON-REQUIRED
        with self.assertRaisesRegex(ValidationError, "POS-REASON-REQUIRED"):
            self.ury_print.select_network_printer("Main Till", "INV-1")

        # Print #2 with reason but no PIN -> POS-AUTH-REQUIRED
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.ury_print.select_network_printer("Main Till", "INV-1", reason="GUEST_REQUEST")

        # Print #2 with reason + Manager PIN -> succeeds with COPY watermark metadata
        tok = self.h.override.issue_token(
            "manager@example.com", "reprint_bill", "Main Till", "INV-1",
            {"action": "reprint_open_bill", "invoice": "INV-1", "print_number": 2},
            reason="GUEST_REQUEST",
        )
        self.ury_print.select_network_printer("Main Till", "INV-1", approval_token=tok, reason="GUEST_REQUEST")
        self.assertEqual(inv.custom_bill_print_count, 2)
        meta = self.h.printer_calls[-1]["extra_metadata"]
        self.assertTrue(meta.get("is_reprint"))
        self.assertEqual(meta.get("copy_number"), 2)
        self.assertIn("COPY", meta.get("reprint_label", "").upper())
        self.assertEqual(len(self.h.logs), 1)

    # 14. Adversarial: reprint count across devices enforced via DB FOR UPDATE
    def test_adversarial_reprint_count_across_devices_enforced_by_db_lock(self):
        inv = self.h.add_invoice("INV-1", custom_bill_print_count=0, invoice_printed=0)
        self.h.profiles["Main Till"].custom_max_free_bill_prints = 1
        self._add_rule("reprint_bill", "URY Cashier", may_perform=0, may_authorise=0, requires_reason=1)
        self._add_rule("reprint_bill", "URY Manager", may_perform=1, may_authorise=1, requires_reason=1)

        # Device 1 (cashier via network print) uses the 1 free print
        self.h.frappe.session.user = "cashier@example.com"
        self.ury_print.select_network_printer("Main Till", "INV-1")
        self.assertEqual(inv.custom_bill_print_count, 1)
        self.assertIn(("POS Invoice", "INV-1"), self.h.locks)

        # Device 2 (captain via QZ print) attempts print without PIN -> blocked by authoritative DB count
        self.h.frappe.session.user = "captain_a@example.com"
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.ury_print.qz_print_update("INV-1", reason="PRINTER_JAM")
        self.assertEqual(inv.custom_bill_print_count, 1)

        # Device 2 retries with Manager PIN + reason -> succeeds
        tok = self.h.override.issue_token(
            "manager@example.com", "reprint_bill", "Main Till", "INV-1",
            {"action": "reprint_open_bill", "invoice": "INV-1", "print_number": 2},
            reason="PRINTER_JAM",
        )
        res = self.ury_print.qz_print_update("INV-1", approval_token=tok, reason="PRINTER_JAM")
        self.assertEqual(res["status"], "Success")
        self.assertEqual(inv.custom_bill_print_count, 2)

    # 15. Closed receipt copy enforces custom_max_free_closed_copies + PIN + reason + COPY marker
    def test_closed_bill_copy_enforces_free_limit_pin_reason_and_keeps_copy_marker(self):
        inv = self.h.add_invoice("INV-CLOSED", docstatus=1, status="Paid")
        self.h.profiles["Main Till"].custom_max_free_closed_copies = 0
        self._add_rule("reprint_bill", "URY Cashier", may_perform=0, may_authorise=0, requires_reason=1)
        self._add_rule("reprint_bill", "URY Manager", may_perform=1, may_authorise=1, requires_reason=1)

        self.h.frappe.session.user = "cashier@example.com"
        with self.assertRaisesRegex(ValidationError, "POS-REASON-REQUIRED"):
            self.api.print_bill_copy("INV-CLOSED", idempotency_key="copy-1")

        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.api.print_bill_copy("INV-CLOSED", idempotency_key="copy-1", reason="GUEST_REQUEST")

        tok = self.h.override.issue_token(
            "manager@example.com", "reprint_bill", "Main Till", "INV-CLOSED",
            {"action": "print_bill_copy", "invoice": "INV-CLOSED", "copy_number": 1},
            reason="GUEST_REQUEST",
        )
        res = self.api.print_bill_copy("INV-CLOSED", idempotency_key="copy-1", approval_token=tok, reason="GUEST_REQUEST")
        self.assertEqual(res["status"], "SUBMITTED")
        self.assertEqual(len(self.h.reprint_logs), 1)
        # COPY marker preserved on the rendered doc
        printed_doc = self.mock_submit_print.call_args.kwargs["doc"]
        self.assertIs(printed_doc._ury_copy_render, self.mock_copy_marker)
        self.assertEqual(printed_doc.copy_number, 1)
        self.assertEqual(len(self.h.logs), 1)

    # 16. Kitchen re-fire enforces reprint_kot privilege + PIN + reason + RE-FIRE mark
    def test_reprint_kot_enforces_privilege_pin_reason_and_refire_mark(self):
        inv = self.h.add_invoice("INV-1", waiter="captain_a@example.com", custom_kot_reprint_count=0)
        self._add_rule("reprint_kot", "URY Captain", may_perform=0, may_authorise=0, requires_reason=1)
        self._add_rule("reprint_kot", "URY Manager", may_perform=1, may_authorise=1, requires_reason=1)

        self.h.frappe.session.user = "captain_a@example.com"
        with self.assertRaisesRegex(ValidationError, "POS-REASON-REQUIRED"):
            self.kot_reprint.reprint_kot("INV-1")

        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.kot_reprint.reprint_kot("INV-1", reason="KITCHEN_REFIRE")

        tok = self.h.override.issue_token(
            "manager@example.com", "reprint_kot", "Main Till", "INV-1",
            {"action": "reprint_kot", "invoice": "INV-1", "reprint_number": 1},
            reason="KITCHEN_REFIRE",
        )
        res = self.kot_reprint.reprint_kot("INV-1", approval_token=tok, reason="KITCHEN_REFIRE")
        self.assertEqual(res, "Success")
        self.assertEqual(inv.custom_kot_reprint_count, 1)
        self.assertEqual(len(self.h.printer_calls), 1)
        self.assertIn("RE-FIRE", self.h.printer_calls[-1]["extra_metadata"].get("reprint_label", "").upper())
        self.assertEqual(len(self.h.logs), 1)

    # 17. 86 availability: Sold Out stays easy; restore / count / reset_policy needs item_86_restore / PIN
    def test_item_86_mark_sold_out_stays_easy_but_restore_count_reset_need_pin(self):
        self._add_rule("item_86_restore", "URY Captain", may_perform=0, may_authorise=0)
        self._add_rule("item_86_restore", "URY Manager", may_perform=1, may_authorise=1)
        self._add_rule("item_86_restore", "Kitchen Lead", may_perform=1, may_authorise=0)

        # Captain marks Sold Out easily with NO PIN
        self.h.frappe.session.user = "captain_a@example.com"
        self.availability.set_item_availability(self.h.branch, "FISH", status="Sold Out")
        row = self.h.availability_rows[f"AVAIL-{self.h.branch}-FISH"]
        self.assertEqual(row.status, "Sold Out")

        # Restoring Available, setting count, or changing reset_policy blocked without PIN
        for kwargs in ({"status": "Available"}, {"count": 5}, {"reset_policy": "Start of business day"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
                    self.availability.set_item_availability(self.h.branch, "FISH", **kwargs)

        # With Manager PIN -> Captain restores Available + sets count in place without switching session
        tok = self.h.override.issue_token(
            "manager@example.com", "item_86_restore", "Main Till", f"{self.h.branch}:FISH",
            {"action": "item_86_restore", "branch": self.h.branch, "item_code": "FISH", "status": "Available", "count": 5, "reset_policy": ""},
        )
        self.availability.set_item_availability(self.h.branch, "FISH", status="Available", count=5, approval_token=tok)
        self.assertEqual(row.status, "Available")
        self.assertEqual(row.remaining_qty, 5)
        self.assertEqual(self.h.frappe.session.user, "captain_a@example.com")
        self.assertEqual(len(self.h.logs), 1)

        # Kitchen Lead (configured via POS Privilege Rule, not hard-coded MANAGE_ROLES) succeeds solo
        self.h.frappe.session.user = "kitchen_lead@example.com"
        self.availability.set_item_availability(self.h.branch, "FISH", count=10)
        self.assertEqual(row.remaining_qty, 10)
        self.assertEqual(len(self.h.logs), 2)

    # 18. Reservation cancel / No Show require reason + reservation_cancel / PIN; Seated stays open
    def test_reservation_cancel_and_no_show_require_reason_and_privilege_or_pin(self):
        res1 = self.h.add_reservation("RES-1", status="Confirmed")
        res2 = self.h.add_reservation("RES-2", status="Confirmed")
        res3 = self.h.add_reservation("RES-3", status="Confirmed")
        self._add_rule("reservation_cancel", "URY Captain", may_perform=0, may_authorise=0, requires_reason=1)
        self._add_rule("reservation_cancel", "URY Manager", may_perform=1, may_authorise=1, requires_reason=1)

        # Wire real validate_status_transitions on save so we prove DocType validation accepts verified PIN!
        def _attach_validate(doc):
            object.__setattr__(doc, "validate_table_is_free_to_seat", lambda: None)
            object.__setattr__(doc, "validate_unseat", lambda: None)
            def _save(**kw):
                self.reservation_dt.URYTableReservation.validate_status_transitions(doc)
                return doc
            object.__setattr__(doc, "save", _save)

        for r in (res1, res2, res3):
            _attach_validate(r)

        self.h.frappe.session.user = "captain_a@example.com"

        # Seated stays unrestricted (no reason, no PIN)
        self.reservation_api.update_reservation_status("RES-3", "Seated")
        self.assertEqual(res3.status, "Seated")

        # Cancelled without reason -> POS-REASON-REQUIRED
        with self.assertRaisesRegex(ValidationError, "POS-REASON-REQUIRED"):
            self.reservation_api.update_reservation_status("RES-1", "Cancelled")

        # Cancelled with reason but no PIN -> POS-AUTH-REQUIRED
        with self.assertRaisesRegex(PermissionError_, "POS-AUTH-REQUIRED"):
            self.reservation_api.update_reservation_status("RES-1", "Cancelled", cancel_reason="GUEST_CANCELLED")
        self.assertEqual(res1.status, "Confirmed")

        # Cancelled with active reason + Manager PIN -> succeeds through DocType validate!
        tok_c = self.h.override.issue_token(
            "manager@example.com", "reservation_cancel", "Main Till", "RES-1",
            {"action": "reservation_cancel", "reservation": "RES-1", "target_status": "Cancelled"},
            reason="GUEST_CANCELLED",
        )
        self.reservation_api.update_reservation_status(
            "RES-1", "Cancelled", cancel_reason="GUEST_CANCELLED", approval_token=tok_c
        )
        self.assertEqual(res1.status, "Cancelled")
        self.assertEqual(res1.cancel_reason, "GUEST_CANCELLED")
        self.assertEqual(res1.custom_cancel_approver, "manager@example.com")

        # No Show with active reason + Manager PIN -> succeeds through DocType validate!
        tok_ns = self.h.override.issue_token(
            "manager@example.com", "reservation_cancel", "Main Till", "RES-2",
            {"action": "reservation_cancel", "reservation": "RES-2", "target_status": "No Show"},
            reason="NO_SHOW",
        )
        self.reservation_api.update_reservation_status(
            "RES-2", "No Show", cancel_reason="NO_SHOW", approval_token=tok_ns
        )
        self.assertEqual(res2.status, "No Show")
        self.assertEqual(res2.custom_cancel_approver, "manager@example.com")
        self.assertEqual(len(self.h.logs), 2)

    # 19. Adversarial: offline replay refused for every Scope f privileged action
    def test_adversarial_offline_replay_refused_for_every_scope_f_action(self):
        self.assertIsNotNone(self.floor, "ury.ury_pos.floor_actions module missing")
        self.h.add_invoice("INV-1", waiter="captain_a@example.com", custom_bill_print_count=1)
        self.h.add_invoice("INV-2", waiter="captain_b@example.com", restaurant_table="T2")
        self.h.add_invoice("INV-CLOSED", docstatus=1, status="Paid")
        self.h.add_reservation("RES-1", status="Confirmed")
        self.h.frappe.session.user = "manager@example.com"
        self.h.frappe.flags.ury_offline_replay = True

        calls = [
            ("table_transfer", lambda: self.order.table_transfer("T1", "T3", "INV-1")),
            ("captain_transfer", lambda: self.order.captain_transfer("captain_a@example.com", "captain_b@example.com", "INV-1")),
            ("transfer_check_outlet", lambda: self.floor.transfer_check_outlet("INV-1", "Rooftop Till", target_table="RT-1")),
            ("pickup_other_check", lambda: self.floor.pickup_other_check("INV-2")),
            ("merge_bills", lambda: self.api.merge_bills("INV-1", "INV-2")),
            ("merge_tables_batch", lambda: self.order.merge_tables_batch("T1", ["T3"])),
            ("split_bill", lambda: self.order.split_bill("INV-1", [{"name": "INV-1-L2", "qty": 1}])),
            ("reprint_open_bill", lambda: self.ury_print.select_network_printer("Main Till", "INV-1", reason="GUEST_REQUEST")),
            ("print_bill_copy", lambda: self.api.print_bill_copy("INV-CLOSED", idempotency_key="off-1", reason="GUEST_REQUEST")),
            ("reprint_kot", lambda: self.kot_reprint.reprint_kot("INV-1", reason="KITCHEN_REFIRE")),
            ("item_86_restore", lambda: self.availability.set_item_availability(self.h.branch, "FISH", status="Available")),
            ("reservation_cancel", lambda: self.reservation_api.update_reservation_status("RES-1", "Cancelled", cancel_reason="GUEST_CANCELLED")),
        ]
        for label, fn in calls:
            with self.subTest(action=label):
                with self.assertRaisesRegex(PermissionError_, "POS-OFFLINE-PRIVILEGED-REFUSED"):
                    fn()


if __name__ == "__main__":
    unittest.main()
