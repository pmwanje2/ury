"""Till cash controls Slice 1: Paid Out/Paid In, Pickup/Loan/Threshold Exception, No Sale,
currency/UGX & drawer isolation; site-less (in-memory Frappe double, no bench required).

Matches Oracle Simphony Cash Management (Opt 33 No Sale, Opt 207 Paid-In/Paid-Out,
Opt 209/250 Pickup/Safe Drop, Opt 258 Threshold Level Exception, Opt 201-204 Cash Drawer
Assignment) and SAP Customer Checkout / POS Store Manager parity.
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

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "ury"
HOOKS_PY = APP / "hooks.py"
CUSTOM_FIELDS_JSON = APP / "fixtures" / "custom_field.json"
DRAWER_EVENT_DIR = APP / "ury" / "doctype" / "pos_drawer_event"
CASH_MOVEMENT_HOOK = APP / "ury" / "hooks" / "ury_cash_movement.py"
TILL_CONTROLS_PY = APP / "ury_pos" / "till_controls.py"


class Row(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


class Refused(Exception):
    pass


class PermissionDenied(Exception):
    pass


class FakeDoc(Row):
    def __init__(self, data=None, store=None):
        super().__init__(data or {})
        self.flags = SimpleNamespace()
        self._store = store
        self._is_new = not bool(self.get("name"))

    def is_new(self):
        return self._is_new

    def get_doc_before_save(self):
        return None

    def append(self, field, value):
        rows = self.setdefault(field, [])
        row = Row(value)
        row.idx = len(rows) + 1
        rows.append(row)
        return row

    def insert(self, ignore_permissions=False):
        if not self.get("name"):
            dt = self.get("doctype", "Doc")
            prefix = dt.replace(" ", "-").upper()
            seq = self._store.next_seq(prefix) if self._store else 1
            self["name"] = f"{prefix}-{seq:04d}"
        self.setdefault("docstatus", 0)
        self.setdefault("owner", self._store.current_user if self._store else "Administrator")
        self.setdefault("creation", datetime(2026, 11, 24, 9, 0, 0))
        if self.get("doctype") == "Journal Entry":
            self._compute_je_totals()
            self._store.run_je_hook("validate", self)
        elif self.get("doctype") == "POS Drawer Event":
            self._store.run_drawer_event_validate(self)
        self._is_new = False
        if self._store is not None:
            self._store.save_doc(self)
        return self

    def save(self, ignore_permissions=False):
        if self.get("doctype") == "Journal Entry":
            self._compute_je_totals()
            self._store.run_je_hook("validate", self)
        elif self.get("doctype") == "POS Drawer Event":
            self._store.run_drawer_event_validate(self)
        if self._store is not None:
            self._store.save_doc(self)
        return self

    def submit(self):
        if self.get("doctype") == "Journal Entry":
            self._compute_je_totals()
            self._store.run_je_hook("validate", self)
            self._store.run_je_hook("before_submit", self)
        self["docstatus"] = 1
        if self._store is not None:
            self._store.save_doc(self)
        return self

    def cancel(self):
        if self.get("doctype") == "Journal Entry":
            self._store.run_je_hook("before_cancel", self)
        self["docstatus"] = 2
        if self._store is not None:
            self._store.save_doc(self)
        return self

    def _compute_je_totals(self):
        accounts = self.get("accounts") or []
        total_debit = 0.0
        total_credit = 0.0
        for row in accounts:
            ex = float(row.get("exchange_rate") or 1.0)
            if row.get("debit") is None:
                row["debit"] = float(row.get("debit_in_account_currency") or 0) * ex
            if row.get("credit") is None:
                row["credit"] = float(row.get("credit_in_account_currency") or 0) * ex
            total_debit += float(row.get("debit") or 0)
            total_credit += float(row.get("credit") or 0)
        self["total_debit"] = total_debit
        self["total_credit"] = total_credit


class FakeOverride:
    """Contract-faithful double of B2a's `ury.ury_pos.override`."""

    PRIVILEGES = {
        "paid_out": ("Paid Out / Paid In", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        "cash_movement": ("Cash In / Paid Out", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        "cash_pickup": ("Cash Pickup / Safe Drop", True, ("URY Cashier", "URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        "cash_loan": ("Cash Loan / Float", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        "no_sale": ("No Sale (Open Cash Drawer)", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        "over_short_close": ("Over/Short at Close", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        "over_halo_tender": ("Over HALO Tender", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        "close_other_till": ("Close Another Till", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
        "reopen_till": ("Reopen Till", True, ("URY Manager", "URY Admin"), ("URY Manager", "URY Admin")),
    }

    def __init__(self, store):
        self.store = store
        self.tokens: dict[str, dict] = {}
        self.consumed: set[str] = set()
        self.logs: list[dict] = []

    def _canonical(self, claim):
        return json.dumps(claim or {}, sort_keys=True, separators=(",", ":"), default=str)

    def mint(self, token, *, requester, approver, privilege, pos_profile, reference_name, claim, expired=False):
        self.tokens[token] = {
            "requester": requester,
            "approver": approver,
            "privilege": privilege,
            "pos_profile": pos_profile,
            "reference_name": reference_name,
            "claim_json": self._canonical(claim),
            "claim": dict(claim or {}),
            "expired": expired,
        }

    def is_policy_active(self, company):
        return bool(self.store.privilege_rules.get(company))

    def may_perform(self, user, pos_profile, privilege, percent=None, amount=None):
        profile = self.store.profiles.get(pos_profile)
        if not profile:
            return False
        roles = set(self.store.user_roles.get(user, ()))
        rules = self.store.privilege_rules.get(profile.company, [])
        matching = [r for r in rules if r["privilege"] == privilege and r["role"] in roles]
        if not matching:
            return False
        for rule in matching:
            if not rule.get("may_perform"):
                continue
            max_amt = float(rule.get("max_amount") or 0)
            if amount is not None and max_amt > 0 and float(amount) > max_amt + 1e-9:
                continue
            return True
        return False

    def consume(self, token, privilege, pos_profile, reference_name, claim):
        if not token or token not in self.tokens or token in self.consumed or self.tokens[token]["expired"]:
            raise Refused("This manager approval is missing or has expired. [POS-AUTH-EXPIRED]")
        rec = self.tokens[token]
        if rec["requester"] != self.store.current_user:
            raise Refused("This manager approval belongs to a different till session. [POS-AUTH-CHANGED]")
        if rec["approver"] == self.store.current_user:
            raise PermissionDenied("You cannot approve your own request. [POS-AUTH-SELF]")
        if (
            rec["privilege"] != privilege
            or rec["pos_profile"] != pos_profile
            or (rec["reference_name"] or "") != (reference_name or "")
            or rec["claim_json"] != self._canonical(claim)
        ):
            raise Refused("The approved values changed after manager approval. [POS-AUTH-CHANGED]")
        self.consumed.add(token)
        self.logs.append({"outcome": "Consumed", **rec})
        return Row(approver=rec["approver"], amount=(claim or {}).get("amount"), claim=dict(claim or {}))

    def require(self, privilege, pos_profile, reference_name, claim, token=None, percent=None, amount=None):
        if token:
            return self.consume(token, privilege, pos_profile, reference_name, claim)
        if self.may_perform(self.store.current_user, pos_profile, privilege, percent=percent, amount=amount):
            return None
        raise PermissionDenied("A manager must approve this action. [POS-AUTH-REQUIRED]")


class MemoryStore:
    def __init__(self):
        self.current_user = "cashier@example.com"
        self.seqs: dict[str, int] = {}
        self.user_roles: dict[str, list[str]] = {
            "Administrator": ["System Manager"],
            "cashier@example.com": ["URY Cashier"],
            "cashier2@example.com": ["URY Cashier"],
            "manager@example.com": ["URY Manager"],
            "manager2@example.com": ["URY Manager"],
            "waiter@example.com": ["URY Captain"],
        }
        self.user_branches: dict[str, str] = {
            "cashier@example.com": "Main Branch",
            "cashier2@example.com": "Main Branch",
            "manager@example.com": "Main Branch",
            "manager2@example.com": "Main Branch",
            "waiter@example.com": "Main Branch",
        }
        self.user_names: dict[str, str] = {
            "cashier@example.com": "Grace Cashier",
            "cashier2@example.com": "Peter Cashier",
            "manager@example.com": "Alex Manager",
            "manager2@example.com": "Sam Manager",
        }
        self.companies: dict[str, Row] = {
            "Hotel Co": Row(name="Hotel Co", default_currency="UGX"),
        }
        self.currencies: dict[str, Row] = {
            "UGX": Row(name="UGX", fraction_units=1, smallest_currency_fraction_value=1),
            "USD": Row(name="USD", fraction_units=100, smallest_currency_fraction_value=0.01),
            "INR": Row(name="INR", fraction_units=100, smallest_currency_fraction_value=0.01),
        }
        self.accounts: dict[str, Row] = {
            "Cash - HC": Row(name="Cash - HC", company="Hotel Co", root_type="Asset", account_currency="UGX", is_group=0, disabled=0),
            "Cash USD - HC": Row(name="Cash USD - HC", company="Hotel Co", root_type="Asset", account_currency="USD", is_group=0, disabled=0),
            "Safe - HC": Row(name="Safe - HC", company="Hotel Co", root_type="Asset", account_currency="UGX", is_group=0, disabled=0),
            "Safe USD - HC": Row(name="Safe USD - HC", company="Hotel Co", root_type="Asset", account_currency="USD", is_group=0, disabled=0),
            "Paid Out - HC": Row(name="Paid Out - HC", company="Hotel Co", root_type="Expense", account_currency="UGX", is_group=0, disabled=0),
            "Paid Out USD - HC": Row(name="Paid Out USD - HC", company="Hotel Co", root_type="Expense", account_currency="USD", is_group=0, disabled=0),
            "Paid In - HC": Row(name="Paid In - HC", company="Hotel Co", root_type="Income", account_currency="UGX", is_group=0, disabled=0),
            "Paid In USD - HC": Row(name="Paid In USD - HC", company="Hotel Co", root_type="Income", account_currency="USD", is_group=0, disabled=0),
            "Tips Payable - HC": Row(name="Tips Payable - HC", company="Hotel Co", root_type="Liability", account_currency="UGX", is_group=0, disabled=0),
            "Over Short - HC": Row(name="Over Short - HC", company="Hotel Co", root_type="Expense", account_currency="UGX", is_group=0, disabled=0),
            "Card - HC": Row(name="Card - HC", company="Hotel Co", root_type="Asset", account_currency="UGX", is_group=0, disabled=0),
        }
        self.modes: dict[str, Row] = {
            "Cash": Row(name="Cash", type="Cash", custom_overtender_rule="Refuse", custom_halo_max_amount=500000),
            "Cash USD": Row(name="Cash USD", type="Cash", custom_overtender_rule="Refuse", custom_halo_max_amount=200),
            "Card": Row(name="Card", type="Bank", custom_overtender_rule="Refuse", custom_halo_max_amount=2000000),
        }
        self.mode_accounts: dict[tuple[str, str], str] = {
            ("Cash", "Hotel Co"): "Cash - HC",
            ("Cash USD", "Hotel Co"): "Cash USD - HC",
            ("Card", "Hotel Co"): "Card - HC",
        }
        self.profiles: dict[str, Row] = {
            "Main Till": Row(
                doctype="POS Profile",
                name="Main Till",
                company="Hotel Co",
                branch="Main Branch",
                currency="UGX",
                cost_center="Main - HC",
                payments=[Row(mode_of_payment="Cash", default=1), Row(mode_of_payment="Card", default=0)],
                applicable_for_users=[
                    Row(user="cashier@example.com"),
                    Row(user="manager@example.com"),
                    Row(user="manager2@example.com"),
                ],
                custom_enable_cash_movements=1,
                custom_cash_safe_account="Safe - HC",
                custom_paid_out_account="Paid Out - HC",
                custom_paid_in_account="Paid In - HC",
                custom_tips_payable_account="Tips Payable - HC",
                custom_cash_over_short_account="Over Short - HC",
                custom_paid_out_approval_threshold=50000,
                custom_paid_in_approval_threshold=50000,
                custom_cash_movement_approver_role="URY Manager",
                custom_cash_pull_threshold=500000,
                custom_over_short_tolerance=5000,
                custom_blind_cash_count=0,
                custom_cash_drawer_id="DRAWER-MAIN-1",
                qz_print=1,
                qz_host="localhost",
            ),
            "Pool Till": Row(
                doctype="POS Profile",
                name="Pool Till",
                company="Hotel Co",
                branch="Main Branch",
                currency="UGX",
                cost_center="Main - HC",
                payments=[Row(mode_of_payment="Cash", default=1)],
                applicable_for_users=[Row(user="cashier2@example.com"), Row(user="manager@example.com")],
                custom_enable_cash_movements=1,
                custom_cash_safe_account="Safe - HC",
                custom_paid_out_account="Paid Out - HC",
                custom_paid_in_account="Paid In - HC",
                custom_cash_over_short_account="Over Short - HC",
                custom_paid_out_approval_threshold=50000,
                custom_paid_in_approval_threshold=50000,
                custom_cash_movement_approver_role="URY Manager",
                custom_cash_pull_threshold=0,
                custom_over_short_tolerance=0,
                custom_blind_cash_count=0,
                custom_cash_drawer_id="DRAWER-POOL-1",
            ),
            "USD Till": Row(
                doctype="POS Profile",
                name="USD Till",
                company="Hotel Co",
                branch="Main Branch",
                currency="USD",
                cost_center="Main - HC",
                payments=[Row(mode_of_payment="Cash USD", default=1)],
                applicable_for_users=[Row(user="cashier@example.com"), Row(user="manager@example.com")],
                custom_enable_cash_movements=1,
                custom_cash_safe_account="Safe USD - HC",
                custom_paid_out_account="Paid Out USD - HC",
                custom_paid_in_account="Paid In USD - HC",
                custom_cash_over_short_account="Over Short - HC",
                custom_paid_out_approval_threshold=50,
                custom_paid_in_approval_threshold=50,
                custom_cash_movement_approver_role="URY Manager",
                custom_cash_pull_threshold=500,
                custom_over_short_tolerance=5,
                custom_blind_cash_count=0,
                custom_cash_drawer_id="DRAWER-USD-1",
            ),
        }
        self.openings: dict[str, FakeDoc] = {
            "OPEN-MAIN-1": FakeDoc(
                {
                    "doctype": "POS Opening Entry",
                    "name": "OPEN-MAIN-1",
                    "pos_profile": "Main Till",
                    "company": "Hotel Co",
                    "branch": "Main Branch",
                    "user": "cashier@example.com",
                    "status": "Open",
                    "docstatus": 1,
                    "period_start_date": "2026-11-24 08:00:00",
                    "balance_details": [Row(mode_of_payment="Cash", opening_amount=100000)],
                },
                store=self,
            ),
            "OPEN-POOL-1": FakeDoc(
                {
                    "doctype": "POS Opening Entry",
                    "name": "OPEN-POOL-1",
                    "pos_profile": "Pool Till",
                    "company": "Hotel Co",
                    "branch": "Main Branch",
                    "user": "cashier2@example.com",
                    "status": "Open",
                    "docstatus": 1,
                    "period_start_date": "2026-11-24 08:00:00",
                    "balance_details": [Row(mode_of_payment="Cash", opening_amount=50000)],
                },
                store=self,
            ),
            "OPEN-USD-1": FakeDoc(
                {
                    "doctype": "POS Opening Entry",
                    "name": "OPEN-USD-1",
                    "pos_profile": "USD Till",
                    "company": "Hotel Co",
                    "branch": "Main Branch",
                    "user": "cashier@example.com",
                    "status": "Open",
                    "docstatus": 1,
                    "period_start_date": "2026-11-24 08:00:00",
                    "balance_details": [Row(mode_of_payment="Cash USD", opening_amount=100)],
                },
                store=self,
            ),
        }
        self.journal_entries: dict[str, FakeDoc] = {}
        self.drawer_events: dict[str, FakeDoc] = {}
        self.pos_invoices: dict[str, FakeDoc] = {}
        self.reasons: dict[str, Row] = {
            "CHANGE": Row(name="CHANGE", code="CHANGE", description="Make Change", applies_to="no_sale", requires_note=0, disabled=0),
            "OTHER_PO": Row(name="OTHER_PO", code="OTHER_PO", description="Other Expense", applies_to="paid_out", requires_note=1, disabled=0),
            "ICE": Row(name="ICE", code="ICE", description="Ice / Supplies", applies_to="paid_out", requires_note=0, disabled=0),
        }
        self.privilege_rules: dict[str, list[dict]] = {}
        self.exchange_rates: dict[tuple[str, str], float] = {("USD", "UGX"): 3700.0}
        self.je_hook_mod = None
        self.drawer_event_cls = None

    def next_seq(self, prefix):
        self.seqs[prefix] = self.seqs.get(prefix, 0) + 1
        return self.seqs[prefix]

    def save_doc(self, doc):
        dt = doc.get("doctype")
        if dt == "Journal Entry":
            self.journal_entries[doc.name] = doc
        elif dt == "POS Drawer Event":
            self.drawer_events[doc.name] = doc
        elif dt == "POS Opening Entry":
            self.openings[doc.name] = doc

    def run_je_hook(self, event, doc):
        if self.je_hook_mod and hasattr(self.je_hook_mod, event):
            getattr(self.je_hook_mod, event)(doc, event)

    def run_drawer_event_validate(self, doc):
        if self.drawer_event_cls is not None:
            self.drawer_event_cls.validate(doc)


def _match_filters(doc, filters):
    if not filters:
        return True
    if isinstance(filters, str):
        return doc.get("name") == filters
    for k, cond in filters.items():
        val = doc.get(k)
        if isinstance(cond, (list, tuple)) and len(cond) == 2:
            op, arg = cond[0].lower(), cond[1]
            if op == "in" and val not in arg:
                return False
            elif op == "not in" and val in arg:
                return False
            elif op == "=" and val != arg:
                return False
            elif op == "!=" and val == arg:
                return False
            elif op == "is":
                if arg == "set" and not val:
                    return False
                if arg == "not set" and val:
                    return False
        else:
            if val != cond:
                return False
    return True


def _build_env():
    store = MemoryStore()
    override = FakeOverride(store)
    frappe_mod = MagicMock()
    frappe_mod.ValidationError = Refused
    frappe_mod.PermissionError = PermissionDenied
    frappe_mod.DoesNotExistError = KeyError
    frappe_mod._ = lambda msg, *args: msg
    frappe_mod._dict = Row
    frappe_mod.flags = SimpleNamespace()
    frappe_mod.conf = {}
    frappe_mod.whitelist = lambda *a, **kw: (a[0] if a and callable(a[0]) else (lambda fn: fn))

    class _Session:
        @property
        def user(self):
            return store.current_user

        @user.setter
        def user(self, v):
            store.current_user = v

    frappe_mod.session = _Session()

    def throw(msg, exc=None, **kwargs):
        raise (exc or Refused)(msg)

    frappe_mod.throw = throw
    frappe_mod.get_roles = lambda user=None: list(store.user_roles.get(user or store.current_user, []))

    def _table_docs(doctype):
        if doctype == "POS Profile":
            return list(store.profiles.values())
        if doctype == "POS Opening Entry":
            return list(store.openings.values())
        if doctype == "Journal Entry":
            return list(store.journal_entries.values())
        if doctype == "POS Drawer Event":
            return list(store.drawer_events.values())
        if doctype == "POS Invoice":
            return list(store.pos_invoices.values())
        if doctype == "POS Reason":
            return list(store.reasons.values())
        if doctype == "Account":
            return list(store.accounts.values())
        if doctype == "Mode of Payment":
            return list(store.modes.values())
        if doctype == "Company":
            return list(store.companies.values())
        if doctype == "Currency":
            return list(store.currencies.values())
        return []

    def get_value(doctype, filters=None, fieldname=None, as_dict=False, for_update=False, **kwargs):
        if doctype == "Mode of Payment Account":
            acct = store.mode_accounts.get((filters.get("parent"), filters.get("company")))
            return Row(default_account=acct) if as_dict and acct else acct
        if doctype == "User":
            uid = filters if isinstance(filters, str) else filters.get("name")
            if fieldname == "full_name":
                return store.user_names.get(uid, uid)
            return uid
        docs = _table_docs(doctype)
        match = next((d for d in docs if _match_filters(d, filters)), None)
        if not match:
            return None
        if isinstance(fieldname, (list, tuple)):
            data = {f: match.get(f) for f in fieldname}
            return Row(data) if as_dict else tuple(data[f] for f in fieldname)
        return match.get(fieldname)

    def get_all(doctype, filters=None, fields=None, pluck=None, order_by=None, limit_page_length=None, **kwargs):
        if doctype == "Journal Entry Account":
            parent = (filters or {}).get("parent")
            je = store.journal_entries.get(parent)
            rows = list(je.get("accounts") or []) if je else []
            if pluck:
                return [r.get(pluck) for r in rows]
            return rows
        docs = [d for d in _table_docs(doctype) if _match_filters(d, filters)]
        if pluck:
            return [d.get(pluck) for d in docs]
        if fields:
            return [Row({f: d.get(f) for f in fields}) for d in docs]
        return docs

    def get_doc(arg1, arg2=None, **kwargs):
        if isinstance(arg1, dict):
            return FakeDoc(arg1, store=store)
        docs = _table_docs(arg1)
        match = next((d for d in docs if d.get("name") == arg2), None)
        if not match:
            raise KeyError(f"{arg1} {arg2} not found")
        return match

    def new_doc(doctype):
        return FakeDoc({"doctype": doctype}, store=store)

    def exists(doctype, filters=None):
        if isinstance(filters, str):
            return any(d.get("name") == filters for d in _table_docs(doctype))
        return any(_match_filters(d, filters) for d in _table_docs(doctype))

    def db_count(doctype, filters=None):
        return len([d for d in _table_docs(doctype) if _match_filters(d, filters)])

    frappe_mod.db.get_value.side_effect = get_value
    frappe_mod.db.exists.side_effect = exists
    frappe_mod.db.count.side_effect = db_count
    frappe_mod.db.table_exists.side_effect = lambda dt: True
    frappe_mod.get_cached_value.side_effect = get_value
    frappe_mod.get_value.side_effect = get_value
    frappe_mod.get_all.side_effect = get_all
    frappe_mod.get_list.side_effect = get_all
    frappe_mod.get_doc.side_effect = get_doc
    frappe_mod.new_doc.side_effect = new_doc
    frappe_mod.get_precision.return_value = 2

    def flt_impl(val, precision=None):
        if val is None or val == "":
            return 0.0
        n = float(val)
        return round(n, precision) if precision is not None else n

    utils_mod = ModuleType("frappe.utils")
    utils_mod.flt = flt_impl
    utils_mod.cint = lambda v: int(float(v or 0))
    utils_mod.nowdate = lambda: "2026-11-24"
    utils_mod.now_datetime = lambda: datetime(2026, 11, 24, 10, 30, 0)
    utils_mod.get_datetime = lambda v: v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    utils_mod.get_time = lambda v: datetime.strptime(str(v), "%H:%M:%S").time() if isinstance(v, str) else v
    frappe_mod.utils = utils_mod

    # Query builder stub for till_cash_movements
    qb_fn_mod = ModuleType("frappe.query_builder.functions")
    qb_fn_mod.Sum = lambda expr: expr

    # ERPNext exchange rate stub
    erpnext_setup_utils = ModuleType("erpnext.setup.utils")
    erpnext_setup_utils.get_exchange_rate = lambda src, dst, *a, **kw: (
        1.0 if src == dst else store.exchange_rates.get((src, dst), 0.0)
    )

    api_stub = ModuleType("ury.ury_pos.api")
    api_stub.getBranch = lambda: store.user_branches.get(store.current_user)

    override_stub = ModuleType("ury.ury_pos.override")
    override_stub.PRIVILEGES = override.PRIVILEGES
    override_stub.is_policy_active = override.is_policy_active
    override_stub.may_perform = override.may_perform
    override_stub.consume = override.consume
    override_stub.require = override.require

    brand_spec = importlib.util.spec_from_file_location("ury.brand", APP / "brand.py")
    brand_mod = importlib.util.module_from_spec(brand_spec)

    modules = {
        "frappe": frappe_mod,
        "frappe.utils": utils_mod,
        "frappe.model": ModuleType("frappe.model"),
        "frappe.model.document": ModuleType("frappe.model.document"),
        "frappe.query_builder": ModuleType("frappe.query_builder"),
        "frappe.query_builder.functions": qb_fn_mod,
        "erpnext": ModuleType("erpnext"),
        "erpnext.setup": ModuleType("erpnext.setup"),
        "erpnext.setup.utils": erpnext_setup_utils,
        "ury.brand": brand_mod,
        "ury.ury_pos.api": api_stub,
        "ury.ury_pos.override": override_stub,
    }
    modules["frappe.model.document"].Document = FakeDoc

    return store, override, frappe_mod, brand_spec, brand_mod, modules


class TestTillCashControlsFixtures(unittest.TestCase):
    """Schema & fixture contracts for Slice 1..3 custom fields and POS Drawer Event."""

    def test_custom_fields_and_hooks_filter_include_all_till_cash_controls_fields(self):
        rows = json.loads(CUSTOM_FIELDS_JSON.read_text(encoding="utf-8"))
        names = {r["name"] for r in rows}
        required = {
            "POS Profile-custom_paid_in_account",
            "POS Profile-custom_paid_in_approval_threshold",
            "POS Profile-custom_cash_pull_threshold",
            "POS Profile-custom_over_short_tolerance",
            "POS Profile-custom_cash_drawer_id",
            "Mode of Payment-custom_halo_max_amount",
            "Journal Entry-custom_cash_movement_reference",
            "POS Closing Entry-custom_over_short_reason",
            "POS Closing Entry-custom_over_short_approver",
            "POS Closing Entry-custom_close_approver",
            "POS Closing Entry-custom_force_closed",
            "POS Closing Entry-custom_force_close_reason",
            "POS Closing Entry-custom_no_sale_count",
            "POS Invoice-custom_halo_approver",
        }
        missing = sorted(required - names)
        self.assertEqual(missing, [], f"Missing Custom Fields in custom_field.json: {missing}")

        hooks_text = HOOKS_PY.read_text(encoding="utf-8")
        for field_name in sorted(required):
            self.assertIn(f'"{field_name}"', hooks_text, f"{field_name} missing from hooks.py fixtures filter")

    def test_pos_drawer_event_doctype_schema_is_insert_only_audit_log(self):
        schema_path = DRAWER_EVENT_DIR / "pos_drawer_event.json"
        self.assertTrue(schema_path.exists(), "POS Drawer Event DocType json must exist")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["name"], "POS Drawer Event")
        fieldnames = {f["fieldname"] for f in schema["fields"]}
        for required in (
            "event_type",
            "company",
            "pos_profile",
            "branch",
            "pos_opening_entry",
            "pos_closing_entry",
            "operator",
            "approver",
            "reason",
            "note",
            "drawer_expected_cash",
            "threshold_amount",
            "drawer_kick_sent",
            "open_checks_snapshot",
            "event_time",
        ):
            self.assertIn(required, fieldnames)
        event_field = next(f for f in schema["fields"] if f["fieldname"] == "event_type")
        self.assertEqual(
            event_field["options"].split("\n"),
            ["No Sale", "Threshold Exception", "Till Reopen", "Force Close"],
        )
        for perm in schema.get("permissions") or []:
            self.assertEqual(perm.get("write", 0), 0, "POS Drawer Event must be insert-only")
            self.assertEqual(perm.get("delete", 0), 0, "POS Drawer Event must not be deletable")


class LoadedSlice1(unittest.TestCase):
    def setUp(self):
        self.store, self.override, self.frappe, brand_spec, brand_mod, modules = _build_env()
        self.enterContext(patch.dict(sys.modules, modules))
        brand_spec.loader.exec_module(brand_mod)

        # Load POS Drawer Event controller
        ctrl_path = DRAWER_EVENT_DIR / "pos_drawer_event.py"
        if not ctrl_path.exists():
            self.fail(f"Missing controller: {ctrl_path}")
        ctrl_spec = importlib.util.spec_from_file_location(
            "ury.ury.doctype.pos_drawer_event.pos_drawer_event", ctrl_path
        )
        self.drawer_ctrl_mod = importlib.util.module_from_spec(ctrl_spec)
        self.enterContext(
            patch.dict(sys.modules, {"ury.ury.doctype.pos_drawer_event.pos_drawer_event": self.drawer_ctrl_mod})
        )
        ctrl_spec.loader.exec_module(self.drawer_ctrl_mod)
        self.store.drawer_event_cls = self.drawer_ctrl_mod.POSDrawerEvent

        # Load ury_cash_movement hook
        hook_spec = importlib.util.spec_from_file_location("ury.ury.hooks.ury_cash_movement", CASH_MOVEMENT_HOOK)
        self.cash_hook = importlib.util.module_from_spec(hook_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury.hooks.ury_cash_movement": self.cash_hook}))
        hook_spec.loader.exec_module(self.cash_hook)
        self.store.je_hook_mod = self.cash_hook

        # Replace till_cash_movements with an in-memory aggregator honoring account vs company currency
        self._orig_till_cash_movements = self.cash_hook.till_cash_movements

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

        # Load till_controls module under test
        if not TILL_CONTROLS_PY.exists():
            self.fail(f"Missing module: {TILL_CONTROLS_PY}")
        tc_spec = importlib.util.spec_from_file_location("ury.ury_pos.till_controls", TILL_CONTROLS_PY)
        self.tc = importlib.util.module_from_spec(tc_spec)
        self.enterContext(patch.dict(sys.modules, {"ury.ury_pos.till_controls": self.tc}))
        tc_spec.loader.exec_module(self.tc)


class TestPaidOutAndPaidInWithPin(LoadedSlice1):
    """Matrix row 24: Paid Out / Paid In above limit with Manager PIN in place, never Desk."""

    def test_paid_out_above_limit_refuses_without_manager_pin(self):
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Paid Out",
                amount=120000,
                reason="Ice delivery",
                reference="RCP-881",
            )
        self.assertEqual(self.store.journal_entries, {})

    def test_paid_out_above_limit_posts_in_place_with_manager_pin_and_keeps_cashier_session(self):
        self.store.current_user = "cashier@example.com"
        claim = {
            "action": "paid_out",
            "movement_type": "Paid Out",
            "amount": 120000,
            "reason": "Ice delivery",
            "reference": "RCP-881",
            "paid_out_target": "expense",
            "pos_opening_entry": "OPEN-MAIN-1",
        }
        self.override.mint(
            "tok-po-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="paid_out",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        res = self.tc.record_cash_movement(
            pos_profile="Main Till",
            movement_type="Paid Out",
            amount=120000,
            reason="Ice delivery",
            reference="RCP-881",
            approval_token="tok-po-1",
        )
        self.assertEqual(self.store.current_user, "cashier@example.com")
        self.assertEqual(res["docstatus"], 1)
        self.assertEqual(res["approver"], "manager@example.com")
        self.assertEqual(res["reference"], "RCP-881")
        je = self.store.journal_entries[res["name"]]
        self.assertEqual(je.docstatus, 1)
        self.assertEqual(je.custom_cash_movement_type, "Paid Out")
        self.assertEqual(je.custom_cash_movement_reason, "Ice delivery")
        self.assertEqual(je.custom_cash_movement_reference, "RCP-881")
        self.assertEqual(je.custom_cash_movement_approver, "manager@example.com")

        # Token is single-use: replaying it must fail
        with self.assertRaisesRegex(Refused, r"POS-AUTH-EXPIRED"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Paid Out",
                amount=120000,
                reason="Ice delivery",
                reference="RCP-881",
                approval_token="tok-po-1",
            )

    def test_paid_out_requires_reason_and_note_when_reason_requires_note(self):
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(Refused, r"POS-CASH-REASON"):
            self.tc.record_cash_movement(
                pos_profile="Main Till", movement_type="Paid Out", amount=10000, reason="   "
            )
        with self.assertRaisesRegex(Refused, r"POS-CASH-REFERENCE"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Paid Out",
                amount=10000,
                reason="OTHER_PO",
                reference="",
            )

    def test_paid_in_posts_native_cash_entry_debits_drawer_and_gates_above_limit(self):
        self.store.current_user = "cashier@example.com"
        # Within limit (<= 50,000) succeeds solo for cashier
        small = self.tc.record_cash_movement(
            pos_profile="Main Till",
            movement_type="Paid In",
            amount=30000,
            reason="Bottle deposit refund",
            reference="DEP-10",
        )
        self.assertEqual(small["docstatus"], 1)
        self.assertEqual(small["drawer_change"], 30000)
        je = self.store.journal_entries[small["name"]]
        drawer_line = next(r for r in je.accounts if r.account == "Cash - HC")
        counter_line = next(r for r in je.accounts if r.account == "Paid In - HC")
        self.assertEqual((drawer_line.debit_in_account_currency, drawer_line.credit_in_account_currency), (30000, 0))
        self.assertEqual((counter_line.debit_in_account_currency, counter_line.credit_in_account_currency), (0, 30000))

        # Above limit (> 50,000) requires manager PIN
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Paid In",
                amount=80000,
                reason="Advance cash deposit",
                reference="ADV-22",
            )

        claim = {
            "action": "paid_in",
            "movement_type": "Paid In",
            "amount": 80000,
            "reason": "Advance cash deposit",
            "reference": "ADV-22",
            "paid_out_target": "expense",
            "pos_opening_entry": "OPEN-MAIN-1",
        }
        self.override.mint(
            "tok-pi-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="paid_out",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        approved = self.tc.record_cash_movement(
            pos_profile="Main Till",
            movement_type="Paid In",
            amount=80000,
            reason="Advance cash deposit",
            reference="ADV-22",
            approval_token="tok-pi-1",
        )
        self.assertEqual(approved["docstatus"], 1)
        self.assertEqual(approved["approver"], "manager@example.com")

    def test_adversarial_cashier_cannot_self_approve_paid_out(self):
        self.store.current_user = "cashier@example.com"
        claim = {
            "action": "paid_out",
            "movement_type": "Paid Out",
            "amount": 90000,
            "reason": "Fuel",
            "reference": "F-1",
            "paid_out_target": "expense",
            "pos_opening_entry": "OPEN-MAIN-1",
        }
        self.override.mint(
            "tok-self",
            requester="cashier@example.com",
            approver="cashier@example.com",
            privilege="paid_out",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-SELF"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Paid Out",
                amount=90000,
                reason="Fuel",
                reference="F-1",
                approval_token="tok-self",
            )

    def test_adversarial_till_with_no_limit_set_fails_closed_for_cashier(self):
        self.store.profiles["Main Till"]["custom_paid_out_approval_threshold"] = None
        self.store.profiles["Main Till"]["custom_paid_in_approval_threshold"] = None
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Paid Out",
                amount=1000,
                reason="Matches",
            )
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Paid In",
                amount=1000,
                reason="Found change",
            )

    def test_adversarial_negative_zero_and_non_finite_amounts_refused(self):
        self.store.current_user = "cashier@example.com"
        for bad in (0, -1, -50000, float("nan"), float("inf"), "not-a-number"):
            with self.subTest(amount=bad):
                with self.assertRaisesRegex(Refused, r"POS-CASH-AMOUNT"):
                    self.tc.record_cash_movement(
                        pos_profile="Main Till",
                        movement_type="Pickup",
                        amount=bad,
                    )

    def test_adversarial_zero_decimal_ugx_refuses_fractional_amount(self):
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(Refused, r"POS-CASH-PRECISION"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Pickup",
                amount=50000.5,
            )
        ok = self.tc.record_cash_movement(
            pos_profile="Main Till",
            movement_type="Pickup",
            amount=50000,
        )
        self.assertEqual(ok["amount"], 50000)

    def test_adversarial_foreign_currency_drawer_posts_multi_currency_je_in_drawer_currency(self):
        self.store.current_user = "cashier@example.com"
        res = self.tc.record_cash_movement(
            pos_profile="USD Till",
            movement_type="Pickup",
            amount=25.50,
            reference="DROP-USD-1",
        )
        je = self.store.journal_entries[res["name"]]
        self.assertEqual(je.multi_currency, 1)
        drawer = next(r for r in je.accounts if r.account == "Cash USD - HC")
        safe = next(r for r in je.accounts if r.account == "Safe USD - HC")
        self.assertEqual(drawer.account_currency, "USD")
        self.assertEqual(drawer.exchange_rate, 3700.0)
        self.assertEqual(drawer.credit_in_account_currency, 25.50)
        self.assertAlmostEqual(drawer.credit, 25.50 * 3700.0, places=2)
        self.assertEqual(safe.debit_in_account_currency, 25.50)

        status = self.tc.get_till_drawer_status("USD Till")
        self.assertEqual(status["currency"], "USD")
        self.assertAlmostEqual(status["expected_drawer_cash"], 74.50, places=2)


class TestPickupLoanThresholdAndNoSale(LoadedSlice1):
    """Matrix rows 23 & 25: Pickup/Loan/Safe Drop, threshold prompt + exception, No Sale."""

    def test_loan_requires_manager_pin_for_cashier_and_succeeds_with_pin(self):
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.record_cash_movement(
                pos_profile="Main Till",
                movement_type="Loan",
                amount=100000,
                reason="Change float",
                reference="LOAN-1",
            )
        claim = {
            "action": "cash_loan",
            "movement_type": "Loan",
            "amount": 100000,
            "reason": "Change float",
            "reference": "LOAN-1",
            "paid_out_target": "expense",
            "pos_opening_entry": "OPEN-MAIN-1",
        }
        self.override.mint(
            "tok-loan-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="cash_loan",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        res = self.tc.record_cash_movement(
            pos_profile="Main Till",
            movement_type="Loan",
            amount=100000,
            reason="Change float",
            reference="LOAN-1",
            approval_token="tok-loan-1",
        )
        self.assertEqual(res["docstatus"], 1)
        self.assertEqual(res["drawer_change"], 100000)

    def test_cash_pull_threshold_prompt_respects_blind_mode_and_logs_exception_with_pin(self):
        # Add cash sale bringing expected drawer cash to 600,000 (>= 500,000 threshold)
        self.store.pos_invoices["INV-1"] = FakeDoc(
            {
                "doctype": "POS Invoice",
                "name": "INV-1",
                "docstatus": 1,
                "pos_profile": "Main Till",
                "consolidated_invoice": None,
                "posting_date": "2026-11-24",
                "posting_time": "09:15:00",
                "change_amount": 0,
                "grand_total": 500000,
                "payments": [Row(mode_of_payment="Cash", amount=500000, account="Cash - HC")],
            }
        )
        self.store.current_user = "cashier@example.com"
        status = self.tc.get_till_drawer_status("Main Till")
        self.assertTrue(status["threshold_exceeded"])
        self.assertEqual(status["cash_pull_threshold"], 500000)
        self.assertEqual(status["expected_drawer_cash"], 600000)
        self.assertGreater(status["suggested_pickup_amount"], 0)

        # In blind close mode, expected_drawer_cash MUST NOT be leaked to cashier
        self.store.profiles["Main Till"]["custom_blind_cash_count"] = 1
        blind_status = self.tc.get_till_drawer_status("Main Till")
        self.assertTrue(blind_status["threshold_exceeded"])
        self.assertIsNone(blind_status["expected_drawer_cash"])
        self.assertIsNone(blind_status["suggested_pickup_amount"])

        # Logging threshold exception requires Manager PIN + reason
        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.log_cash_pull_exception(
                pos_profile="Main Till",
                reason="Rush hour - drop after lunch",
            )

        claim = {
            "action": "threshold_exception",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "threshold_amount": 500000,
            "reason": "Rush hour - drop after lunch",
        }
        self.override.mint(
            "tok-exc-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="cash_pickup",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        exc = self.tc.log_cash_pull_exception(
            pos_profile="Main Till",
            reason="Rush hour - drop after lunch",
            approval_token="tok-exc-1",
        )
        self.assertEqual(exc["event_type"], "Threshold Exception")
        self.assertEqual(exc["approver"], "manager@example.com")
        ev = self.store.drawer_events[exc["name"]]
        self.assertEqual(ev.threshold_amount, 500000)
        self.assertEqual(ev.drawer_expected_cash, 600000)

    def test_no_sale_requires_reason_and_pin_for_cashier_and_counts_on_x_report(self):
        self.store.current_user = "cashier@example.com"
        with self.assertRaisesRegex(Refused, r"POS-NO-SALE-REASON"):
            self.tc.trigger_no_sale(pos_profile="Main Till", reason="  ")

        with self.assertRaisesRegex(PermissionDenied, r"POS-AUTH-REQUIRED"):
            self.tc.trigger_no_sale(pos_profile="Main Till", reason="CHANGE")

        claim = {
            "action": "no_sale",
            "pos_profile": "Main Till",
            "pos_opening_entry": "OPEN-MAIN-1",
            "reason": "CHANGE",
            "note": "",
        }
        self.override.mint(
            "tok-ns-1",
            requester="cashier@example.com",
            approver="manager@example.com",
            privilege="no_sale",
            pos_profile="Main Till",
            reference_name="OPEN-MAIN-1",
            claim=claim,
        )
        res = self.tc.trigger_no_sale(
            pos_profile="Main Till",
            reason="CHANGE",
            approval_token="tok-ns-1",
        )
        self.assertEqual(res["drawer_kick_hex"], "1b700019fa")
        self.assertEqual(res["no_sale_count_for_shift"], 1)
        self.assertEqual(res["approver"], "manager@example.com")
        self.assertEqual(self.store.current_user, "cashier@example.com")

        # Manager acting solo (no rules configured = today's manager roles may act)
        self.store.current_user = "manager@example.com"
        res2 = self.tc.trigger_no_sale(pos_profile="Main Till", reason="CHANGE")
        self.assertEqual(res2["no_sale_count_for_shift"], 2)
        self.assertIsNone(res2["approver"])

        # X-report reports exact shift No Sale count
        x_rep = self.tc.get_shift_x_report("Main Till")
        self.assertEqual(x_rep["no_sale_count"], 2)

        # Immutable audit event: cannot edit or delete
        ev = self.store.drawer_events[res["event_name"]]
        ev.reason = "Tampered"
        with self.assertRaisesRegex(Refused, r"POS-DRAWER-IMMUTABLE"):
            ev.save()
        with self.assertRaisesRegex(Refused, r"POS-DRAWER-IMMUTABLE"):
            self.drawer_ctrl_mod.POSDrawerEvent.on_trash(ev)

    def test_adversarial_two_tills_on_one_drawer_isolate_movements_and_block_physical_drawer_collision(self):
        # Main Till and Pool Till share GL account "Cash - HC"
        self.store.current_user = "cashier@example.com"
        self.tc.record_cash_movement(pos_profile="Main Till", movement_type="Pickup", amount=20000)

        self.store.current_user = "cashier2@example.com"
        self.tc.record_cash_movement(pos_profile="Pool Till", movement_type="Pickup", amount=10000)

        main_x = self.tc.get_shift_x_report("Main Till")
        pool_x = self.tc.get_shift_x_report("Pool Till")
        self.assertEqual(main_x["net_drawer_movement"], -20000)
        self.assertEqual(pool_x["net_drawer_movement"], -10000)
        self.assertEqual(len(main_x["movements"]), 1)
        self.assertEqual(len(pool_x["movements"]), 1)

        # Two open tills claiming the SAME physical cash drawer ID must collide safely
        self.store.profiles["Pool Till"]["custom_cash_drawer_id"] = "DRAWER-MAIN-1"
        self.store.current_user = "manager@example.com"
        with self.assertRaisesRegex(Refused, r"POS-DRAWER-IN-USE"):
            self.tc.trigger_no_sale(pos_profile="Pool Till", reason="CHANGE")


if __name__ == "__main__":
    unittest.main()
