"""Server-side POS discount governance, preview, check mutations, split/merge/return, and REST guard (site-less).

Matches pos-override-contract.md and pos-pms-side-gaps.md §4.1–4.7:
- DISCOUNT_ALLOWED_ROLES and MAX_DISCOUNT_PERCENTAGE are deleted.
- Blank limit is 0% (with or without siteops sales-discount-control@3 installed).
- Reason is mandatory (active, POS/Both, note when requires_note, approval when requires_supervisor).
- 100% comp ALWAYS requires a second person, even for a manager with a 100% role limit.
- Consumed single-use approval token alone sets doc.flags.discount_approval = {approver, percent}.
- Changed claim after approval, removed then re-added, partial tender floor, UGX 0-decimal rounding,
  tax-inclusive & tax-exclusive preview, line + check discount together, negative/NaN/huge inputs,
  REST straight to save, split/merge checks with discount, and partial return flat-discount proration.
"""

import datetime
import importlib.util
import json
import math
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[4]  # repo root
URY_PKG = ROOT / "ury"
ORDER_PATH = URY_PKG / "ury/doctype/ury_order/ury_order.py"
DISCOUNT_PATH = URY_PKG / "ury_pos/discount.py"
HOOK_PATH = URY_PKG / "ury/hooks/ury_pos_invoice.py"
API_PATH = URY_PKG / "ury_pos/api.py"


class Row(dict):
    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self[key]
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value

    def as_dict(self, no_default_fields=False, no_child_table_fields=False, no_private_properties=False, **kw):
        row = dict(self)
        if no_default_fields:
            for key in ("doctype", "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx"):
                row.pop(key, None)
        if no_child_table_fields:
            for key in ("parent", "parentfield", "parenttype"):
                row.pop(key, None)
        return row


class Refused(Exception):
    pass


class NotPermitted(Exception):
    pass


def _stub(name, **attrs):
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _precision_for_currency(currency):
    if currency in ("UGX", "JPY", "KRW", "RWF", "TZS", "XAF", "XOF"):
        return 0
    if currency in ("KWD", "BHD", "OMR"):
        return 3
    return 2


def _make_frappe_double():
    framework = MagicMock()
    framework.PermissionError = NotPermitted
    framework.ValidationError = Refused
    framework.UniqueValidationError = type("UniqueCollision", (Refused,), {})
    framework.AuthenticationError = type("AuthFailed", (NotPermitted,), {})
    framework._ = lambda *messages: messages[0]
    framework.whitelist = lambda *args, **kwargs: (lambda fn: fn)
    framework._dict = Row
    framework.flags = SimpleNamespace()
    framework.conf = {}
    framework.form_dict = {}
    framework.session = SimpleNamespace(user="cashier@example.com", sid="sess-1")
    framework.local = SimpleNamespace(session=framework.session)
    framework.request = SimpleNamespace(
        path="/api/method/ury.ury.doctype.ury_order.ury_order.make_invoice",
        headers={},
        cookies={"sid": "sess-1"},
    )

    cache_store = {}

    class FakeCache:
        def get_value(self, key):
            return cache_store.get(key)

        def set_value(self, key, value, expires_in_sec=None):
            cache_store[key] = value

        def delete_value(self, key):
            cache_store.pop(key, None)

        def delete_key(self, key):
            cache_store.pop(key, None)

    fake_cache = FakeCache()
    framework.cache = lambda: fake_cache
    framework._cache_store = cache_store

    def throw(message, exception=None, **kwargs):
        raise (exception or Refused)(message)

    framework.throw = throw
    framework.parse_json = lambda value: json.loads(value) if isinstance(value, str) else value
    framework.as_json = lambda value: json.dumps(value)

    def flt_fn(value, precision=None):
        if value is None or value == "":
            num = 0.0
        else:
            num = float(value)
        if precision is not None:
            return round(num, int(precision))
        return num

    def cint_fn(value):
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    utils = _stub(
        "frappe.utils",
        cint=cint_fn,
        flt=flt_fn,
        get_datetime=lambda v=None: v if isinstance(v, datetime.datetime) else datetime.datetime(2026, 11, 24, 12, 0, 0),
        get_fullname=lambda u=None: str(u or ""),
        getdate=lambda v=None: v,
        today=lambda: "2026-11-24",
        get_system_timezone=lambda: "Africa/Kampala",
        now_datetime=lambda: datetime.datetime(2026, 11, 24, 12, 0, 0),
        now=lambda: "2026-11-24 12:00:00",
        get_time=lambda v=None: v,
        get_first_day=lambda v=None: v,
        get_last_day=lambda v=None: v,
    )
    framework.utils = utils
    return framework, utils


class FakePOSInvoice:
    """Realistic ERPNext v16 POS Invoice double supporting line discounts, header %/flat
    discounts, tax-inclusive and tax-exclusive VAT, currency precision, and Item.max_discount."""

    def __init__(
        self,
        name="CHK-1",
        items=(),
        taxes=(),
        payments=(),
        currency="UGX",
        company="Lakeside Hotel",
        pos_profile="Lakeside Till",
        apply_discount_on="Grand Total",
        owner="waiter@example.com",
        docstatus=0,
        **extra,
    ):
        self.doctype = "POS Invoice"
        self.name = name
        self.items = [Row(i) for i in items]
        self.taxes = [Row(t) for t in taxes]
        self.payments = [Row(p) for p in payments]
        self.currency = currency
        self.company = company
        self.branch = "Lakeside"
        self.pos_profile = pos_profile
        self.customer = "Walk-in"
        self.order_type = "Dine In"
        self.apply_discount_on = apply_discount_on
        self.additional_discount_percentage = 0.0
        self.discount_amount = 0.0
        self.coupon_code = None
        self.custom_discount_reason = None
        self.custom_discount_note = None
        self.custom_discount_approver = None
        self.custom_discount_approved_percent = 0.0
        self.ury_discount_meta = None
        self.custom_merged_pos_invoice = None
        self.custom_merged_tables = None
        self.restaurant_table = None
        self.invoice_printed = 0
        self.docstatus = docstatus
        self.is_return = 0
        self.return_against = None
        self.is_consolidated = 0
        self.owner = owner
        self.waiter = owner
        self.cashier = "cashier@example.com"
        self.room_number = None
        self.conversion_rate = 1.0
        self.pricing_rules = []
        self.flags = SimpleNamespace()
        self.events = []
        self._is_new = False
        self._doc_before_save = None
        self.total = 0.0
        self.net_total = 0.0
        self.total_taxes_and_charges = 0.0
        self.grand_total = 0.0
        self.rounded_total = 0.0
        self.paid_amount = 0.0
        self.outstanding_amount = 0.0
        for k, v in extra.items():
            setattr(self, k, v)
        self.calculate_taxes_and_totals()

    def precision(self, fieldname, parentfield=None):
        return _precision_for_currency(self.currency)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def append(self, key, row):
        getattr(self, key).append(Row(row))

    def remove(self, item):
        self.items.remove(item)

    def is_new(self):
        return self._is_new

    def get_doc_before_save(self):
        return self._doc_before_save

    def check_permission(self, permtype="read"):
        self.events.append(("permission", permtype))

    def set_missing_values(self, for_validate=False):
        pass

    def run_method(self, name, *args, **kwargs):
        if name == "calculate_taxes_and_totals":
            self.calculate_taxes_and_totals()

    def calculate_taxes_and_totals(self):
        prec = self.precision("grand_total")
        inclusive_rate = sum(
            float(t.get("rate") or 0)
            for t in self.taxes
            if int(t.get("included_in_print_rate") or 0) and (t.get("charge_type") or "") != "Actual"
        )
        exclusive_rate = sum(
            float(t.get("rate") or 0)
            for t in self.taxes
            if not int(t.get("included_in_print_rate") or 0) and (t.get("charge_type") or "") != "Actual"
        )
        actual_tax = sum(
            float(t.get("tax_amount") or 0)
            for t in self.taxes
            if (t.get("charge_type") or "") == "Actual"
        )

        # 1) Item-level discounts — mirrors REAL ERPNext v16 calculate_item_rate():
        # If item.rate is non-zero AND does not equal calculated_rate, ERPNext v16
        # prefers item.rate and resets discount_percentage = 0!
        gross_items = 0.0
        for item in self.items:
            qty = float(item.get("qty") or 0)
            plr = float(item.get("price_list_rate") or 0)
            if not plr:
                item["discount_percentage"] = 0.0
                item["discount_amount"] = 0.0
                rate = round(float(item.get("rate") or 0), prec)
            else:
                pct = float(item.get("discount_percentage") or 0)
                amt = float(item.get("discount_amount") or 0)
                if pct > 0:
                    amt = round(plr * pct / 100.0, prec)
                    item["discount_amount"] = amt
                calculated_rate = round(plr - amt, prec)
                existing_rate = round(float(item.get("rate") or 0), prec)
                if not existing_rate:
                    item["rate"] = calculated_rate
                    rate = calculated_rate
                elif existing_rate == calculated_rate:
                    rate = existing_rate
                else:
                    # ERPNext v16 resets discount_percentage to 0 and keeps existing_rate
                    item["discount_amount"] = round(plr - existing_rate, prec)
                    item["discount_percentage"] = 0.0
                    rate = existing_rate
            item["amount"] = round(rate * qty, prec)
            gross_items += item["amount"]

        self.total = round(gross_items, prec)
        # Pre-header-discount net & tax
        if inclusive_rate > 0:
            pre_net = round(self.total / (1.0 + inclusive_rate / 100.0), prec)
            pre_tax = round(self.total - pre_net + actual_tax, prec)
            pre_grand = round(self.total + actual_tax, prec)
        else:
            pre_net = self.total
            pre_tax = round(pre_net * exclusive_rate / 100.0 + actual_tax, prec)
            pre_grand = round(pre_net + pre_tax, prec)

        # 2) Header-level discount
        basis = pre_net if self.apply_discount_on == "Net Total" else pre_grand
        if float(self.additional_discount_percentage or 0) > 0:
            self.discount_amount = round(basis * float(self.additional_discount_percentage) / 100.0, prec)
        else:
            self.discount_amount = round(float(self.discount_amount or 0), prec)

        if self.apply_discount_on == "Net Total":
            self.net_total = round(pre_net - self.discount_amount, prec)
            if inclusive_rate > 0:
                self.total_taxes_and_charges = round(self.net_total * inclusive_rate / 100.0 + actual_tax, prec)
                self.grand_total = round(self.net_total + self.total_taxes_and_charges, prec)
            else:
                self.total_taxes_and_charges = round(self.net_total * exclusive_rate / 100.0 + actual_tax, prec)
                self.grand_total = round(self.net_total + self.total_taxes_and_charges, prec)
        else:
            # Grand Total basis
            self.grand_total = round(pre_grand - self.discount_amount, prec)
            if inclusive_rate > 0:
                net_ex_actual = round((self.grand_total - actual_tax) / (1.0 + inclusive_rate / 100.0), prec)
                self.net_total = net_ex_actual
                self.total_taxes_and_charges = round(self.grand_total - self.net_total, prec)
            elif exclusive_rate > 0:
                net_ex_actual = round((self.grand_total - actual_tax) / (1.0 + exclusive_rate / 100.0), prec)
                self.net_total = net_ex_actual
                self.total_taxes_and_charges = round(self.grand_total - self.net_total, prec)
            else:
                self.net_total = round(self.grand_total - actual_tax, prec)
                self.total_taxes_and_charges = round(actual_tax, prec)

        self.rounded_total = self.grand_total
        self.events.append(("totals", self.additional_discount_percentage, self.discount_amount, self.grand_total))

    def set_paid_amount(self):
        self.paid_amount = round(sum(float(p.get("amount") or 0) for p in self.payments), self.precision("paid_amount"))

    def set_outstanding_amount(self):
        self.outstanding_amount = round(max(0.0, (self.rounded_total or self.grand_total) - self.paid_amount), self.precision("outstanding_amount"))

    def save(self, **kwargs):
        self.events.append(("save", self.additional_discount_percentage, self.discount_amount, self.grand_total))

    def insert(self, **kwargs):
        self.events.append(("insert", self.additional_discount_percentage, self.discount_amount, self.grand_total))

    def reload(self):
        pass

    def discard(self):
        self.docstatus = 2
        self.events.append(("discard",))

    def db_set(self, field, value, *args, **kwargs):
        setattr(self, field, value)

    def submit(self):
        self.docstatus = 1
        self.events.append(("submit",))


def _load_modules():
    framework, utils = _make_frappe_double()
    brand_mod = _stub(
        "ury.brand",
        coded_message=lambda key, msg="": f"[{key}] {msg}".strip(),
    )
    class _FakeTracker:
        def __init__(self, key, max_consecutive_login_attempts=5, lock_interval=900):
            self.key = key

        def is_user_allowed(self):
            return True

        def add_failure_attempt(self):
            pass

        def add_success_attempt(self):
            pass

    modules = {
        "frappe": framework,
        "frappe.auth": _stub("frappe.auth", LoginAttemptTracker=_FakeTracker),
        "frappe.utils": utils,
        "frappe.utils.password": _stub(
            "frappe.utils.password",
            check_password=MagicMock(),
            get_encryption_key=lambda: "test-encryption-key",
        ),
        "frappe.core": _stub("frappe.core"),
        "frappe.core.doctype": _stub("frappe.core.doctype"),
        "frappe.core.doctype.activity_log": _stub("frappe.core.doctype.activity_log"),
        "frappe.core.doctype.activity_log.activity_log": _stub(
            "frappe.core.doctype.activity_log.activity_log",
            add_authentication_log=MagicMock(),
        ),
        "frappe.core.doctype.user_permission": _stub("frappe.core.doctype.user_permission"),
        "frappe.core.doctype.user_permission.user_permission": _stub(
            "frappe.core.doctype.user_permission.user_permission",
            get_user_permissions=lambda *a, **k: {},
        ),
        "frappe.model.document": _stub("frappe.model.document", Document=object),
        "erpnext.controllers.queries": _stub("erpnext.controllers.queries", item_query=MagicMock()),
        "ury.brand": brand_mod,
        "ury.ury_pos.api": _stub(
            "ury.ury_pos.api",
            getBranch=MagicMock(return_value="Lakeside"),
            getBranchRoom=MagicMock(return_value=[]),
            getRoom=MagicMock(return_value=[]),
            posOpening=MagicMock(),
        ),
        "ury.ury_pos.default_customer": _stub(
            "ury.ury_pos.default_customer",
            get_default_customer_config=MagicMock(return_value={}),
            resolve_default_customer=MagicMock(),
        ),
        "ury.ury.api.ury_kot_generate": _stub(
            "ury.ury.api.ury_kot_generate",
            _execute_kot=MagicMock(),
            process_items_for_cancel_kot=MagicMock(),
        ),
        "ury.ury.api.ury_order_reservation_service": _stub(
            "ury.ury.api.ury_order_reservation_service",
            _line_key=MagicMock(),
            _line_ref=MagicMock(),
            reconcile_order_reservations=MagicMock(),
            release_order_reservations=MagicMock(),
            resolve_production_context=MagicMock(return_value=None),
            _warehouse_for_context=MagicMock(),
        ),
        "ury.ury.api.ury_stock_policy": _stub(
            "ury.ury.api.ury_stock_policy",
            get_branch_stock_policy=MagicMock(return_value=SimpleNamespace(reservation_control_enabled=False)),
        ),
        "ury.ury.doctype.alert_settings.alert_settings": _stub(
            "ury.ury.doctype.alert_settings.alert_settings",
            get_alert_rule=MagicMock(return_value=None),
        ),
        "ury.ury.api.ury_kot_notification": _stub(
            "ury.ury.api.ury_kot_notification",
            create_system_notification=MagicMock(),
            get_users_with_role=MagicMock(return_value=[]),
        ),
        "ury.ury.api.item_price": _stub(
            "ury.ury.api.item_price",
            item_price_release_states=MagicMock(),
            price_valid_on=MagicMock(),
        ),
        "ury.ury.doctype.staff_discount_policy.staff_discount_policy": _stub(
            "ury.ury.doctype.staff_discount_policy.staff_discount_policy",
            get_applicable_policy=MagicMock(return_value=None),
        ),
    }

    # Load override (or temporary stub) and discount modules if they exist yet (RED expects failure when absent)
    with patch.dict(sys.modules, modules):
        for stale in ("ury.ury_pos.discount", "ury.ury_pos._discount_override_stub", "ury.ury_pos.override"):
            sys.modules.pop(stale, None)
        for mod_name, rel_path in (
            ("ury.ury_pos.override", URY_PKG / "ury_pos/override.py"),
            ("ury.ury_pos._discount_override_stub", URY_PKG / "ury_pos/_discount_override_stub.py"),
        ):
            if rel_path.exists():
                mspec = importlib.util.spec_from_file_location(mod_name, rel_path)
                mmod = importlib.util.module_from_spec(mspec)
                sys.modules[mspec.name] = mmod
                mspec.loader.exec_module(mmod)
        discount_mod = None
        if DISCOUNT_PATH.exists():
            dspec = importlib.util.spec_from_file_location("ury.ury_pos.discount", DISCOUNT_PATH)
            discount_mod = importlib.util.module_from_spec(dspec)
            sys.modules[dspec.name] = discount_mod
            dspec.loader.exec_module(discount_mod)

        ospec = importlib.util.spec_from_file_location("ury.ury.doctype.ury_order.ury_order", ORDER_PATH)
        order_mod = importlib.util.module_from_spec(ospec)
        sys.modules[ospec.name] = order_mod
        ospec.loader.exec_module(order_mod)

        hspec = importlib.util.spec_from_file_location("ury.ury.hooks.ury_pos_invoice", HOOK_PATH)
        hook_mod = importlib.util.module_from_spec(hspec)
        sys.modules[hspec.name] = hook_mod
        hspec.loader.exec_module(hook_mod)

    return framework, order_mod, discount_mod, hook_mod


def _item(row_id, code, qty, rate, **extra):
    d = dict(
        name=row_id,
        item_code=code,
        item_name=code.title(),
        qty=qty,
        price_list_rate=rate,
        base_price_list_rate=rate,
        rate=rate,
        amount=qty * rate,
        discount_percentage=0.0,
        discount_amount=0.0,
        margin_type="",
        margin_rate_or_amount=0.0,
        pricing_rules=None,
        reservation_line_key=f"key-{row_id}",
        cost_center="Main - LH",
        uom="Nos",
        conversion_factor=1.0,
        warehouse="Stores - LH",
        ury_held=0,
    )
    d.update(extra)
    return Row(d)


class DiscountBase(unittest.TestCase):
    def setUp(self):
        self.frappe, self.order, self.discount, self.hook = _load_modules()
        override_mod = self.discount._get_override_module()
        self.enterContext(patch.object(override_mod, "_get_user_permissions", return_value={}))
        self.enterContext(patch.object(override_mod, "_add_authentication_log", MagicMock()))
        self.enterContext(
            patch.object(
                override_mod,
                "_get_login_attempt_tracker_cls",
                return_value=type(
                    "_T",
                    (),
                    {
                        "__init__": lambda self, *a, **k: None,
                        "is_user_allowed": lambda self: True,
                        "add_failure_attempt": lambda self: None,
                        "add_success_attempt": lambda self: None,
                    },
                ),
            )
        )
        self.frappe.db.sql.side_effect = self._db_sql
        self.pos_reasons = []
        self.roles_by_user = {
            "cashier@example.com": ["URY Cashier"],
            "manager@example.com": ["URY Manager"],
            "owner@example.com": ["Owner Approver"],
            "gm@example.com": ["URY Manager", "Owner Approver"],
            "waiter@example.com": ["URY Captain"],
            "Administrator": ["Administrator", "System Manager"],
            "sysman@example.com": ["System Manager"],
        }
        self.approval_limits = {}  # company -> dict(max_document_discount_percent, discount_limits_by_role, discount_approver_role)
        self.has_approval_limit_doctype = True
        self.has_discount_reason_doctype = True
        self.has_privilege_rule_doctype = False
        self.privilege_rules = []  # list[Row] matching B2a POS Privilege Rule schema
        self.auth_logs = []  # in-memory POS Authorisation Log rows for real ury.ury_pos.override
        self.consumed_token_hashes = set()
        self.reasons = {
            "Corporate rate": Row(name="Corporate rate", code="Corporate rate", description="Corporate rate", applies_to="POS", requires_supervisor=0, requires_note=0, sort_order=10, disabled=0),
            "Complaint": Row(name="Complaint", code="Complaint", description="Guest complaint", applies_to="Both", requires_supervisor=0, requires_note=0, sort_order=20, disabled=0),
            "VIP Comp": Row(name="VIP Comp", code="VIP Comp", description="VIP complimentary", applies_to="POS", requires_supervisor=1, requires_note=0, sort_order=25, disabled=0),
            "Staff": Row(name="Staff", code="Staff", description="Staff meal", applies_to="POS", requires_supervisor=0, requires_note=0, sort_order=30, disabled=0),
            "Sales only": Row(name="Sales only", code="Sales only", description="B2B agreement", applies_to="Sales documents", requires_supervisor=0, requires_note=0, sort_order=40, disabled=0),
            "Retired": Row(name="Retired", code="Retired", description="Old promo", applies_to="POS", requires_supervisor=0, requires_note=0, sort_order=50, disabled=1),
            "Other": Row(name="Other", code="Other", description="Other reason", applies_to="Both", requires_supervisor=0, requires_note=1, sort_order=60, disabled=0),
        }
        self.coupons = {}  # coupon_name -> Row
        self.pricing_rules = {}  # rule_name -> Row
        self.item_max_discount = {}  # item_code -> float
        self.invoice_fields = {
            "custom_discount_reason",
            "custom_discount_note",
            "custom_discount_approver",
            "custom_discount_approved_percent",
            "ury_discount_meta",
            "room_number",
            "coupon_code",
        }
        self.coupon_fields = {"custom_discount_reason"}
        self.profile = Row(
            name="Lakeside Till",
            company="Lakeside Hotel",
            branch="Lakeside",
            currency="UGX",
            customer="Walk-in",
            disabled=0,
            allow_discount_change=1,
            custom_enable_discount=1,
            apply_discount_on="Grand Total",
            ignore_pricing_rule=0,
            selling_price_list="Standard Selling",
            applicable_for_users=[
                Row(user="cashier@example.com"),
                Row(user="manager@example.com"),
                Row(user="owner@example.com"),
                Row(user="gm@example.com"),
            ],
        )
        self.docs = {}  # (doctype, name) -> doc

        self.frappe.get_roles.side_effect = lambda user=None: list(
            self.roles_by_user.get(user or self.frappe.session.user, [])
        )
        self.frappe.get_meta.side_effect = self._get_meta
        self.frappe.db.exists.side_effect = self._db_exists
        self.frappe.db.table_exists.side_effect = self._db_table_exists
        self.frappe.db.get_value.side_effect = self._db_get_value
        self.frappe.db.is_unique_key_violation.return_value = False
        self.frappe.get_cached_value.side_effect = self._get_cached_value
        self.frappe.get_cached_doc.side_effect = self._get_cached_doc
        self.frappe.get_doc.side_effect = self._get_doc
        self.frappe.get_all.side_effect = self._get_all
        self.frappe.get_value.side_effect = self._get_value
        self.frappe.has_permission.return_value = True
        self.frappe.get_hooks.side_effect = lambda hook=None, *a, **k: (
            ["test.pin_verifier"] if hook == "pos_override_pin_verifier" else []
        )
        self.frappe.get_attr.side_effect = lambda path: (
            (lambda user, pin, *a, **kw: pin == "4321") if path == "test.pin_verifier" else None
        )

    def _db_sql(self, query, params=None, as_dict=False):
        q = str(query or "").lower()
        if "count(*)" in q:
            return [[0]]
        if "max(" in q:
            return [[None]]
        return []

    def _db_table_exists(self, doctype):
        if doctype == "Approval Limit":
            return self.has_approval_limit_doctype
        if doctype == "Discount Reason":
            return self.has_discount_reason_doctype
        if doctype == "POS Privilege Rule":
            return True
        if doctype == "POS Reason":
            return bool(self.pos_reasons)
        return True

    def _get_meta(self, doctype):
        if doctype == "POS Invoice":
            return SimpleNamespace(has_field=lambda f: f in self.invoice_fields)
        if doctype == "Coupon Code":
            return SimpleNamespace(has_field=lambda f: f in self.coupon_fields)
        if doctype == "Approval Limit":
            return SimpleNamespace(has_field=lambda f: f in ("max_document_discount_percent", "discount_limits_by_role", "discount_approver_role"))
        if doctype == "POS Profile":
            return SimpleNamespace(has_field=lambda f: f in ("allow_discount_change", "custom_enable_discount", "apply_discount_on"))
        return SimpleNamespace(has_field=lambda f: False)

    def _db_exists(self, doctype, name=None):
        if doctype == "DocType":
            if name == "Approval Limit":
                return self.has_approval_limit_doctype
            if name == "Discount Reason":
                return self.has_discount_reason_doctype
            if name == "POS Privilege Rule":
                return self.has_privilege_rule_doctype
            return True
        if doctype == "Discount Reason":
            return name in self.reasons
        if doctype == "Coupon Code":
            return name in self.coupons
        if doctype == "Approval Limit":
            return self.has_approval_limit_doctype and name in self.approval_limits
        if doctype == "User":
            return name if isinstance(name, str) and name in self.roles_by_user else None
        if doctype == "POS Profile":
            return name if name == self.profile.name else None
        if doctype == "URY User":
            return True
        if doctype == "POS Privilege Rule":
            return any(all(r.get(k) == v for k, v in (name or {}).items()) for r in self.privilege_rules)
        return (doctype, name) in self.docs

    def _db_get_value(self, doctype, filters, fieldname=None, as_dict=False, **kw):
        if doctype == "Approval Limit":
            if not self.has_approval_limit_doctype:
                return None
            row = self.approval_limits.get(filters)
            if not row:
                return None
            if isinstance(fieldname, (list, tuple)):
                out = Row({f: row.get(f) for f in fieldname})
                return out if as_dict else tuple(row.get(f) for f in fieldname)
            return row.get(fieldname)
        if doctype == "Discount Reason":
            row = self.reasons.get(filters)
            if not row:
                return None
            if isinstance(fieldname, (list, tuple)):
                out = Row({f: row.get(f) for f in fieldname})
                return out if as_dict else tuple(row.get(f) for f in fieldname)
            return row.get(fieldname)
        if doctype == "Coupon Code":
            row = self.coupons.get(filters)
            if not row and isinstance(filters, dict):
                code = filters.get("coupon_code")
                row = next((c for c in self.coupons.values() if c.get("coupon_code") == code), None)
            if not row:
                return None
            if isinstance(fieldname, (list, tuple)):
                out = Row({f: row.get(f) for f in fieldname})
                return out if as_dict else tuple(row.get(f) for f in fieldname)
            return row.get(fieldname)
        if doctype == "Pricing Rule":
            row = self.pricing_rules.get(filters)
            if not row:
                return None
            if isinstance(fieldname, (list, tuple)):
                out = Row({f: row.get(f) for f in fieldname})
                return out if as_dict else tuple(row.get(f) for f in fieldname)
            return row.get(fieldname)
        if doctype == "Item":
            if fieldname == "max_discount":
                return self.item_max_discount.get(filters, 0)
            return None
        if doctype == "User":
            if isinstance(fieldname, (list, tuple)):
                data = {"name": filters, "enabled": 1, "user_type": "System User", "full_name": filters}
                return Row({f: data.get(f) for f in fieldname}) if as_dict else tuple(data.get(f) for f in fieldname)
            if fieldname == "enabled":
                return 1
            if fieldname == "user_type":
                return "System User"
            if fieldname == "full_name":
                return filters
            return 1
        if doctype == "POS Authorisation Log":
            match = None
            if isinstance(filters, dict):
                for entry in reversed(self.auth_logs):
                    if all(entry.get(k) == v for k, v in filters.items()):
                        match = entry
                        break
            if not match:
                return None
            if isinstance(fieldname, (list, tuple)):
                out = Row({f: match.get(f) for f in fieldname})
                return out if as_dict else tuple(match.get(f) for f in fieldname)
            return match.get(fieldname)
        if doctype == "POS Invoice":
            doc = self.docs.get(("POS Invoice", filters))
            if not doc:
                return None
            if fieldname == "docstatus":
                return doc.docstatus
            if isinstance(fieldname, (list, tuple)):
                out = Row({f: doc.get(f) for f in fieldname})
                return out if as_dict else tuple(doc.get(f) for f in fieldname)
            return doc.get(fieldname)
        return None

    def _get_cached_value(self, doctype, name, fieldname):
        return self._db_get_value(doctype, name, fieldname)

    def _get_value(self, doctype, name, fieldname=None, **kw):
        if doctype == "POS Invoice" and fieldname == "order_type":
            return "Dine In"
        return self._db_get_value(doctype, name, fieldname, **kw)

    def _get_cached_doc(self, doctype, name):
        if doctype == "POS Profile":
            return self.profile
        return self._get_doc(doctype, name)

    def _get_doc(self, doctype, name=None, **kw):
        if isinstance(doctype, dict):
            payload = dict(doctype)
            case = self

            class _LogDoc(Row):
                def __init__(self, d):
                    super().__init__(d)
                    self.flags = SimpleNamespace()

                def insert(self, ignore_permissions=False, **ikw):
                    if self.get("doctype") == "POS Authorisation Log":
                        cth = self.get("consumed_token_hash")
                        if cth:
                            if cth in case.consumed_token_hashes:
                                raise case.frappe.UniqueValidationError("Duplicate consumed_token_hash")
                            case.consumed_token_hashes.add(cth)
                        if not self.get("name"):
                            self["name"] = f"LOG-{len(case.auth_logs) + 1:04d}"
                        case.auth_logs.append(Row(self))
                    return self

            return _LogDoc(payload)
        if doctype == "POS Profile":
            return self.profile
        if doctype == "Coupon Code" and name in self.coupons:
            return self.coupons[name]
        if doctype == "Pricing Rule" and name in self.pricing_rules:
            return self.pricing_rules[name]
        if doctype == "Discount Reason" and name in self.reasons:
            return self.reasons[name]
        if (doctype, name) in self.docs:
            return self.docs[(doctype, name)]
        raise KeyError((doctype, name))

    def _get_all(self, doctype, filters=None, fields=None, order_by=None, pluck=None, **kw):
        filters = filters or {}
        if doctype == "Discount Reason":
            rows = list(self.reasons.values())
            if "disabled" in filters:
                rows = [r for r in rows if int(r.get("disabled") or 0) == int(filters["disabled"])]
            if "applies_to" in filters:
                cond = filters["applies_to"]
                if isinstance(cond, (list, tuple)) and cond[0] == "in":
                    rows = [r for r in rows if r.get("applies_to") in cond[1]]
            rows.sort(key=lambda r: (int(r.get("sort_order") or 0), r.get("code") or r.get("name") or ""))
            return [r.get(pluck) for r in rows] if pluck else rows
        if doctype == "Coupon Code":
            rows = list(self.coupons.values())
            return [r.get(pluck) for r in rows] if pluck else rows
        if doctype == "Pricing Rule":
            rows = list(self.pricing_rules.values())
            if "disable" in filters:
                rows = [r for r in rows if int(r.get("disable") or 0) == int(filters["disable"])]
            if "coupon_code_based" in filters:
                rows = [r for r in rows if int(r.get("coupon_code_based") or 0) == int(filters["coupon_code_based"])]
            return [r.get(pluck) for r in rows] if pluck else rows
        if doctype == "POS Privilege Rule":
            rows = list(self.privilege_rules)
            # When privilege_rules wasn't explicitly populated for override.py authorise() tests,
            # provide default manager authorise rules matching B2a seed-once defaults ONLY when
            # filtering by role (override._effective_rules_for_roles), not when discount.py probes company rules.
            if not rows and not self.has_privilege_rule_doctype and "role" in filters:
                rows = [
                    Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=0, max_amount=0, allow_self_approval=0, require_clocked_in=0, privilege_group=0),
                    Row(company="Lakeside Hotel", pos_profile="", privilege="complimentary", role="URY Manager", may_perform=0, may_authorise=1, max_percent=0, max_amount=0, allow_self_approval=0, require_clocked_in=0, privilege_group=0),
                    Row(company="Lakeside Hotel", pos_profile="", privilege="discount_remove", role="URY Manager", may_perform=1, may_authorise=1, max_percent=0, max_amount=0, allow_self_approval=0, require_clocked_in=0, privilege_group=0),
                ]
            for k, v in filters.items():
                if isinstance(v, (list, tuple)) and v[0] == "in":
                    rows = [r for r in rows if r.get(k) in v[1]]
                else:
                    rows = [r for r in rows if r.get(k) == v]
            return [r.get(pluck) for r in rows] if pluck else rows
        if doctype == "POS Reason":
            rows = list(self.pos_reasons)
            if "disabled" in filters:
                rows = [r for r in rows if int(r.get("disabled") or 0) == int(filters["disabled"])]
            return [r.get(pluck) for r in rows] if pluck else rows
        if doctype == "User":
            rows = [
                Row(name=u, full_name=u, enabled=1, user_type="System User")
                for u in sorted(self.roles_by_user.keys())
                if u not in ("Administrator", "Guest")
            ]
            return [r.get(pluck) for r in rows] if pluck else rows
        if doctype == "POS Invoice":
            out = []
            for (dt, _), doc in self.docs.items():
                if dt != "POS Invoice":
                    continue
                ok = True
                for k, v in filters.items():
                    if isinstance(v, (list, tuple)) and v[0] == "!=":
                        if doc.get(k) == v[1]:
                            ok = False
                    elif doc.get(k) != v:
                        ok = False
                if ok:
                    out.append(doc)
            return out
        return []

    def _register_check(self, check):
        self.docs[("POS Invoice", check.name)] = check
        return check

    def _patch_settlement_helpers(self, check):
        for helper in (
            "_validate_client_ids",
            "_lock_settlement_customer",
            "_client_order_invoice",
            "_check_held_before_settlement",
            "_refuse_client_order_rebind",
            "_set_settlement_customer",
            "_enforce_on_account",
            "_enforce_order_access",
            "_free_tables_if_no_open_invoices",
        ):
            self.enterContext(patch.object(self.order, helper, return_value=None))
        self.enterContext(patch.object(self.order, "get_order_invoice", return_value=check))

    def _issue_override_token(self, check, claim, approver="manager@example.com", percent=None, amount=None, privilege="discount"):
        """Mint a real single-use approval token through the override contract's public authorise()."""
        self.assertIsNotNone(self.discount, "ury.ury_pos.discount module does not exist yet")
        override_mod = self.discount._get_override_module()
        res = override_mod.authorise(
            pos_profile=self.profile.name,
            privilege=privilege,
            approver=approver,
            pin="4321",
            reference_doctype="POS Invoice",
            reference_name=check.name,
            claim=claim,
        )
        return res["token"]


class TestLegacyConstantsRemoved(DiscountBase):
    def test_legacy_discount_role_and_100_percent_constants_are_deleted(self):
        self.assertFalse(hasattr(self.order, "DISCOUNT_ALLOWED_ROLES"))
        self.assertFalse(hasattr(self.order, "MAX_DISCOUNT_PERCENTAGE"))
        self.assertFalse(hasattr(self.order, "_validate_additional_discount"))

    def test_old_client_sending_additionalDiscount_is_refused_never_old_100_percent(self):
        check = self._register_check(FakePOSInvoice("CHK-OLD", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)
        with self.assertRaises((Refused, NotPermitted)):
            self.order.make_invoice(
                customer="Walk-in",
                payments=[{"mode_of_payment": "Cash", "amount": 0}],
                cashier="cashier@example.com",
                pos_profile="Lakeside Till",
                owner="cashier@example.com",
                additionalDiscount=100,
                invoice="CHK-OLD",
                submit_invoice=0,
            )
        self.assertEqual(check.additional_discount_percentage, 0)
        self.assertFalse([e for e in check.events if e[0] == "save"])


class TestLimitsAndGovernance(DiscountBase):
    def test_blank_limit_is_zero_without_sales_discount_control_installed(self):
        self.has_approval_limit_doctype = False
        self.invoice_fields = {"room_number", "coupon_code", "ury_discount_meta"}
        check = self._register_check(FakePOSInvoice("CHK-NO-B1", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.order.make_invoice(
                customer="Walk-in",
                payments=[{"mode_of_payment": "Cash", "amount": 45000}],
                cashier="cashier@example.com",
                pos_profile="Lakeside Till",
                owner="cashier@example.com",
                invoice="CHK-NO-B1",
                submit_invoice=0,
                discount_percent=10,
                discount_reason="Complaint",
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

    def test_highest_role_line_wins_and_non_finite_ignored_and_admin_gets_no_free_pass(self):
        self.assertIsNotNone(self.discount, "ury.ury_pos.discount module does not exist yet")
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="",
            discount_limits_by_role=(
                "URY Cashier: 5\n"
                "URY Manager: 20\n"
                "Bad Line\n"
                "URY Cashier: nan\n"
                "URY Cashier: inf\n"
                "URY Cashier: -10\n"
                "URY Cashier: 150\n"
            ),
            discount_approver_role="Accounts Manager",
        )
        self.roles_by_user["dual@example.com"] = ["URY Cashier", "URY Manager"]
        self.assertEqual(self.discount.user_discount_limit("cashier@example.com", "Lakeside Hotel"), 5.0)
        self.assertEqual(self.discount.user_discount_limit("dual@example.com", "Lakeside Hotel"), 20.0)
        # Administrator and System Manager get NO free pass when not listed and base is blank
        self.assertEqual(self.discount.user_discount_limit("Administrator", "Lakeside Hotel"), 0.0)
        self.assertEqual(self.discount.user_discount_limit("sysman@example.com", "Lakeside Hotel"), 0.0)

    def test_within_limit_succeeds_without_approval_and_over_limit_requires_approval(self):
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 10\nURY Manager: 50",
            discount_approver_role="URY Manager",
        )
        check = self._register_check(FakePOSInvoice("CHK-LIM", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        # 10% within cashier limit succeeds without token
        res = self.order.apply_check_discount(
            invoice="CHK-LIM",
            pos_profile="Lakeside Till",
            discount_percent=10,
            discount_reason="Complaint",
        )
        self.assertEqual(res["after_total"], 45000)
        self.assertEqual(check.additional_discount_percentage, 10)
        self.assertEqual(check.custom_discount_reason, "Complaint")
        self.assertIsNone(getattr(check.flags, "discount_approval", None))

        # 15% over cashier limit fails without token
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.order.apply_check_discount(
                invoice="CHK-LIM",
                pos_profile="Lakeside Till",
                discount_percent=15,
                discount_reason="Complaint",
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

    def test_100_percent_comp_needs_second_person_even_for_manager_with_100_limit(self):
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Manager: 100",
            discount_approver_role="URY Manager",
        )
        self.frappe.session.user = "manager@example.com"
        check = self._register_check(FakePOSInvoice("CHK-COMP", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        preview = self.order.preview_settlement_totals(
            invoice="CHK-COMP",
            pos_profile="Lakeside Till",
            discount_percent=100,
            discount_reason="Complaint",
        )
        self.assertTrue(preview["needs_approval"])
        self.assertTrue(preview["is_comp"])
        self.assertEqual(preview.get("privilege"), "complimentary")

        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.order.apply_check_discount(
                invoice="CHK-COMP",
                pos_profile="Lakeside Till",
                discount_percent=100,
                discount_reason="Complaint",
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

        # A second person (another manager) CAN authorise it — MUST use privilege="complimentary", NOT "discount"
        self.roles_by_user["gm@example.com"] = ["URY Manager"]
        wrong_priv_token = self._issue_override_token(
            check, preview["claim"], approver="gm@example.com", percent=100, privilege="discount"
        )
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.order.apply_check_discount(
                invoice="CHK-COMP",
                pos_profile="Lakeside Till",
                discount_percent=100,
                discount_reason="Complaint",
                approval=wrong_priv_token,
            )
        self.assertIn("POS-AUTH-CHANGED", str(ctx.exception))

        token = self._issue_override_token(
            check, preview["claim"], approver="gm@example.com", percent=100, privilege="complimentary"
        )
        applied = self.order.apply_check_discount(
            invoice="CHK-COMP",
            pos_profile="Lakeside Till",
            discount_percent=100,
            discount_reason="Complaint",
            approval=token,
        )
        self.assertEqual(applied["after_total"], 0)
        self.assertEqual(check.flags.discount_approval["approver"], "gm@example.com")
        self.assertEqual(check.flags.discount_approval["percent"], 100)


class TestReasonsPresetsAndFields(DiscountBase):
    def setUp(self):
        super().setUp()
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 20\nURY Manager: 80",
            discount_approver_role="URY Manager",
        )

    def test_no_reason_or_inactive_or_non_pos_reason_refused(self):
        check = self._register_check(FakePOSInvoice("CHK-R", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        for bad_reason in (None, "", "   ", "Retired", "Sales only", "Nonexistent"):
            with self.subTest(reason=bad_reason):
                with self.assertRaises(Refused):
                    self.order.apply_check_discount(
                        invoice="CHK-R",
                        pos_profile="Lakeside Till",
                        discount_percent=10,
                        discount_reason=bad_reason,
                    )

    def test_reason_requiring_note_refuses_blank_note_and_accepts_note(self):
        check = self._register_check(FakePOSInvoice("CHK-NOTE", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        with self.assertRaises(Refused):
            self.order.apply_check_discount(
                invoice="CHK-NOTE",
                pos_profile="Lakeside Till",
                discount_percent=10,
                discount_reason="Other",
                discount_note="   ",
            )

        res = self.order.apply_check_discount(
            invoice="CHK-NOTE",
            pos_profile="Lakeside Till",
            discount_percent=10,
            discount_reason="Other",
            discount_note="Long wait for mains",
        )
        self.assertEqual(res["after_total"], 45000)
        self.assertEqual(check.custom_discount_reason, "Other")
        self.assertEqual(check.custom_discount_note, "Long wait for mains")

    def test_reason_requiring_supervisor_needs_approval_even_within_cashier_limit(self):
        check = self._register_check(FakePOSInvoice("CHK-SUP-R", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        preview = self.order.preview_settlement_totals(
            invoice="CHK-SUP-R",
            pos_profile="Lakeside Till",
            discount_percent=5,
            discount_reason="VIP Comp",
        )
        self.assertTrue(preview["needs_approval"])
        self.assertIn("reason_requires_supervisor", preview["approval_reasons"])

        with self.assertRaises((Refused, NotPermitted)):
            self.order.apply_check_discount(
                invoice="CHK-SUP-R",
                pos_profile="Lakeside Till",
                discount_percent=5,
                discount_reason="VIP Comp",
            )

    def test_flat_amount_reaches_discount_amount_and_open_gated_by_allow_discount_change(self):
        check = self._register_check(FakePOSInvoice("CHK-FLAT", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        self.profile.allow_discount_change = 0
        with self.assertRaises((Refused, NotPermitted)):
            self.order.apply_check_discount(
                invoice="CHK-FLAT",
                pos_profile="Lakeside Till",
                discount_amount=5000,
                discount_reason="Complaint",
            )

        self.profile.allow_discount_change = 1
        res = self.order.apply_check_discount(
            invoice="CHK-FLAT",
            pos_profile="Lakeside Till",
            discount_amount=5000,
            discount_reason="Complaint",
        )
        self.assertEqual(check.additional_discount_percentage, 0)
        self.assertEqual(check.discount_amount, 5000)
        self.assertEqual(res["before_total"], 50000)
        self.assertEqual(res["discount_total"], 5000)
        self.assertEqual(res["after_total"], 45000)

    def test_coupon_preset_sets_coupon_code_auto_fills_reason_and_works_when_open_disabled(self):
        self.profile.allow_discount_change = 0
        self.pricing_rules["PR-HAPPY15"] = Row(
            name="PR-HAPPY15",
            title="Happy Hour 15%",
            apply_on="Transaction",
            selling=1,
            disable=0,
            coupon_code_based=1,
            price_or_product_discount="Price",
            rate_or_discount="Discount Percentage",
            discount_percentage=15.0,
            discount_amount=0.0,
            apply_discount_on="Grand Total",
            company="Lakeside Hotel",
            valid_from="2026-01-01",
            valid_upto="2026-12-31",
        )
        self.coupons["Happy Hour 15"] = Row(
            name="Happy Hour 15",
            coupon_name="Happy Hour 15",
            coupon_code="HAPPY15",
            coupon_type="Promotional",
            pricing_rule="PR-HAPPY15",
            custom_discount_reason="Corporate rate",
            valid_from="2026-01-01",
            valid_upto="2026-12-31",
            maximum_use=0,
            used=0,
        )
        check = self._register_check(FakePOSInvoice("CHK-COUP", items=[_item("r1", "FISH", 1, 40000)]))
        self._patch_settlement_helpers(check)

        res = self.order.apply_check_discount(
            invoice="CHK-COUP",
            pos_profile="Lakeside Till",
            coupon_code="Happy Hour 15",
        )
        self.assertEqual(check.coupon_code, "Happy Hour 15")
        self.assertEqual(check.custom_discount_reason, "Corporate rate")
        self.assertEqual(check.additional_discount_percentage, 15.0)
        self.assertEqual(res["discount_total"], 6000)
        self.assertEqual(res["after_total"], 34000)

    def test_line_discounts_reach_rows_capped_by_item_max_discount_and_combine_with_check_discount(self):
        self.item_max_discount["STEAK"] = 15.0
        check = self._register_check(
            FakePOSInvoice(
                "CHK-LINE",
                items=[
                    _item("r1", "STEAK", 1, 40000),
                    _item("r2", "SODA", 2, 5000),
                ],
            )
        )
        self._patch_settlement_helpers(check)

        # Exceeding Item.max_discount is refused cleanly
        with self.assertRaises(Refused):
            self.order.apply_check_discount(
                invoice="CHK-LINE",
                pos_profile="Lakeside Till",
                line_discounts=[{"row": "r1", "percent": 20}],
                discount_reason="Complaint",
            )

        # Line discount 10% on STEAK (4,000) + Check flat discount 2,000 = 6,000 off 50,000 (12%, within 20% limit)
        res = self.order.apply_check_discount(
            invoice="CHK-LINE",
            pos_profile="Lakeside Till",
            line_discounts=[{"row": "r1", "percent": 10}],
            discount_amount=2000,
            discount_reason="Complaint",
        )
        self.assertEqual(check.items[0].discount_percentage, 10)
        self.assertEqual(check.items[0].rate, 36000)
        self.assertEqual(check.discount_amount, 2000)
        self.assertEqual(res["before_total"], 50000)
        self.assertEqual(res["discount_total"], 6000)
        self.assertEqual(res["after_total"], 44000)
        self.assertAlmostEqual(res["effective_percent"], 12.0, places=2)


class TestTokenLifecyclePartialTenderAndInputs(DiscountBase):
    def setUp(self):
        super().setUp()
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 80",
            discount_approver_role="URY Manager",
        )

    def test_token_single_use_and_changed_after_approval_refused(self):
        check = self._register_check(FakePOSInvoice("CHK-TOK", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        preview = self.order.preview_settlement_totals(
            invoice="CHK-TOK",
            pos_profile="Lakeside Till",
            discount_percent=25,
            discount_reason="Complaint",
        )
        token = self._issue_override_token(check, preview["claim"], approver="manager@example.com", percent=25)

        # Changed after approval (30% instead of 25%) MUST refuse with POS-AUTH-CHANGED
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.order.apply_check_discount(
                invoice="CHK-TOK",
                pos_profile="Lakeside Till",
                discount_percent=30,
                discount_reason="Complaint",
                approval=token,
            )
        self.assertIn("POS-AUTH-CHANGED", str(ctx.exception))

        # Exact approved claim succeeds once
        res = self.order.apply_check_discount(
            invoice="CHK-TOK",
            pos_profile="Lakeside Till",
            discount_percent=25,
            discount_reason="Complaint",
            approval=token,
        )
        self.assertEqual(res["after_total"], 37500)
        self.assertEqual(check.flags.discount_approval["approver"], "manager@example.com")

        # Replaying the same token is refused (single-use)
        with self.assertRaises((Refused, NotPermitted)):
            self.order.apply_check_discount(
                invoice="CHK-TOK",
                pos_profile="Lakeside Till",
                discount_percent=25,
                discount_reason="Complaint",
                approval=token,
            )

    def test_removed_then_re_added_requires_fresh_approval(self):
        check = self._register_check(FakePOSInvoice("CHK-REM", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        preview = self.order.preview_settlement_totals(
            invoice="CHK-REM",
            pos_profile="Lakeside Till",
            discount_percent=25,
            discount_reason="Complaint",
        )
        token = self._issue_override_token(check, preview["claim"], approver="manager@example.com", percent=25)
        self.order.apply_check_discount(
            invoice="CHK-REM",
            pos_profile="Lakeside Till",
            discount_percent=25,
            discount_reason="Complaint",
            approval=token,
        )
        self.assertEqual(check.additional_discount_percentage, 25)
        self.assertEqual(check.custom_discount_approver, "manager@example.com")

        # Remove before settlement clears discount AND approval metadata
        removed = self.order.remove_check_discount(invoice="CHK-REM", pos_profile="Lakeside Till")
        self.assertEqual(removed["after_total"], 50000)
        self.assertEqual(check.additional_discount_percentage, 0)
        self.assertEqual(check.discount_amount, 0)
        self.assertFalse(check.custom_discount_reason)
        self.assertFalse(check.custom_discount_approver)

        # Re-adding 25% without a fresh approval MUST fail!
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.order.apply_check_discount(
                invoice="CHK-REM",
                pos_profile="Lakeside Till",
                discount_percent=25,
                discount_reason="Complaint",
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

    def test_discount_after_partial_tender_allowed_above_tender_and_refused_below_tender(self):
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 80",
            discount_approver_role="URY Manager",
        )
        check = self._register_check(
            FakePOSInvoice(
                "CHK-PART",
                items=[_item("r1", "FISH", 1, 100000)],
                payments=[Row(mode_of_payment="Cash", amount=40000, custom_tender_received=1)],
            )
        )
        self._patch_settlement_helpers(check)

        # 10% off -> 90,000 >= 40,000 received tender -> allowed
        ok = self.order.apply_check_discount(
            invoice="CHK-PART",
            pos_profile="Lakeside Till",
            discount_percent=10,
            discount_reason="Complaint",
        )
        self.assertEqual(ok["after_total"], 90000)

        # 70% off -> 30,000 < 40,000 received tender -> refused
        with self.assertRaises(Refused) as ctx:
            self.order.apply_check_discount(
                invoice="CHK-PART",
                pos_profile="Lakeside Till",
                discount_percent=70,
                discount_reason="Complaint",
            )
        self.assertIn("POS-DISC-BELOW-TENDER", str(ctx.exception))

    def test_negative_nan_inf_huge_inputs_refused(self):
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 50",
            discount_approver_role="URY Manager",
        )
        check = self._register_check(FakePOSInvoice("CHK-BAD", items=[_item("r1", "FISH", 1, 20000)]))
        self._patch_settlement_helpers(check)

        for kwargs in (
            {"discount_percent": -5},
            {"discount_percent": float("nan")},
            {"discount_percent": float("inf")},
            {"discount_percent": "NaN"},
            {"discount_percent": 101},
            {"discount_percent": 999999},
            {"discount_amount": -100},
            {"discount_amount": float("nan")},
            {"discount_amount": float("inf")},
            {"discount_amount": 25000},  # > 20000 check total
            {"discount_percent": 10, "discount_amount": 1000},  # both header modes
            {"line_discounts": [{"row": "r1", "percent": -10}]},
            {"line_discounts": [{"row": "r1", "amount": 999999}]},
            {"line_discounts": [{"row": "nonexistent", "percent": 10}]},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(Refused):
                    self.order.preview_settlement_totals(
                        invoice="CHK-BAD",
                        pos_profile="Lakeside Till",
                        discount_reason="Complaint",
                        **kwargs,
                    )


class TestRoundingAndTaxPreview(DiscountBase):
    def test_zero_decimal_ugx_and_inclusive_vs_exclusive_tax_preview(self):
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 20",
            discount_approver_role="URY Manager",
        )
        # UGX (0 decimals), 18% VAT inclusive
        ugx_inc = self._register_check(
            FakePOSInvoice(
                "CHK-UGX-INC",
                currency="UGX",
                items=[_item("r1", "PLATTER", 1, 37000)],
                taxes=[Row(description="VAT 18%", rate=18.0, included_in_print_rate=1)],
            )
        )
        self._patch_settlement_helpers(ugx_inc)
        p_inc = self.order.preview_settlement_totals(
            invoice="CHK-UGX-INC",
            pos_profile="Lakeside Till",
            discount_amount=5000,
            discount_reason="Complaint",
        )
        self.assertEqual(p_inc["currency"], "UGX")
        self.assertEqual(p_inc["precision"], 0)
        self.assertEqual(p_inc["before_total"], 37000)
        self.assertEqual(p_inc["discount_total"], 5000)
        self.assertEqual(p_inc["after_total"], 32000)
        self.assertEqual(p_inc["tax_total"], round(32000 - round(32000 / 1.18, 0), 0))
        self.assertIsInstance(p_inc["after_total"], (int, float))
        self.assertEqual(p_inc["after_total"], round(p_inc["after_total"], 0))

        # USD (2 decimals), 18% VAT exclusive
        self.profile.currency = "USD"
        usd_exc = self._register_check(
            FakePOSInvoice(
                "CHK-USD-EXC",
                currency="USD",
                items=[_item("r1", "STEAK", 1, 100.00)],
                taxes=[Row(description="VAT 18%", rate=18.0, included_in_print_rate=0)],
            )
        )
        with patch.object(self.order, "get_order_invoice", return_value=usd_exc):
            p_exc = self.order.preview_settlement_totals(
                invoice="CHK-USD-EXC",
                pos_profile="Lakeside Till",
                discount_percent=10,
                discount_reason="Complaint",
            )
        self.assertEqual(p_exc["currency"], "USD")
        self.assertEqual(p_exc["precision"], 2)
        self.assertEqual(p_exc["before_total"], 118.00)
        self.assertEqual(p_exc["discount_total"], 11.80)
        self.assertEqual(p_exc["after_total"], 106.20)
        self.assertEqual(p_exc["tax_total"], 16.20)


class TestRestStraightToSaveAndReturnProration(DiscountBase):
    def test_rest_straight_to_save_without_flag_refused_even_without_b1(self):
        self.has_approval_limit_doctype = False
        self.invoice_fields = {"room_number", "coupon_code", "ury_discount_meta"}
        self.frappe.request = SimpleNamespace(path="/api/resource/POS Invoice/CHK-REST")
        prev = FakePOSInvoice("CHK-REST", items=[_item("r1", "FISH", 1, 50000)])
        doc = FakePOSInvoice("CHK-REST", items=[_item("r1", "FISH", 1, 50000)])
        doc._doc_before_save = prev
        doc.additional_discount_percentage = 30
        doc.calculate_taxes_and_totals()

        self.assertTrue(hasattr(self.hook, "validate_pos_discount"), "validate_pos_discount hook missing")
        with self.assertRaises((Refused, NotPermitted)):
            self.hook.validate_pos_discount(doc)

    def test_rest_cannot_forge_approver_or_raise_approved_discount(self):
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 80",
            discount_approver_role="URY Manager",
        )
        # 1) Forging custom_discount_approver directly on save
        prev = FakePOSInvoice("CHK-FORGE", items=[_item("r1", "FISH", 1, 50000)])
        doc = FakePOSInvoice("CHK-FORGE", items=[_item("r1", "FISH", 1, 50000)])
        doc._doc_before_save = prev
        doc.additional_discount_percentage = 20
        doc.custom_discount_reason = "Complaint"
        doc.custom_discount_approver = "manager@example.com"
        doc.custom_discount_approved_percent = 20
        doc.calculate_taxes_and_totals()
        with self.assertRaises((Refused, NotPermitted)):
            self.hook.validate_pos_discount(doc)

        # 2) Previously approved at 20%, later save tries to raise to 35% without fresh flag
        prev_ok = FakePOSInvoice("CHK-RAISE", items=[_item("r1", "FISH", 1, 50000)])
        prev_ok.additional_discount_percentage = 20
        prev_ok.custom_discount_reason = "Complaint"
        prev_ok.custom_discount_approver = "manager@example.com"
        prev_ok.custom_discount_approved_percent = 20
        prev_ok.calculate_taxes_and_totals()

        raised = FakePOSInvoice("CHK-RAISE", items=[_item("r1", "FISH", 1, 50000)])
        raised._doc_before_save = prev_ok
        raised.additional_discount_percentage = 35
        raised.custom_discount_reason = "Complaint"
        raised.custom_discount_approver = "manager@example.com"
        raised.custom_discount_approved_percent = 20
        raised.calculate_taxes_and_totals()
        with self.assertRaises((Refused, NotPermitted)):
            self.hook.validate_pos_discount(raised)

        # 3) Unchanged already-approved save (e.g. settle / print) succeeds
        same = FakePOSInvoice("CHK-RAISE", items=[_item("r1", "FISH", 1, 50000)])
        same._doc_before_save = prev_ok
        same.additional_discount_percentage = 20
        same.custom_discount_reason = "Complaint"
        same.custom_discount_approver = "manager@example.com"
        same.custom_discount_approved_percent = 20
        same.calculate_taxes_and_totals()
        self.hook.validate_pos_discount(same)  # must not raise

    def test_partial_return_prorates_flat_header_discount_as_sap_copy_control(self):
        self.assertTrue(hasattr(self.hook, "prorate_return_discount"), "prorate_return_discount hook missing")
        source = self._register_check(
            FakePOSInvoice(
                "CHK-ORIG",
                currency="UGX",
                items=[
                    _item("r1", "FISH", 2, 10000),
                    _item("r2", "SODA", 1, 10000),
                ],
                docstatus=1,
            )
        )
        source.additional_discount_percentage = 0
        source.discount_amount = 6000  # flat 6,000 off 30,000
        source.calculate_taxes_and_totals()

        # Partial return #1: 1x FISH (10,000 / 30,000 = 1/3 -> -2,000 discount)
        ret1 = FakePOSInvoice(
            "CHK-RET-1",
            currency="UGX",
            is_return=1,
            return_against="CHK-ORIG",
            items=[_item("rr1", "FISH", -1, 10000)],
        )
        ret1.additional_discount_percentage = 0
        ret1.discount_amount = -6000  # what native make_sales_return copies!
        self.hook.prorate_return_discount(ret1)
        ret1.calculate_taxes_and_totals()
        self.assertEqual(ret1.discount_amount, -2000)
        self.assertEqual(ret1.grand_total, -8000)

        # Mark ret1 submitted, then return the remaining 2 items (20,000 -> -4,000 exact remainder)
        ret1.docstatus = 1
        self._register_check(ret1)

        ret2 = FakePOSInvoice(
            "CHK-RET-2",
            currency="UGX",
            is_return=1,
            return_against="CHK-ORIG",
            items=[_item("rr2", "FISH", -1, 10000), _item("rr3", "SODA", -1, 10000)],
        )
        ret2.additional_discount_percentage = 0
        ret2.discount_amount = -6000
        self.hook.prorate_return_discount(ret2)
        ret2.calculate_taxes_and_totals()
        self.assertEqual(ret2.discount_amount, -4000)
        self.assertEqual(ret2.grand_total, -16000)


class TestSplitMergeAndOptions(DiscountBase):
    def test_split_bill_preserves_line_discount_and_prorates_flat_header_discount(self):
        self.enterContext(patch.object(self.order, "_enforce_order_access"))
        self.enterContext(patch.object(self.order, "_split_kots_to_move", return_value=[]))
        self.frappe.generate_hash.return_value = "GRP-DISC-1"

        source = self._register_check(
            FakePOSInvoice(
                "CHK-SP-1",
                currency="UGX",
                items=[
                    _item("i1", "FISH", 1, 27000, price_list_rate=30000, base_price_list_rate=30000, discount_percentage=10, discount_amount=3000, amount=27000),
                    _item("i2", "STEAK", 1, 20000),
                ],
                custom_discount_reason="Complaint",
                custom_discount_approver="manager@example.com",
                custom_discount_approved_percent=20,
            )
        )
        source.discount_amount = 4700  # flat header discount on 27,000 + 20,000 = 47,000 (10%)
        source.calculate_taxes_and_totals()

        child = FakePOSInvoice("CHK-SP-CHILD", currency="UGX", items=[])
        self.frappe.new_doc.return_value = child

        self.order.split_bill("CHK-SP-1", [{"name": "i1", "qty": 1}])

        # Moved line kept its 10% line discount
        self.assertEqual(child.items[0].discount_percentage, 10)
        self.assertEqual(child.items[0].discount_amount, 3000)
        # Flat header discount prorated cleanly: 27,000/47,000 * 4700 = 2700 on child, 2000 on source
        self.assertEqual(child.discount_amount, 2700)
        self.assertEqual(source.discount_amount, 2000)
        self.assertEqual(child.discount_amount + source.discount_amount, 4700)
        self.assertEqual(child.custom_discount_reason, "Complaint")
        self.assertEqual(child.custom_discount_approver, "manager@example.com")

    def test_consolidate_checks_carries_ury_discount_meta_without_b1_fields_and_passes_validate(self):
        """Real bench bug: merging a discounted check without B1 custom_discount_* fields
        must carry reason/approver in ury_discount_meta and pass validate_pos_discount on save
        and on subsequent settlement/print saves."""
        self.enterContext(patch.object(self.order, "get_branch_stock_policy", return_value=Row(reservation_control_enabled=False)))
        self.enterContext(patch.object(self.order, "_free_tables_if_no_open_invoices"))

        # Simulate a site WITHOUT siteops sales-discount-control@3 (only ury_discount_meta exists)
        orig_get_meta = self.frappe.get_meta
        def _meta_without_b1(dt):
            if dt == "POS Invoice":
                return Row(has_field=lambda f: f == "ury_discount_meta")
            return orig_get_meta(dt)
        self.frappe.get_meta.side_effect = _meta_without_b1

        src = self._register_check(
            FakePOSInvoice(
                "CHK-M-SRC",
                currency="UGX",
                items=[_item("s1", "FISH", 1, 20000)],
                ury_discount_meta=json.dumps({
                    "reason": "Complaint",
                    "reason_description": "Complaint",
                    "note": "Delayed dish",
                    "approver": "manager@example.com",
                    "approved_percent": 20.0,
                    "line_reasons": {},
                }),
            )
        )
        src.discount_amount = 4000
        src.calculate_taxes_and_totals()

        dst = self._register_check(
            FakePOSInvoice("CHK-M-DST", currency="UGX", items=[_item("d1", "STEAK", 1, 30000)])
        )
        dst.calculate_taxes_and_totals()
        dst.set_paid_amount = lambda: None
        dst.set_outstanding_amount = lambda: None
        src.discard = lambda: None
        src.db_set = lambda *a, **kw: None
        # Add as_dict on source items to match Frappe Document rows
        for row in src.items:
            row.as_dict = lambda _r=row, **kw: dict(_r)
        # Real Frappe runs validate_pos_discount during destination.save()
        def _save_with_validate():
            self.hook.validate_pos_discount(dst)
        dst.save = _save_with_validate

        self.order.consolidate_checks(dst, [src])

        self.assertEqual(dst.discount_amount, 4000)
        carried = self.discount._read_meta(dst)
        self.assertEqual(carried.get("reason"), "Complaint")
        self.assertEqual(carried.get("note"), "Delayed dish")
        self.assertEqual(carried.get("approver"), "manager@example.com")
        self.assertEqual(carried.get("approved_percent"), 20.0)

        # Subsequent save (e.g. printing or settling after merge) must also pass validate_pos_discount
        prev_snapshot = FakePOSInvoice(
            "CHK-M-DST",
            currency="UGX",
            items=[_item("d1", "STEAK", 1, 30000), _item("s1", "FISH", 1, 20000)],
            ury_discount_meta=dst.ury_discount_meta,
        )
        prev_snapshot.discount_amount = 4000
        prev_snapshot.calculate_taxes_and_totals()
        dst._doc_before_save = prev_snapshot
        self.hook.validate_pos_discount(dst)

    def test_list_discount_options_orders_reasons_by_sort_order_and_filters_presets(self):
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 10",
            discount_approver_role="URY Manager",
        )
        self.pricing_rules["PR-OK"] = Row(
            name="PR-OK",
            title="10% Lunch Promo",
            apply_on="Transaction",
            selling=1,
            disable=0,
            coupon_code_based=1,
            price_or_product_discount="Price",
            discount_percentage=10.0,
            discount_amount=0.0,
            company="Lakeside Hotel",
            valid_from="2026-01-01",
            valid_upto="2026-12-31",
        )
        self.pricing_rules["PR-OTHER-CO"] = Row(
            name="PR-OTHER-CO",
            title="Other Co Promo",
            apply_on="Transaction",
            selling=1,
            disable=0,
            coupon_code_based=1,
            price_or_product_discount="Price",
            discount_percentage=10.0,
            discount_amount=0.0,
            company="Other Hotel",
        )
        self.coupons["LUNCH10"] = Row(
            name="LUNCH10",
            coupon_name="LUNCH10",
            coupon_code="LUNCH10",
            coupon_type="Promotional",
            pricing_rule="PR-OK",
            custom_discount_reason="Corporate rate",
            valid_from="2026-01-01",
            valid_upto="2026-12-31",
            maximum_use=0,
            used=0,
        )
        self.coupons["EXHAUSTED"] = Row(
            name="EXHAUSTED",
            coupon_name="EXHAUSTED",
            coupon_code="USEDUP",
            coupon_type="Promotional",
            pricing_rule="PR-OK",
            maximum_use=5,
            used=5,
        )
        self.coupons["OTHERCO"] = Row(
            name="OTHERCO",
            coupon_name="OTHERCO",
            coupon_code="OTHERCO",
            coupon_type="Promotional",
            pricing_rule="PR-OTHER-CO",
            maximum_use=0,
            used=0,
        )

        opts = self.order.list_discount_options(pos_profile="Lakeside Till")
        self.assertEqual([r["code"] for r in opts["reasons"]], ["Corporate rate", "Complaint", "VIP Comp", "Staff", "Other"])
        self.assertEqual([p["name"] for p in opts["presets"]], ["LUNCH10"])
        self.assertEqual(opts["presets"][0]["discount_reason"], "Corporate rate")
        self.assertTrue(opts["allow_discount_change"])
        self.assertEqual(opts["limit_percent"], 10.0)


class TestAmendment1PrivilegesAndRuleMin(DiscountBase):
    """Amendment 1 compliance:
    - privilege codes: discount, discount_remove, complimentary
    - min(Approval Limit, POS Privilege Rule) wins; narrower pos_profile rule overrides company-wide
    - No rule for a role when rules exist = 0% alone
    - Server gate at the mutation itself enforces the same rule for REST, Desk, offline replay, old clients
    """

    def test_min_of_approval_limit_and_pos_privilege_rule_wins_with_narrower_profile(self):
        self.has_privilege_rule_doctype = True
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 25\nURY Captain: 15",
            discount_approver_role="URY Manager",
        )
        self.privilege_rules = [
            # Company-wide rules: Cashier max 50% (Approval Limit 5% wins!), Manager max 0 (defers to Approval Limit 25%)
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Cashier", may_perform=1, may_authorise=0, max_percent=50),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=0),
            # Narrower profile rule tightens Manager on Lakeside Till to 10% -> min(25, 10) = 10%
            Row(company="Lakeside Hotel", pos_profile="Lakeside Till", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=10),
        ]
        # Cashier: min(Approval Limit 5, rule 50) = 5%
        self.assertEqual(self.discount.user_discount_limit("cashier@example.com", "Lakeside Hotel", "Lakeside Till"), 5.0)
        # Manager: min(Approval Limit 25, narrow profile rule 10) = 10%
        self.assertEqual(self.discount.user_discount_limit("manager@example.com", "Lakeside Hotel", "Lakeside Till"), 10.0)
        # Waiter (URY Captain): has Approval Limit 15% but NO POS Privilege Rule -> 0% alone!
        self.assertEqual(self.discount.user_discount_limit("waiter@example.com", "Lakeside Hotel", "Lakeside Till"), 0.0)

        # Narrower profile rule with may_perform=0 drops Cashier to 0% even though Approval Limit says 5%
        self.privilege_rules.append(
            Row(company="Lakeside Hotel", pos_profile="Lakeside Till", privilege="discount", role="URY Cashier", may_perform=0, may_authorise=0, max_percent=50)
        )
        self.assertEqual(self.discount.user_discount_limit("cashier@example.com", "Lakeside Hotel", "Lakeside Till"), 0.0)

        # REST save (validate_pos_discount) MUST enforce the exact same min(Approval Limit, POS Privilege Rule)!
        self.frappe.session.user = "manager@example.com"
        check = self._register_check(
            FakePOSInvoice(
                "CHK-AM1-REST",
                items=[_item("r1", "FISH", 1, 100000)],
                pos_profile="Lakeside Till",
            )
        )
        check.additional_discount_percentage = 15  # <= Approval Limit 25%, but > narrow profile rule 10%!
        check.custom_discount_reason = "Complaint"
        check.calculate_taxes_and_totals()
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.hook.validate_pos_discount(check)
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

    def test_discount_remove_privilege_enforced_with_rules_and_on_printed_check(self):
        self.has_privilege_rule_doctype = True
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 20\nURY Manager: 80",
            discount_approver_role="URY Manager",
        )
        self.privilege_rules = [
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Cashier", may_perform=1, may_authorise=0, max_percent=20),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=80),
            # Only URY Manager may perform or authorise discount_remove
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount_remove", role="URY Manager", may_perform=1, may_authorise=1, max_percent=0),
        ]
        check = self._register_check(FakePOSInvoice("CHK-AM1-REM", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        # Cashier applies 10% within limit
        self.order.apply_check_discount(
            invoice="CHK-AM1-REM",
            pos_profile="Lakeside Till",
            discount_percent=10,
            discount_reason="Complaint",
        )
        self.assertEqual(check.additional_discount_percentage, 10)

        opts_cashier = self.order.list_discount_options(pos_profile="Lakeside Till")
        self.assertFalse(opts_cashier.get("can_remove_discount"))

        # Cashier cannot remove without discount_remove manager approval
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.order.remove_check_discount(invoice="CHK-AM1-REM", pos_profile="Lakeside Till")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))
        self.assertEqual(check.additional_discount_percentage, 10)

        # Manager token for discount_remove succeeds
        remove_claim = self.discount.canonical_remove_claim(check, row=None)
        token = self._issue_override_token(
            check, remove_claim, approver="manager@example.com", privilege="discount_remove"
        )
        res = self.order.remove_check_discount(
            invoice="CHK-AM1-REM", pos_profile="Lakeside Till", approval=token
        )
        self.assertEqual(res["after_total"], 50000)
        self.assertEqual(check.additional_discount_percentage, 0)

        # Without POS Privilege Rule installed: printed check removal still requires discount_remove approval for Cashier
        self.has_privilege_rule_doctype = False
        self.privilege_rules = []
        self.order.apply_check_discount(
            invoice="CHK-AM1-REM",
            pos_profile="Lakeside Till",
            discount_percent=10,
            discount_reason="Complaint",
        )
        check.invoice_printed = 1
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            self.order.remove_check_discount(invoice="CHK-AM1-REM", pos_profile="Lakeside Till")
        self.assertIn("POS-AUTH-REQUIRED", str(ctx.exception))

        printed_claim = self.discount.canonical_remove_claim(check, row=None)
        token2 = self._issue_override_token(
            check, printed_claim, approver="manager@example.com", privilege="discount_remove"
        )
        res2 = self.order.remove_check_discount(
            invoice="CHK-AM1-REM", pos_profile="Lakeside Till", approval=token2
        )
        self.assertEqual(res2["after_total"], 50000)

    def test_preview_reports_privilege_discount_vs_complimentary(self):
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5",
            discount_approver_role="URY Manager",
        )
        check = self._register_check(FakePOSInvoice("CHK-AM1-PRIV", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        p20 = self.order.preview_settlement_totals(
            invoice="CHK-AM1-PRIV",
            pos_profile="Lakeside Till",
            discount_percent=20,
            discount_reason="Complaint",
        )
        self.assertEqual(p20.get("privilege"), "discount")

        p100 = self.order.preview_settlement_totals(
            invoice="CHK-AM1-PRIV",
            pos_profile="Lakeside Till",
            discount_percent=100,
            discount_reason="Complaint",
        )
        self.assertEqual(p100.get("privilege"), "complimentary")

    def test_frappe_document_child_rows_without_dict_item_assignment_work_on_preview_apply_and_remove(self):
        """Real Frappe POSInvoiceItem rows are Document objects (have .get/.set, NOT __setitem__)."""
        class _FrappeChildRow:
            def __init__(self, data):
                self.__dict__.update(data)

            def get(self, key, default=None):
                return self.__dict__.get(key, default)

            def set(self, key, value):
                self.__dict__[key] = value

            def __getitem__(self, key):
                raise TypeError("'POSInvoiceItem' object is not subscriptable")

            def __setitem__(self, key, value):
                raise TypeError("'POSInvoiceItem' object does not support item assignment")

        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 20",
            discount_approver_role="URY Manager",
        )
        check = self._register_check(FakePOSInvoice("CHK-DOCROW", items=[]))
        check.items = [_FrappeChildRow(dict(_item("r1", "FISH", 1, 50000)))]
        check.calculate_taxes_and_totals = lambda: (
            setattr(check, "grand_total", 50000 - (5000 if check.items[0].get("discount_percentage") == 10 else 0)),
            setattr(check, "rounded_total", check.grand_total),
            setattr(check, "net_total", check.grand_total),
        )
        self._patch_settlement_helpers(check)

        # Preview restores snapshot without TypeError
        prev = self.order.preview_settlement_totals(
            invoice="CHK-DOCROW",
            pos_profile="Lakeside Till",
            line_discounts=[{"row": "r1", "percent": 10}],
            discount_reason="Complaint",
        )
        self.assertEqual(prev["after_total"], 45000)

        # Apply line discount and then remove single row without TypeError
        self.order.apply_check_discount(
            invoice="CHK-DOCROW",
            pos_profile="Lakeside Till",
            line_discounts=[{"row": "r1", "percent": 10}],
            discount_reason="Complaint",
        )
        self.assertEqual(check.items[0].get("discount_percentage"), 10)
        self.order.remove_check_discount(invoice="CHK-DOCROW", pos_profile="Lakeside Till", row="r1")
        self.assertEqual(check.items[0].get("discount_percentage"), 0)


class TestHydrogenR1Defects(DiscountBase):
    """RED -> GREEN regression suite for every P0 and P1 uncovered in Hydrogen R1 live review."""

    def _siteops_disc_007_guard(self, doc, prev_approver, prev_pct):
        """Mirrors siteops#8620 sales-discount-control@3 lines 243-252."""
        approval_flag = getattr(getattr(doc, "flags", None), "discount_approval", None)
        if approval_flag is None:
            curr_approver = str(doc.get("custom_discount_approver") or "")
            curr_pct = float(doc.get("custom_discount_approved_percent") or 0.0)
            if curr_approver != str(prev_approver or "") or curr_pct != float(prev_pct or 0.0):
                raise Refused(
                    "SITEOPS-DISC-007: Discount approver and approved percentage can only be set "
                    "by a verified override and cannot be altered on a later save. Nothing was saved."
                )

    def test_p0_2_line_percent_amount_and_combined_persist_under_erpnext_v16_calculate_item_rate(self):
        """P0-2: Real ERPNext v16 calculate_item_rate resets discount_percentage=0 if rate is not updated."""
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 25",
            discount_approver_role="Owner Approver",
        )
        check = self._register_check(
            FakePOSInvoice(
                "CHK-P02",
                items=[
                    _item("r1", "STEAK", 1, 50000),
                    _item("r2", "JUICE", 1, 10000),
                ],
            )
        )
        self._patch_settlement_helpers(check)

        # 1) Line % preview + combined with check flat amount 2,000 -> 5,000 + 2,000 = 7,000 off 60,000 (11.67% > 5% limit!)
        prev_comb = self.order.preview_settlement_totals(
            invoice="CHK-P02",
            pos_profile="Lakeside Till",
            discount_amount=2000,
            line_discounts=[{"row": "r1", "percent": 10}],
            discount_reason="Complaint",
        )
        self.assertEqual(prev_comb["line_discount_total"], 5000)
        self.assertEqual(prev_comb["header_discount_amount"], 2000)
        self.assertEqual(prev_comb["discount_total"], 7000)
        self.assertEqual(prev_comb["after_total"], 53000)
        self.assertAlmostEqual(prev_comb["effective_percent"], 11.6667, places=2)
        self.assertTrue(prev_comb["needs_approval"])

        # 2) Line amount preview (5,000 off 50,000)
        prev_amt = self.order.preview_settlement_totals(
            invoice="CHK-P02",
            pos_profile="Lakeside Till",
            line_discounts=[{"row": "r1", "amount": 5000}],
            discount_reason="Complaint",
        )
        self.assertEqual(prev_amt["line_discount_total"], 5000)
        self.assertEqual(prev_amt["discount_total"], 5000)
        self.assertEqual(prev_amt["after_total"], 55000)

        # 3) Persisting with manager approval keeps rate=45000 and discount_percentage=10 even after second calculate_taxes_and_totals()
        token = self._issue_override_token(check, prev_comb["claim"], approver="manager@example.com", percent=12)
        saved = self.order.apply_check_discount(
            invoice="CHK-P02",
            pos_profile="Lakeside Till",
            discount_amount=2000,
            line_discounts=[{"row": "r1", "percent": 10}],
            discount_reason="Complaint",
            approval=token,
        )
        # Re-run ERPNext v16 calculate_taxes_and_totals as save() does
        check.calculate_taxes_and_totals()
        self.assertEqual(check.items[0].discount_percentage, 10)
        self.assertEqual(check.items[0].rate, 45000)
        self.assertEqual(check.items[0].amount, 45000)
        self.assertEqual(saved["line_discount_total"], 5000)
        self.assertEqual(saved["after_total"], 53000)

    def test_p0_3_remove_approved_discount_and_lower_downward_pass_siteops_disc_007_and_validate(self):
        """P0-3: Removing an approved discount passes SITEOPS-DISC-007; lowering 10%->5% passes without POS-DISC-FORGED."""
        self.has_privilege_rule_doctype = True
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 25",
            discount_approver_role="Owner Approver",
        )
        self.privilege_rules = [
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Cashier", may_perform=1, may_authorise=0, max_percent=5),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=25),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount_remove", role="URY Manager", may_perform=1, may_authorise=1, max_percent=0),
        ]
        check = self._register_check(FakePOSInvoice("CHK-P03", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        # Apply 10% with manager PIN
        p10 = self.order.preview_settlement_totals(
            invoice="CHK-P03", pos_profile="Lakeside Till", discount_percent=10, discount_reason="Complaint"
        )
        tok10 = self._issue_override_token(check, p10["claim"], approver="manager@example.com", percent=10)
        self.order.apply_check_discount(
            invoice="CHK-P03", pos_profile="Lakeside Till", discount_percent=10, discount_reason="Complaint", approval=tok10
        )
        self.assertEqual(check.custom_discount_approver, "manager@example.com")
        self.assertEqual(check.custom_discount_approved_percent, 10.0)

        # Snapshot committed state before downward edit (10% -> 5%)
        prev_snap = FakePOSInvoice(
            "CHK-P03",
            items=[_item("r1", "FISH", 1, 50000)],
            additional_discount_percentage=10.0,
            discount_amount=5000.0,
            custom_discount_reason="Complaint",
            custom_discount_approver="manager@example.com",
            custom_discount_approved_percent=10.0,
            ury_discount_meta=check.ury_discount_meta,
        )
        check._doc_before_save = prev_snap

        def _guarded_save(**kw):
            self._siteops_disc_007_guard(check, prev_snap.custom_discount_approver, prev_snap.custom_discount_approved_percent)
            self.hook.validate_pos_discount(check)

        check.save = _guarded_save

        # 1) Lowering 10% -> 5% (within cashier's 5% limit, no PIN) MUST succeed without POS-DISC-FORGED or SITEOPS-DISC-007
        res5 = self.order.apply_check_discount(
            invoice="CHK-P03", pos_profile="Lakeside Till", discount_percent=5, discount_reason="Complaint"
        )
        self.assertEqual(res5["after_total"], 47500)
        self.assertEqual(check.additional_discount_percentage, 5.0)

        # 2) Removing the approved discount with discount_remove PIN MUST pass SITEOPS-DISC-007 & clear fields
        prev_snap2 = FakePOSInvoice(
            "CHK-P03",
            items=[_item("r1", "FISH", 1, 50000)],
            additional_discount_percentage=5.0,
            discount_amount=2500.0,
            custom_discount_reason="Complaint",
            custom_discount_approver=check.custom_discount_approver,
            custom_discount_approved_percent=check.custom_discount_approved_percent,
            ury_discount_meta=check.ury_discount_meta,
        )
        check._doc_before_save = prev_snap2

        def _guarded_remove_save(**kw):
            self._siteops_disc_007_guard(check, prev_snap2.custom_discount_approver, prev_snap2.custom_discount_approved_percent)
            self.hook.validate_pos_discount(check)

        check.save = _guarded_remove_save

        rem_claim = self.discount.canonical_remove_claim(check, row=None)
        rem_tok = self._issue_override_token(check, rem_claim, approver="manager@example.com", privilege="discount_remove")
        res_rem = self.order.remove_check_discount(
            invoice="CHK-P03", pos_profile="Lakeside Till", approval=rem_tok
        )
        self.assertEqual(res_rem["after_total"], 50000)
        self.assertFalse(check.custom_discount_approver)
        self.assertEqual(check.additional_discount_percentage, 0)

    def test_p0_4_all_discount_removal_bypasses_blocked_without_discount_remove_auth(self):
        """P0-4: apply(percent=0), make_invoice(clear_discount=1), apply(line percent=0), and REST clear all require discount_remove."""
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 20\nURY Manager: 80",
            discount_approver_role="URY Manager",
        )
        check = self._register_check(
            FakePOSInvoice("CHK-P04", items=[_item("r1", "FISH", 1, 50000)], invoice_printed=1)
        )
        self._patch_settlement_helpers(check)

        # Put both header 5% and line 10% on printed check
        self.order.apply_check_discount(
            invoice="CHK-P04",
            pos_profile="Lakeside Till",
            discount_percent=5,
            line_discounts=[{"row": "r1", "percent": 10}],
            discount_reason="Complaint",
        )
        self.assertEqual(check.additional_discount_percentage, 5)
        self.assertEqual(check.items[0].discount_percentage, 10)

        # Vector 1: apply_check_discount(discount_percent=0)
        with self.assertRaises((Refused, NotPermitted)) as ctx1:
            self.order.apply_check_discount(
                invoice="CHK-P04", pos_profile="Lakeside Till", discount_percent=0
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx1.exception))
        self.assertEqual(check.additional_discount_percentage, 5)

        # Vector 2: make_invoice(clear_discount=1)
        with self.assertRaises((Refused, NotPermitted)) as ctx2:
            self.order.make_invoice(
                customer="Walk-in",
                payments=[{"mode_of_payment": "Cash", "amount": 50000}],
                cashier="cashier@example.com",
                pos_profile="Lakeside Till",
                owner="cashier@example.com",
                invoice="CHK-P04",
                submit_invoice=0,
                clear_discount=1,
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx2.exception))
        self.assertEqual(check.additional_discount_percentage, 5)

        # Vector 3: apply_check_discount(line_discounts=[{"row": "r1", "percent": 0}])
        with self.assertRaises((Refused, NotPermitted)) as ctx3:
            self.order.apply_check_discount(
                invoice="CHK-P04",
                pos_profile="Lakeside Till",
                discount_percent=5,
                line_discounts=[{"row": "r1", "percent": 0}],
                discount_reason="Complaint",
            )
        self.assertIn("POS-AUTH-REQUIRED", str(ctx3.exception))
        self.assertEqual(check.items[0].discount_percentage, 10)

        # Vector 4a: direct REST save clearing header discount (even if flipping invoice_printed=0!)
        prev_printed = FakePOSInvoice(
            "CHK-P04-REST",
            items=[_item("r1", "FISH", 1, 50000)],
            invoice_printed=1,
            additional_discount_percentage=10,
            discount_amount=5000,
            custom_discount_reason="Complaint",
        )
        prev_printed.calculate_taxes_and_totals()
        rest_cleared = FakePOSInvoice(
            "CHK-P04-REST",
            items=[_item("r1", "FISH", 1, 50000)],
            invoice_printed=0,  # attempt to clear invoice_printed in same PUT!
            additional_discount_percentage=0,
            discount_amount=0,
        )
        rest_cleared._doc_before_save = prev_printed
        rest_cleared.calculate_taxes_and_totals()
        with self.assertRaises((Refused, NotPermitted)) as ctx4:
            self.hook.validate_pos_discount(rest_cleared)
        self.assertIn("POS-AUTH-REQUIRED", str(ctx4.exception))

        # Vector 4b: direct REST save clearing line discount on printed check
        prev_line = FakePOSInvoice(
            "CHK-P04-RESTL",
            items=[_item("r1", "FISH", 1, 45000, price_list_rate=50000, base_price_list_rate=50000, discount_percentage=10, discount_amount=5000, amount=45000)],
            invoice_printed=1,
            custom_discount_reason="Complaint",
        )
        rest_line_cleared = FakePOSInvoice(
            "CHK-P04-RESTL",
            items=[_item("r1", "FISH", 1, 50000, discount_percentage=0, discount_amount=0, amount=50000)],
            invoice_printed=1,
        )
        rest_line_cleared._doc_before_save = prev_line
        with self.assertRaises((Refused, NotPermitted)) as ctx4b:
            self.hook.validate_pos_discount(rest_line_cleared)
        self.assertIn("POS-AUTH-REQUIRED", str(ctx4b.exception))

    def test_p0_5_actual_tax_charge_row_cannot_zero_out_discount_or_bypass_validation(self):
        """P0-5: Adding charge_type='Actual' (5000) + discount_amount=5000 must NOT compute discount_total=0."""
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5",
            discount_approver_role="Owner Approver",
        )
        prev = FakePOSInvoice(
            "CHK-P05",
            items=[_item("r1", "FISH", 1, 50000)],
            taxes=[Row(charge_type="Actual", description="Service Fee", tax_amount=5000, rate=0, included_in_print_rate=0)],
        )
        doc = FakePOSInvoice(
            "CHK-P05",
            items=[_item("r1", "FISH", 1, 50000)],
            taxes=[Row(charge_type="Actual", description="Service Fee", tax_amount=5000, rate=0, included_in_print_rate=0)],
            apply_discount_on="Grand Total",
        )
        doc._doc_before_save = prev
        doc.discount_amount = 5000
        doc.calculate_taxes_and_totals()

        summary = self.discount._compute_manual_discount_summary(doc, 0)
        self.assertEqual(summary["discount_total"], 5000.0)
        self.assertEqual(summary["header_discount_amount"], 5000.0)
        self.assertEqual(summary["effective_percent"], 10.0)

        # Must refuse because reason is missing (and 10% > 5% limit)
        with self.assertRaises((Refused, NotPermitted)):
            self.hook.validate_pos_discount(doc)

    def test_p0_6_complimentary_approver_eligibility_aligned_with_approval_limit_and_owner_approver(self):
        """P0-6: URY Manager (25% limit, not Owner Approver) cannot approve 100% comp; Owner Approver without rule CAN."""
        self.has_privilege_rule_doctype = True
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 25",
            discount_approver_role="Owner Approver",
        )
        self.privilege_rules = [
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Cashier", may_perform=1, may_authorise=0, max_percent=5),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=25),
            Row(company="Lakeside Hotel", pos_profile="", privilege="complimentary", role="URY Manager", may_perform=0, may_authorise=1, max_percent=0, requires_reason=1),
        ]
        # Seeded POS Reason rows (like real B2a migration!) must NOT block Discount Reason on complimentary
        self.pos_reasons = [
            Row(code="OTHER", description="Other", applies_to="", requires_note=1, sort_order=999, disabled=0),
        ]
        check = self._register_check(FakePOSInvoice("CHK-P06", items=[_item("r1", "FISH", 1, 60000)]))
        override_mod = self.discount._get_override_module()

        approvers = override_mod.list_approvers(
            pos_profile="Lakeside Till",
            privilege="complimentary",
            reference_name="CHK-P06",
            percent=100,
        )
        approver_ids = {a["user"] for a in approvers}
        self.assertNotIn("manager@example.com", approver_ids)
        self.assertIn("owner@example.com", approver_ids)

        claim = self.discount.canonical_discount_claim(percent=100, reason="Complaint", precision=0)
        # URY Manager (limit 25%, not Owner Approver) MUST be refused at authorise() before consuming token
        with self.assertRaises((Refused, NotPermitted)) as ctx:
            override_mod.authorise(
                pos_profile="Lakeside Till",
                privilege="complimentary",
                approver="manager@example.com",
                pin="4321",
                reference_doctype="POS Invoice",
                reference_name="CHK-P06",
                claim=claim,
            )
        self.assertIn("POS-AUTH-NOT-ELIGIBLE", str(ctx.exception))

        # Owner Approver (no POS Privilege Rule row for complimentary) MUST succeed with Discount Reason "Complaint"
        res = override_mod.authorise(
            pos_profile="Lakeside Till",
            privilege="complimentary",
            approver="owner@example.com",
            pin="4321",
            reference_doctype="POS Invoice",
            reference_name="CHK-P06",
            claim=claim,
        )
        self.assertEqual(res["approver"], "owner@example.com")

    def test_p0_7_split_bill_by_approving_owner_and_meta_only_approver_pass_siteops_guards(self):
        """P0-7: Approving owner splitting their approved check & meta-only approver split both avoid SITEOPS-DISC-002/007."""
        self.enterContext(patch.object(self.order, "_enforce_order_access"))
        self.enterContext(patch.object(self.order, "_split_kots_to_move", return_value=[]))
        self.frappe.generate_hash.return_value = "GRP-P07"

        def _siteops_check(doc, is_new=False, prev_approver="", prev_pct=0.0):
            flag = getattr(getattr(doc, "flags", None), "discount_approval", None)
            if flag is not None:
                ap = str(flag.get("approver") or "")
                if ap == self.frappe.session.user or ap == str(doc.get("owner") or ""):
                    raise Refused("SITEOPS-DISC-002 (SUPERVISOR_SELF_APPROVAL)")
            elif not is_new:
                curr_ap = str(doc.get("custom_discount_approver") or "")
                curr_pct = float(doc.get("custom_discount_approved_percent") or 0.0)
                if curr_ap != prev_approver or curr_pct != prev_pct:
                    raise Refused("SITEOPS-DISC-007")
            else:
                if doc.get("custom_discount_approver") or float(doc.get("custom_discount_approved_percent") or 0) > 0:
                    raise Refused("SITEOPS-DISC-007")

        # Case 1: Approving owner splits cashier's check
        self.frappe.session.user = "owner@example.com"
        source1 = self._register_check(
            FakePOSInvoice(
                "CHK-P07-1",
                owner="cashier@example.com",
                items=[_item("i1", "FISH", 1, 30000), _item("i2", "STEAK", 1, 20000)],
                additional_discount_percentage=30,
                custom_discount_reason="Complaint",
                custom_discount_approver="owner@example.com",
                custom_discount_approved_percent=30.0,
            )
        )
        source1.save = lambda **kw: _siteops_check(source1, is_new=False, prev_approver="owner@example.com", prev_pct=30.0)
        child1 = FakePOSInvoice("CHK-P07-1C", owner="owner@example.com", items=[])
        child1.insert = lambda **kw: _siteops_check(child1, is_new=True)
        self.frappe.new_doc.return_value = child1

        self.order.split_bill("CHK-P07-1", [{"name": "i1", "qty": 1}])
        self.assertEqual(self.frappe.session.user, "owner@example.com")  # session restored!
        self.assertEqual(child1.owner, "cashier@example.com")

        # Case 2: Source had approver ONLY in ury_discount_meta (pre-B1), split after B1 installed
        self.frappe.session.user = "cashier@example.com"
        source2 = self._register_check(
            FakePOSInvoice(
                "CHK-P07-2",
                owner="cashier@example.com",
                items=[_item("i1", "FISH", 1, 30000), _item("i2", "STEAK", 1, 20000)],
                additional_discount_percentage=20,
                custom_discount_reason="Complaint",
                custom_discount_approver=None,
                custom_discount_approved_percent=0.0,
                ury_discount_meta=json.dumps({
                    "reason": "Complaint",
                    "approver": "manager@example.com",
                    "approved_percent": 20.0,
                }),
            )
        )
        source2.save = lambda **kw: _siteops_check(source2, is_new=False, prev_approver="", prev_pct=0.0)
        child2 = FakePOSInvoice("CHK-P07-2C", owner="cashier@example.com", items=[])
        child2.insert = lambda **kw: _siteops_check(child2, is_new=True)
        self.frappe.new_doc.return_value = child2

        self.order.split_bill("CHK-P07-2", [{"name": "i1", "qty": 1}])
        self.assertEqual(child2.flags.discount_approval["approver"], "manager@example.com")
        self.assertEqual(source2.flags.discount_approval["approver"], "manager@example.com")

    def test_p1_1_explicit_complimentary_allowed_when_open_discounts_disabled(self):
        """P1-1: Explicit complimentary=1 works when allow_discount_change=0, while open 10% stays refused."""
        self.profile.allow_discount_change = 0
        check = self._register_check(FakePOSInvoice("CHK-P11", items=[_item("r1", "FISH", 1, 50000)]))
        self._patch_settlement_helpers(check)

        with self.assertRaises((Refused, NotPermitted)):
            self.order.preview_settlement_totals(
                invoice="CHK-P11", pos_profile="Lakeside Till", discount_percent=10, discount_reason="Complaint"
            )

        comp = self.order.preview_settlement_totals(
            invoice="CHK-P11",
            pos_profile="Lakeside Till",
            discount_percent=100,
            complimentary=1,
            discount_reason="Complaint",
        )
        self.assertTrue(comp["is_comp"])
        self.assertEqual(comp["privilege"], "complimentary")
        self.assertTrue(comp["needs_approval"])
        self.assertEqual(comp["after_total"], 0)

    def test_p1_2_currency_precision_prefers_iso_minor_units_over_system_default_2_for_ugx_and_kwd(self):
        """P1-2: UGX=0 and KWD=3 even when real Frappe doc.precision('grand_total') returns System Settings 2."""
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 50",
            discount_approver_role="URY Manager",
        )
        # Simulate Frappe System Settings currency_precision = 2 overriding doc.precision()
        ugx_doc = self._register_check(FakePOSInvoice("CHK-UGX-P", currency="UGX", items=[_item("r1", "FISH", 1, 50000)]))
        ugx_doc.precision = lambda fieldname, parentfield=None: 2
        self._patch_settlement_helpers(ugx_doc)
        p_ugx = self.order.preview_settlement_totals(
            invoice="CHK-UGX-P", pos_profile="Lakeside Till", discount_amount=5000, discount_reason="Complaint"
        )
        self.assertEqual(p_ugx["precision"], 0)

        self.profile.currency = "KWD"
        kwd_doc = self._register_check(
            FakePOSInvoice("CHK-KWD-P", currency="KWD", items=[_item("r1", "STEAK", 1, 15.375)])
        )
        kwd_doc.precision = lambda fieldname, parentfield=None: 2
        with patch.object(self.order, "get_order_invoice", return_value=kwd_doc):
            p_kwd = self.order.preview_settlement_totals(
                invoice="CHK-KWD-P", pos_profile="Lakeside Till", discount_amount=1.234, discount_reason="Complaint"
            )
        self.assertEqual(p_kwd["precision"], 3)
        self.assertEqual(p_kwd["before_total"], 15.375)
        self.assertEqual(p_kwd["discount_total"], 1.234)
        self.assertEqual(p_kwd["after_total"], 14.141)

    def test_p1_2_apply_check_discount_preserves_kwd_3_decimals_on_save(self):
        """P1-2: apply_check_discount must preserve 3 decimals on KWD save() even when System Settings currency_precision=2."""
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 50",
            discount_approver_role="URY Manager",
        )
        self.profile.currency = "KWD"
        kwd_doc = self._register_check(
            FakePOSInvoice("CHK-KWD-SAVE", currency="KWD", items=[_item("r1", "STEAK", 1, 15.375)])
        )
        kwd_doc.precision = lambda fieldname, parentfield=None: 2
        orig_save = kwd_doc.save

        def _real_erpnext_save(ignore_permissions=False):
            # Mirror Frappe Document._validate -> calculate_taxes_and_totals + round_floats_in using doc.precision()
            kwd_doc.calculate_taxes_and_totals()
            p = kwd_doc.precision("grand_total")
            kwd_doc.grand_total = round(kwd_doc.grand_total, p)
            kwd_doc.rounded_total = round(kwd_doc.rounded_total, p)
            return orig_save(ignore_permissions=ignore_permissions)

        kwd_doc.save = _real_erpnext_save
        self._patch_settlement_helpers(kwd_doc)
        self.order.apply_check_discount(
            invoice="CHK-KWD-SAVE",
            pos_profile="Lakeside Till",
            discount_amount=1.234,
            discount_reason="Complaint",
        )
        self.assertEqual(kwd_doc.grand_total, 14.141)

    def test_p1_3_item_scoped_coupon_refused_without_b1(self):
        """P1-3: Coupon linked to Pricing Rule with apply_on='Item Code' must NOT bleed across header without #8620."""
        self.has_approval_limit_doctype = False
        self.pricing_rules["PR-ITEM10"] = Row(
            name="PR-ITEM10",
            title="Item Only 10%",
            apply_on="Item Code",
            selling=1,
            disable=0,
            coupon_code_based=1,
            price_or_product_discount="Price",
            rate_or_discount="Discount Percentage",
            discount_percentage=10.0,
            discount_amount=0.0,
            company="Lakeside Hotel",
            valid_from="2026-01-01",
            valid_upto="2026-12-31",
        )
        self.coupons["ITEMONLY10"] = Row(
            name="ITEMONLY10",
            coupon_name="ITEMONLY10",
            coupon_code="ITEMONLY10",
            coupon_type="Promotional",
            pricing_rule="PR-ITEM10",
            valid_from="2026-01-01",
            valid_upto="2026-12-31",
            maximum_use=0,
            used=0,
        )
        check = self._register_check(
            FakePOSInvoice("CHK-P13", items=[_item("r1", "FISH", 1, 40000), _item("r2", "JUICE", 1, 10000)])
        )
        self._patch_settlement_helpers(check)

        with self.assertRaises(Refused) as ctx:
            self.order.preview_settlement_totals(
                invoice="CHK-P13", pos_profile="Lakeside Till", coupon_code="ITEMONLY10", discount_reason="Complaint"
            )
        self.assertIn("POS-DISC-COUPON-SCOPE", str(ctx.exception))

        with self.assertRaises(Refused) as ctx2:
            self.order.apply_check_discount(
                invoice="CHK-P13", pos_profile="Lakeside Till", coupon_code="ITEMONLY10", discount_reason="Complaint"
            )
        self.assertIn("POS-DISC-COUPON-SCOPE", str(ctx2.exception))

    def test_p1_5_manager_privilege_rule_limit_honored_when_approval_limit_not_installed(self):
        """P1-5: Without #8620 Approval Limit, min(0%, Privilege Rule) must NOT zero out managers with max_percent=25."""
        self.has_approval_limit_doctype = False
        self.has_privilege_rule_doctype = True
        self.privilege_rules = [
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Cashier", may_perform=1, may_authorise=0, max_percent=0),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=25),
        ]
        self.assertEqual(self.discount.user_discount_limit("manager@example.com", "Lakeside Hotel", "Lakeside Till"), 25.0)
        self.assertEqual(self.discount.user_discount_limit("cashier@example.com", "Lakeside Hotel", "Lakeside Till"), 0.0)


class TestHydrogenR2Regressions(DiscountBase):
    """Strict RED->GREEN regression tests for Hydrogen R2 findings on pmwanje2/ury#148."""

    @staticmethod
    def _siteops_v3_exceeds_limit(header_pct, disc_units, basis_units, approved_pct):
        """Mirror siteops#8620 sales-discount-control@3 parse_percent + exceeds_limit."""
        units = int(round(float(approved_pct) * 1000000.0))
        scale = 1000000
        while scale > 1 and units % 10 == 0:
            units //= 10
            scale //= 10
        limit_num = units / float(scale)
        if float(header_pct or 0.0) > limit_num:
            return True
        if disc_units <= 0:
            return False
        if units == 0:
            return True
        denom = 100 * scale
        allowed_units = (2 * basis_units * units + denom) // (2 * denom)
        return disc_units > allowed_units

    def test_r2_p0_1_rounded_percent_pin_approval_covers_requested_percent_kwd_ugx_usd(self):
        """P0 #1: Rounded % discounts (KWD 15.712, UGX 333, USD 10.04 @ 10%) with Manager PIN
        must set flags.discount_approval['percent'] to cover both effective_pct and requested
        additional_discount_percentage without exceeding the approver's authorized ceiling."""
        self.has_privilege_rule_doctype = True
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 25",
            discount_approver_role="Owner Approver",
        )
        self.privilege_rules = [
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Cashier", may_perform=1, may_authorise=0, max_percent=5),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=25),
            Row(company="Lakeside Hotel", pos_profile="", privilege="split_check", role="URY Cashier", may_perform=1, may_authorise=0),
            Row(company="Lakeside Hotel", pos_profile="", privilege="split_check", role="URY Manager", may_perform=1, may_authorise=1),
        ]

        cases = [
            ("CHK-R2-KWD", "KWD", 3, 15.712, 10.0),
            ("CHK-R2-UGX", "UGX", 0, 333.0, 10.0),
            ("CHK-R2-USD", "USD", 2, 10.04, 10.0),
        ]
        for inv_name, currency, prec, gross, req_pct in cases:
            with self.subTest(currency=currency, gross=gross, req_pct=req_pct):
                self.profile.currency = currency
                check = self._register_check(
                    FakePOSInvoice(inv_name, currency=currency, items=[_item("r1", "STEAK", 1, gross)])
                )
                check.precision = lambda fieldname, parentfield=None, _p=prec: _p

                def _siteops_v3_save(_chk=check, _prec=prec, _gross=gross, **kw):
                    flag = getattr(getattr(_chk, "flags", None), "discount_approval", None)
                    self.assertIsNotNone(flag, "PIN-approved discount must set flags.discount_approval")
                    scale = 10 ** _prec
                    disc_units = int(round(float(_chk.discount_amount or 0.0) * scale))
                    basis_units = int(round(_gross * scale))
                    if self._siteops_v3_exceeds_limit(
                        _chk.additional_discount_percentage, disc_units, basis_units, flag["percent"]
                    ):
                        raise Refused(
                            "SITEOPS-DISC-002: The approved discount percentage does not cover the discount on this check."
                        )
                    self.hook.validate_pos_discount(_chk)

                check.save = _siteops_v3_save
                self._patch_settlement_helpers(check)

                prev = self.order.preview_settlement_totals(
                    invoice=inv_name, pos_profile="Lakeside Till", discount_percent=req_pct, discount_reason="Complaint"
                )
                self.assertTrue(prev["needs_approval"])
                tok = self._issue_override_token(check, prev["claim"], approver="manager@example.com", percent=req_pct)
                self.order.apply_check_discount(
                    invoice=inv_name,
                    pos_profile="Lakeside Till",
                    discount_percent=req_pct,
                    discount_reason="Complaint",
                    approval=tok,
                )
                self.assertGreaterEqual(check.flags.discount_approval["percent"], req_pct)
                self.assertEqual(check.flags.discount_approval["percent"], req_pct)
                self.assertEqual(check.custom_discount_approved_percent, req_pct)

        # Ceiling safety: UGX 335 @ 10% rounds UP (34 UGX => effective_pct = 10.1493%).
        # When Manager ceiling is 10%, approved_pct MUST NOT exceed 10.0 and must still pass
        # both #8620 half-up check and subsequent validate_pos_discount saves.
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 10",
            discount_approver_role="Owner Approver",
        )
        self.privilege_rules = [
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Cashier", may_perform=1, may_authorise=0, max_percent=5),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=10),
        ]
        self.profile.currency = "UGX"
        up_check = self._register_check(
            FakePOSInvoice("CHK-R2-UGX-UP", currency="UGX", items=[_item("r1", "STEAK", 1, 335.0)])
        )
        up_check.precision = lambda fieldname, parentfield=None: 0

        def _siteops_up_save(**kw):
            flag = getattr(getattr(up_check, "flags", None), "discount_approval", None)
            self.assertIsNotNone(flag)
            self.assertLessEqual(flag["percent"], 10.0)
            disc_units = int(round(float(up_check.discount_amount or 0.0)))
            self.assertFalse(
                self._siteops_v3_exceeds_limit(up_check.additional_discount_percentage, disc_units, 335, flag["percent"])
            )
            self.hook.validate_pos_discount(up_check)

        up_check.save = _siteops_up_save
        self._patch_settlement_helpers(up_check)
        prev_up = self.order.preview_settlement_totals(
            invoice="CHK-R2-UGX-UP", pos_profile="Lakeside Till", discount_percent=10.0, discount_reason="Complaint"
        )
        tok_up = self._issue_override_token(up_check, prev_up["claim"], approver="manager@example.com", percent=10.0)
        self.order.apply_check_discount(
            invoice="CHK-R2-UGX-UP",
            pos_profile="Lakeside Till",
            discount_percent=10.0,
            discount_reason="Complaint",
            approval=tok_up,
        )
        self.assertEqual(up_check.flags.discount_approval["percent"], 10.0)
        self.assertEqual(up_check.custom_discount_approved_percent, 10.0)

        # Subsequent print/settlement save (without approval flag) must also pass validate_pos_discount
        up_check.flags.discount_approval = None
        up_check._doc_before_save = FakePOSInvoice(
            "CHK-R2-UGX-UP",
            currency="UGX",
            items=[_item("r1", "STEAK", 1, 335.0)],
            additional_discount_percentage=10.0,
            discount_amount=up_check.discount_amount,
            custom_discount_reason="Complaint",
            custom_discount_approver="manager@example.com",
            custom_discount_approved_percent=10.0,
            ury_discount_meta=up_check.ury_discount_meta,
        )
        up_check._doc_before_save.calculate_taxes_and_totals()
        self.hook.validate_pos_discount(up_check)

    def test_r2_p0_2_consolidate_line_only_discounted_check_preserves_meta_and_passes_siteops_disc_001(self):
        """P0 #2: Consolidating a line-only discounted check onto a clean check must preserve
        ury_discount_meta standalone AND write custom_discount_* + flags.discount_approval for #8620."""
        self.enterContext(patch.object(self.order, "get_branch_stock_policy", return_value=Row(reservation_control_enabled=False)))
        self.enterContext(patch.object(self.order, "_free_tables_if_no_open_invoices"))

        # Case A: Standalone (without B1 custom_discount_* fields, only ury_discount_meta)
        orig_get_meta = self.frappe.get_meta
        def _meta_without_b1(dt):
            if dt == "POS Invoice":
                return Row(has_field=lambda f: f == "ury_discount_meta")
            return orig_get_meta(dt)

        with patch.object(self.frappe, "get_meta", side_effect=_meta_without_b1):
            src_a = self._register_check(
                FakePOSInvoice(
                    "CHK-M-LINE-SRC-A",
                    currency="UGX",
                    items=[
                        _item(
                            "s1", "STEAK", 1, 24000,
                            price_list_rate=30000, base_price_list_rate=30000,
                            discount_percentage=20, discount_amount=6000, amount=24000,
                        )
                    ],
                    ury_discount_meta=json.dumps({
                        "reason": "Complaint",
                        "reason_description": "Complaint",
                        "approver": "manager@example.com",
                        "approved_percent": 20.0,
                        "line_reasons": {"s1": "Complaint"},
                    }),
                )
            )
            dst_a = self._register_check(
                FakePOSInvoice("CHK-M-LINE-DST-A", currency="UGX", items=[_item("d1", "JUICE", 1, 10000)])
            )
            self.order.consolidate_checks(dst_a, [src_a])

            self.assertIsNotNone(dst_a.ury_discount_meta, "Standalone consolidate dropped ury_discount_meta on line-only check")
            parsed_a = json.loads(dst_a.ury_discount_meta)
            self.assertEqual(parsed_a["reason"], "Complaint")
            self.assertEqual(parsed_a["approver"], "manager@example.com")
            self.assertEqual(parsed_a["approved_percent"], 20.0)
            self.assertEqual(dst_a.grand_total, 34000)

        # Case B: Under #8620 B1 schema — must pass SITEOPS-DISC-001 and SITEOPS-DISC-002
        src_b = self._register_check(
            FakePOSInvoice(
                "CHK-M-LINE-SRC-B",
                currency="UGX",
                items=[
                    _item(
                        "s1", "STEAK", 1, 24000,
                        price_list_rate=30000, base_price_list_rate=30000,
                        discount_percentage=20, discount_amount=6000, amount=24000,
                    )
                ],
                custom_discount_reason="Complaint",
                custom_discount_approver="manager@example.com",
                custom_discount_approved_percent=20.0,
                ury_discount_meta=json.dumps({
                    "reason": "Complaint",
                    "reason_description": "Complaint",
                    "approver": "manager@example.com",
                    "approved_percent": 20.0,
                    "line_reasons": {"s1": "Complaint"},
                }),
            )
        )
        dst_b = self._register_check(
            FakePOSInvoice("CHK-M-LINE-DST-B", currency="UGX", items=[_item("d1", "JUICE", 1, 10000)])
        )

        def _siteops_merge_save(**kw):
            # Mirror #8620 SITEOPS-DISC-001 + SITEOPS-DISC-002 for line-discounted check
            if not (dst_b.get("custom_discount_reason") or "").strip():
                raise Refused("SITEOPS-DISC-001: Every till discount needs an active POS Discount Reason. Nothing was saved.")
            flag = getattr(getattr(dst_b, "flags", None), "discount_approval", None)
            if not flag or not flag.get("approver"):
                raise Refused("SITEOPS-DISC-002: This discount exceeds your 5% limit and needs a supervisor's override.")
            if self._siteops_v3_exceeds_limit(0.0, 6000, 40000, flag["percent"]):
                raise Refused("SITEOPS-DISC-002: Approved percentage does not cover consolidated line discount.")

        dst_b.save = _siteops_merge_save
        self.order.consolidate_checks(dst_b, [src_b])

        self.assertEqual(dst_b.custom_discount_reason, "Complaint")
        self.assertEqual(dst_b.custom_discount_approver, "manager@example.com")
        self.assertEqual(dst_b.custom_discount_approved_percent, 20.0)
        self.assertEqual(dst_b.flags.discount_approval["approver"], "manager@example.com")
        self.assertEqual(dst_b.flags.discount_approval["percent"], 20.0)
        self.assertEqual(dst_b.grand_total, 34000)

    def test_r2_p1_split_bill_discount_percent_inflation_blocks_without_pin_and_succeeds_with_pin(self):
        """P1: split_bill must fail closed with POS-AUTH-REQUIRED when splitting inflates either
        half's effective discount % above both caller self-limit and prior approved %, and succeed
        cleanly when re-authorized with a Manager PIN token."""
        self.has_privilege_rule_doctype = True
        self.approval_limits["Lakeside Hotel"] = Row(
            max_document_discount_percent="0",
            discount_limits_by_role="URY Cashier: 5\nURY Manager: 25",
            discount_approver_role="Owner Approver",
        )
        self.privilege_rules = [
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Cashier", may_perform=1, may_authorise=0, max_percent=5),
            Row(company="Lakeside Hotel", pos_profile="", privilege="discount", role="URY Manager", may_perform=1, may_authorise=1, max_percent=25),
            Row(company="Lakeside Hotel", pos_profile="", privilege="split_check", role="URY Cashier", may_perform=1, may_authorise=0),
            Row(company="Lakeside Hotel", pos_profile="", privilege="split_check", role="URY Manager", may_perform=1, may_authorise=1),
        ]
        self.enterContext(patch.object(self.order, "_enforce_order_access"))
        self.enterContext(patch.object(self.order, "_split_kots_to_move", return_value=[]))
        self.frappe.generate_hash.return_value = "GRP-R2-SPLIT"

        # Case A: Self-escalation 3.75% (<= 5% cashier limit, no approver) -> 15% on remaining check
        src_a = self._register_check(
            FakePOSInvoice(
                "CHK-R2-SP-A",
                currency="UGX",
                items=[
                    _item(
                        "i_steak", "STEAK", 1, 25500,
                        price_list_rate=30000, base_price_list_rate=30000,
                        discount_percentage=15, discount_amount=4500, amount=25500,
                    ),
                    _item("i_platter", "PLATTER", 1, 90000),
                ],
                custom_discount_reason="Complaint",
                ury_discount_meta=json.dumps({"reason": "Complaint", "reason_description": "Complaint"}),
            )
        )
        child_a = FakePOSInvoice("CHK-R2-SP-A-C", currency="UGX", items=[])
        self.frappe.new_doc.return_value = child_a

        with self.assertRaises((Refused, NotPermitted)) as ctx_a:
            self.order.split_bill("CHK-R2-SP-A", [{"name": "i_platter", "qty": 1}])
        self.assertIn("POS-AUTH-REQUIRED", str(ctx_a.exception))
        self.assertFalse(
            [e for e in child_a.events if e[0] in ("insert", "save")],
            "Blocked split must not insert child invoice",
        )
        self.assertFalse(
            [e for e in src_a.events if e[0] in ("insert", "save")],
            "Blocked split must not save source invoice",
        )

        # Re-attempt Case A with valid Manager PIN token covering 15% -> succeeds cleanly
        src_a_ok = self._register_check(
            FakePOSInvoice(
                "CHK-R2-SP-A2",
                currency="UGX",
                items=[
                    _item(
                        "i_steak", "STEAK", 1, 25500,
                        price_list_rate=30000, base_price_list_rate=30000,
                        discount_percentage=15, discount_amount=4500, amount=25500,
                    ),
                    _item("i_platter", "PLATTER", 1, 90000),
                ],
                custom_discount_reason="Complaint",
                ury_discount_meta=json.dumps({"reason": "Complaint", "reason_description": "Complaint"}),
            )
        )
        child_a_ok = FakePOSInvoice("CHK-R2-SP-A2-C", currency="UGX", items=[])
        self.frappe.new_doc.return_value = child_a_ok
        split_claim_a = self.discount.canonical_split_discount_claim(
            "CHK-R2-SP-A2", [{"name": "i_platter", "qty": 1}], percent=15.0, reason="Complaint"
        )
        tok_a = self._issue_override_token(src_a_ok, split_claim_a, approver="manager@example.com", percent=15.0)
        self.order.split_bill("CHK-R2-SP-A2", [{"name": "i_platter", "qty": 1}], approval=tok_a)
        self.assertEqual(src_a_ok.flags.discount_approval["approver"], "manager@example.com")
        self.assertEqual(src_a_ok.flags.discount_approval["percent"], 15.0)
        self.assertEqual(src_a_ok.custom_discount_approver, "manager@example.com")
        self.assertEqual(src_a_ok.custom_discount_approved_percent, 15.0)

        # Case B: Inflation above prior approved % (15% approved -> 20% on remaining check)
        src_b = self._register_check(
            FakePOSInvoice(
                "CHK-R2-SP-B",
                currency="UGX",
                items=[
                    _item(
                        "i_steak", "STEAK", 1, 24000,
                        price_list_rate=30000, base_price_list_rate=30000,
                        discount_percentage=20, discount_amount=6000, amount=24000,
                    ),
                    _item("i_juice", "JUICE", 1, 10000),
                ],
                custom_discount_reason="Complaint",
                custom_discount_approver="manager@example.com",
                custom_discount_approved_percent=15.0,
                ury_discount_meta=json.dumps({
                    "reason": "Complaint",
                    "approver": "manager@example.com",
                    "approved_percent": 15.0,
                }),
            )
        )
        child_b = FakePOSInvoice("CHK-R2-SP-B-C", currency="UGX", items=[])
        self.frappe.new_doc.return_value = child_b

        with self.assertRaises((Refused, NotPermitted)) as ctx_b:
            self.order.split_bill("CHK-R2-SP-B", [{"name": "i_juice", "qty": 1}])
        self.assertIn("POS-AUTH-REQUIRED", str(ctx_b.exception))
        self.assertFalse([e for e in child_b.events if e[0] in ("insert", "save")])
        self.assertFalse([e for e in src_b.events if e[0] in ("insert", "save")])

        # Case C: Inflation above prior approved % on the MOVED new split check (moving STEAK 20%)
        src_c = self._register_check(
            FakePOSInvoice(
                "CHK-R2-SP-C",
                currency="UGX",
                items=[
                    _item(
                        "i_steak", "STEAK", 1, 24000,
                        price_list_rate=30000, base_price_list_rate=30000,
                        discount_percentage=20, discount_amount=6000, amount=24000,
                    ),
                    _item("i_juice", "JUICE", 1, 10000),
                ],
                custom_discount_reason="Complaint",
                custom_discount_approver="manager@example.com",
                custom_discount_approved_percent=15.0,
                ury_discount_meta=json.dumps({
                    "reason": "Complaint",
                    "approver": "manager@example.com",
                    "approved_percent": 15.0,
                }),
            )
        )
        child_c = FakePOSInvoice("CHK-R2-SP-C-C", currency="UGX", items=[])
        self.frappe.new_doc.return_value = child_c

        with self.assertRaises((Refused, NotPermitted)) as ctx_c:
            self.order.split_bill("CHK-R2-SP-C", [{"name": "i_steak", "qty": 1}])
        self.assertIn("POS-AUTH-REQUIRED", str(ctx_c.exception))

        # Re-attempt Case C with valid Manager PIN token covering 20% -> succeeds cleanly
        src_c_ok = self._register_check(
            FakePOSInvoice(
                "CHK-R2-SP-C2",
                currency="UGX",
                items=[
                    _item(
                        "i_steak", "STEAK", 1, 24000,
                        price_list_rate=30000, base_price_list_rate=30000,
                        discount_percentage=20, discount_amount=6000, amount=24000,
                    ),
                    _item("i_juice", "JUICE", 1, 10000),
                ],
                custom_discount_reason="Complaint",
                custom_discount_approver="manager@example.com",
                custom_discount_approved_percent=15.0,
                ury_discount_meta=json.dumps({
                    "reason": "Complaint",
                    "approver": "manager@example.com",
                    "approved_percent": 15.0,
                }),
            )
        )
        child_c_ok = FakePOSInvoice("CHK-R2-SP-C2-C", currency="UGX", items=[])
        self.frappe.new_doc.return_value = child_c_ok
        split_claim_c = self.discount.canonical_split_discount_claim(
            "CHK-R2-SP-C2", [{"name": "i_steak", "qty": 1}], percent=20.0, reason="Complaint"
        )
        tok_c = self._issue_override_token(src_c_ok, split_claim_c, approver="manager@example.com", percent=20.0)
        self.order.split_bill("CHK-R2-SP-C2", [{"name": "i_steak", "qty": 1}], approval=tok_c)
        self.assertEqual(child_c_ok.flags.discount_approval["approver"], "manager@example.com")
        self.assertEqual(child_c_ok.flags.discount_approval["percent"], 20.0)
        self.assertEqual(child_c_ok.custom_discount_approver, "manager@example.com")
        self.assertEqual(child_c_ok.custom_discount_approved_percent, 20.0)

    def test_r3_p1_approved_percentage_check_discount_allows_adding_item_while_blocking_forged_raise(self):
        """R3 Finding 2: adding an item to an already-approved 10% check scales discount_amount cleanly without SITEOPS-DISC-007."""
        prev = FakePOSInvoice(
            "CHK-R3-PCT",
            currency="UGX",
            items=[_item("r1", "STEAK", 1, 35000)],
            additional_discount_percentage=10.0,
            ury_discount_meta=json.dumps({
                "reason": "Corporate rate",
                "reason_description": "Corporate rate",
                "approver": "manager@example.com",
                "approved_percent": 10.0,
            }, sort_keys=True),
        )
        prev.calculate_taxes_and_totals()
        self.assertEqual(prev.discount_amount, 3500.0)

        # 1) Legitimate edit: cashier adds an undiscounted item (10,000) -> 10% of 45,000 = 4,500 -> MUST PASS!
        added = FakePOSInvoice(
            "CHK-R3-PCT",
            currency="UGX",
            items=[
                _item("r1", "STEAK", 1, 35000),
                _item("r2", "JUICE", 1, 10000),
            ],
            additional_discount_percentage=10.0,
            ury_discount_meta=prev.ury_discount_meta,
        )
        added._doc_before_save = prev
        added.calculate_taxes_and_totals()
        self.assertEqual(added.discount_amount, 4500.0)
        self.hook.validate_pos_discount(added)  # must not raise SITEOPS-DISC-007!

        # 2) Malicious raise: bumping additional_discount_percentage from 10% -> 15% MUST still raise SITEOPS-DISC-007!
        raised_pct = FakePOSInvoice(
            "CHK-R3-PCT",
            currency="UGX",
            items=[
                _item("r1", "STEAK", 1, 35000),
                _item("r2", "JUICE", 1, 10000),
            ],
            additional_discount_percentage=15.0,
            ury_discount_meta=prev.ury_discount_meta,
        )
        raised_pct._doc_before_save = prev
        raised_pct.calculate_taxes_and_totals()
        with self.assertRaises((Refused, NotPermitted)):
            self.hook.validate_pos_discount(raised_pct)

        # 3) Malicious flat raise: on a flat-discounted check (additional_discount_percentage=0, 3,500 off),
        #    bumping discount_amount to 4,500 while adding an item MUST still raise SITEOPS-DISC-007!
        prev_flat = FakePOSInvoice(
            "CHK-R3-FLAT",
            currency="UGX",
            items=[_item("r1", "STEAK", 1, 35000)],
            additional_discount_percentage=0.0,
            discount_amount=3500.0,
            ury_discount_meta=prev.ury_discount_meta,
        )
        prev_flat.calculate_taxes_and_totals()
        raised_flat = FakePOSInvoice(
            "CHK-R3-FLAT",
            currency="UGX",
            items=[
                _item("r1", "STEAK", 1, 35000),
                _item("r2", "JUICE", 1, 10000),
            ],
            additional_discount_percentage=0.0,
            discount_amount=4500.0,
            ury_discount_meta=prev.ury_discount_meta,
        )
        raised_flat._doc_before_save = prev_flat
        raised_flat.calculate_taxes_and_totals()
        with self.assertRaises((Refused, NotPermitted)):
            self.hook.validate_pos_discount(raised_flat)


if __name__ == "__main__":
    unittest.main()
