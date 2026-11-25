"""Scope e privileged pricing controls for POS (Oracle Simphony & SAP POS 1:1 parity).

Covers matrix rows 11–15 and 30 (tracker G5, G19 part, G34):
- Price override on an open check line (`override_line_price`, privilege `price_override`)
- Open item & zero-price item (`add_open_item`, privilege `open_item`)
- Complimentary / house / non-revenue check or item (`apply_complimentary`, privilege `complimentary`)
- Tax exempt per check or item via native Tax Category / Tax Rule / Item Tax Template
  (`apply_tax_exemption`, `clear_tax_exemption`, privilege `tax_exempt`)
- Service charge auto-apply, waive, and change (`sync_service_charge`, `adjust_service_charge`,
  privilege `service_charge_waive`)
- Post-close tip adjustment on #145's Tips Payable liability model
  (`adjust_closed_check_tip`, privilege `tip_adjust`)
- Mutation-site validation hook (`validate_privileged_pricing_mutations`) so REST, Desk,
  offline replay, and old clients meet the exact same server rules.
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import json
import math
from typing import Any

import frappe
from frappe import _

try:
    from frappe.utils import cint, flt
except ImportError:  # pragma: no cover - partial frappe stubs in unrelated unit tests
    def cint(val, default=0):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return default

    def flt(val, precision=None):
        try:
            num = float(val or 0)
        except (TypeError, ValueError):
            num = 0.0
        return round(num, int(precision)) if precision is not None else num


_ZERO_DECIMAL_CURRENCIES = frozenset({"UGX", "JPY", "KRW", "VND", "CLP", "RWF", "BIF", "TZS", "XAF", "XOF"})
_THREE_DECIMAL_CURRENCIES = frozenset({"KWD", "BHD", "OMR", "JOD", "TND", "LYD", "IQD"})

try:
    from ury.ury_pos.discount import (
        currency_precision as _disc_currency_precision,
        validate_manual_line_discount_cap,
        with_currency_precision as _disc_with_currency_precision,
    )
except ImportError:  # pragma: no cover - site-less test stub without discount module
    _disc_currency_precision = None
    validate_manual_line_discount_cap = None
    _disc_with_currency_precision = None


def _currency_precision(doc=None, currency: str | None = None) -> int:
    if _disc_currency_precision is not None:
        try:
            return _disc_currency_precision(doc, currency)
        except Exception:
            pass
    code = (currency or (doc.get("currency") if doc is not None and callable(getattr(doc, "get", None)) else getattr(doc, "currency", None)) or "").strip().upper()
    if code in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in _THREE_DECIMAL_CURRENCIES:
        return 3
    if doc is not None and callable(getattr(doc, "precision", None)):
        try:
            p = doc.precision("grand_total")
            if isinstance(p, int) and not isinstance(p, bool) and p >= 0:
                return p
        except Exception:
            pass
    return 2


@contextmanager
def _with_currency_precision(doc, prec: int | None = None):
    if _disc_with_currency_precision is not None:
        with _disc_with_currency_precision(doc, prec):
            yield
        return
    yield


def _nowdate() -> str:
    fn = getattr(getattr(frappe, "utils", None), "nowdate", None)
    if callable(fn):
        return fn()
    import datetime as _dt

    return _dt.date.today().isoformat()


def _getdate(val=None):
    fn = getattr(getattr(frappe, "utils", None), "getdate", None)
    if callable(fn):
        return fn(val)
    import datetime as _dt

    if isinstance(val, _dt.date):
        return val
    return _dt.date.fromisoformat(str(val or _nowdate())[:10])

from ury.brand import coded_message
from ury.ury_pos.approval_currency import (
    company_approval_amount,
    invoice_approval_currency,
    profile_approval_currency,
)
from ury.ury_pos.privileged_auth import (
    DEFAULT_MANAGER_ROLES,
    assert_not_efris_fiscalised,
    assert_not_offline_replay,
    authorize_or_perform,
)

ALLOWED_COMP_TYPES = frozenset({"Complimentary", "House", "Non-Revenue"})
ALLOWED_EFRIS_EXEMPT_CODES = frozenset({"02", "03", "04"})


def _parse_finite_float(value: Any, code: str, message: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        frappe.throw(coded_message(code, _(message)), frappe.ValidationError)
    if not math.isfinite(num):
        frappe.throw(coded_message(code, _(message)), frappe.ValidationError)
    return num


def _load_invoice_and_profile(invoice_name: str, pos_profile_name: str | None = None, *, for_update: bool = True):
    if not invoice_name:
        frappe.throw(coded_message("POS-INVOICE-REQUIRED", _("Invoice is required.")), frappe.ValidationError)
    doc = frappe.get_doc("POS Invoice", invoice_name, for_update=for_update)
    if not frappe.has_permission("POS Invoice", "write", doc=doc):
        frappe.throw(_("Not permitted to modify this check."), frappe.PermissionError)
    effective_profile = pos_profile_name or doc.get("pos_profile")
    if not effective_profile:
        frappe.throw(_("POS Profile is required."), frappe.ValidationError)
    if doc.get("pos_profile") and effective_profile != doc.get("pos_profile"):
        frappe.throw(_("Use the check's own POS Profile."), frappe.PermissionError)
    profile = frappe.get_doc("POS Profile", effective_profile)
    return doc, profile


def _resolve_item_cost(item_code: str, warehouse: str | None, buying_price_list: str | None = None) -> float:
    """Resolve best available unit cost for below-cost override checks (Bin -> Item -> Buying Price List)."""
    if warehouse:
        bin_rate = flt(
            frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate")
        )
        if bin_rate > 0:
            return bin_rate
    item_vals = frappe.db.get_value("Item", item_code, ["valuation_rate", "last_purchase_rate"], as_dict=True)
    if isinstance(item_vals, dict):
        if flt(item_vals.get("valuation_rate")) > 0:
            return flt(item_vals.get("valuation_rate"))
        if flt(item_vals.get("last_purchase_rate")) > 0:
            return flt(item_vals.get("last_purchase_rate"))
    if buying_price_list:
        buy_rate = flt(
            frappe.db.get_value("Item Price", {"item_code": item_code, "price_list": buying_price_list}, "price_list_rate")
        )
        if buy_rate > 0:
            return buy_rate
    return 0.0


def _is_room_charge_mode(mode_of_payment: str | None, company: str | None) -> bool:
    if not mode_of_payment:
        return False
    has_col = getattr(getattr(frappe, "db", None), "has_column", None)
    can_query_clearing = bool(company) and (not callable(has_col) or bool(has_col("Company", "custom_guest_ledger_clearing")))
    clearing = frappe.db.get_value("Company", company, "custom_guest_ledger_clearing") if can_query_clearing else None
    if clearing:
        acct = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": mode_of_payment, "company": company},
            "default_account",
        )
        if acct and acct == clearing:
            return True
    return "room charge" in str(mode_of_payment).casefold()


def _invoice_has_room_charge(doc) -> bool:
    if (doc.get("room_number") or "").strip():
        return True
    if (doc.get("custom_room_folio") or doc.get("kamra_ref") or ""):
        return True
    company = doc.get("company")
    for row in doc.get("payments") or []:
        if flt(row.get("amount")) > 0 and _is_room_charge_mode(row.get("mode_of_payment"), company):
            return True
    return False


def _save_verified(doc) -> None:
    doc.flags.ury_privileged_action_verified = True
    try:
        doc.save()
    finally:
        doc.flags.ury_privileged_action_verified = False


def _insert_verified(doc) -> None:
    doc.flags.ury_privileged_action_verified = True
    try:
        doc.insert()
    finally:
        doc.flags.ury_privileged_action_verified = False


# =====================================================================
# E1: Price Override (Matrix Row 11, privilege `price_override`)
# =====================================================================


def build_price_override_claim(
    *,
    invoice_name: str,
    item_row_name: str,
    item_code: str,
    original_rate: float,
    new_rate: float,
    reason: str,
    reason_note: str = "",
    below_cost: bool = False,
    item_cost: float = 0.0,
) -> dict[str, Any]:
    delta = round(abs(flt(new_rate) - flt(original_rate)), 4)
    pct = round((delta / flt(original_rate) * 100.0), 4) if flt(original_rate) > 0 else 100.0
    return {
        "action": "price_override",
        "invoice": invoice_name,
        "item_row_name": item_row_name,
        "item_code": item_code,
        "original_rate": round(flt(original_rate), 4),
        "new_rate": round(flt(new_rate), 4),
        "amount": delta,
        "percent": pct,
        "below_cost": bool(below_cost),
        "item_cost": round(flt(item_cost), 4),
        "reason": (reason or "").strip(),
        "note": (reason_note or "").strip(),
    }


@frappe.whitelist(methods=["POST"])
def override_line_price(
    invoice: str,
    pos_profile: str | None = None,
    item_row_name: str | None = None,
    new_rate: float | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    approval_token: str | None = None,
) -> dict[str, Any]:
    """Override a line's unit rate on an open draft POS Invoice.

    Respects ERPNext native `POS Profile.allow_rate_change`, rejects negative
    rates unconditionally, escalates below-cost overrides to Manager PIN, and
    requires a second person when overriding a priced item to 0.00.
    """
    assert_not_offline_replay()
    rate_val = _parse_finite_float(new_rate, "PRICE-INVALID", "Enter a valid price.")
    if rate_val < 0:
        frappe.throw(
            coded_message("PRICE-NEGATIVE", _("Item price cannot be negative.")),
            frappe.ValidationError,
        )

    doc, profile = _load_invoice_and_profile(invoice, pos_profile, for_update=True)
    if cint(doc.get("docstatus")) != 0:
        frappe.throw(
            coded_message("PRICE-CHECK-CLOSED", _("Prices can only be overridden on an open check.")),
            frappe.ValidationError,
        )
    assert_not_efris_fiscalised(doc)

    if not cint(profile.get("allow_rate_change")):
        frappe.throw(
            coded_message(
                "PRICE-OVERRIDE-DISABLED",
                _("Price overrides are disabled for POS Profile {0}.").format(profile.name),
            ),
            frappe.PermissionError,
        )

    target_row = next((r for r in (doc.get("items") or []) if r.get("name") == item_row_name), None)
    if target_row is None:
        frappe.throw(
            coded_message("PRICE-ROW-NOT-FOUND", _("Selected line item was not found on this check.")),
            frappe.ValidationError,
        )

    prec = _doc_precision(doc, "rate")
    rate_val = flt(rate_val, prec)
    approval_currency = invoice_approval_currency(doc, frappe)
    base_rate_val = flt(rate_val * float(approval_currency[1]))

    original_rate = flt(
        target_row.get("custom_original_price_list_rate")
        or target_row.get("price_list_rate")
        or target_row.get("rate")
    )
    warehouse = target_row.get("warehouse") or profile.get("warehouse")
    item_cost = _resolve_item_cost(
        target_row.get("item_code"),
        warehouse,
        profile.get("buying_price_list"),
    )
    is_zero_override = original_rate > 0 and rate_val == 0
    # item_cost (Bin.valuation_rate / Item.valuation_rate) is in company currency;
    # compare against company-currency base_rate_val so USD/EUR/KWD checks never false-trigger below_cost.
    below_cost = item_cost > 0 and base_rate_val < item_cost
    delta = abs(Decimal(str(rate_val)) - Decimal(str(original_rate)))
    pct = (float(delta) / original_rate * 100.0) if original_rate > 0 else 100.0

    claim = build_price_override_claim(
        invoice_name=doc.name,
        item_row_name=item_row_name,
        item_code=target_row.get("item_code"),
        original_rate=original_rate,
        new_rate=rate_val,
        reason=reason or "",
        reason_note=reason_note or "",
        below_cost=below_cost,
        item_cost=item_cost,
    )

    auth = authorize_or_perform(
        privilege="price_override",
        pos_profile=profile.name,
        reference_doctype="POS Invoice",
        reference_name=doc.name,
        claim=claim,
        token=approval_token,
        percent=pct,
        amount=company_approval_amount(
            doc, delta * abs(Decimal(str(flt(target_row.get("qty"))))), frappe,
            currency_and_rate=approval_currency,
        ),
        reason=reason,
        reason_note=reason_note,
        require_reason=True,
        fallback_roles=DEFAULT_MANAGER_ROLES,
        always_second_person=is_zero_override,
        force_pin=below_cost,
    )

    # Preserve original released list price for audit/reporting; set price_list_rate = rate
    # and clear any prior Pricing Rule / line discount so subsequent bill/line discounts
    # and native returns apply cleanly to the manager-agreed rate without double-discounting.
    if not target_row.get("custom_original_price_list_rate"):
        target_row.custom_original_price_list_rate = original_rate
    target_row.rate = rate_val
    target_row.price_list_rate = rate_val
    target_row.base_rate = base_rate_val
    target_row.base_price_list_rate = base_rate_val
    target_row.discount_percentage = 0
    target_row.discount_amount = 0
    target_row.margin_rate_or_amount = 0
    target_row.pricing_rules = None
    target_row.ignore_pricing_rule = 1
    target_row.custom_price_overridden = 1
    if isinstance(doc.get("pricing_rules"), list) and item_row_name:
        doc.set(
            "pricing_rules",
            [pr for pr in doc.get("pricing_rules") if pr.get("child_docname") != item_row_name],
        )
    target_row.custom_price_override_reason = (
        f"{auth['reason']}: {auth['reason_note']}" if auth["reason_note"] else auth["reason"]
    )
    target_row.custom_price_override_approver = auth["approver"]

    with _with_currency_precision(doc, prec):
        sync_service_charge(doc, profile)
        doc.calculate_taxes_and_totals()
        _save_verified(doc)

    return {
        "invoice": doc.name,
        "item_row_name": item_row_name,
        "original_rate": original_rate,
        "rate": rate_val,
        "new_rate": rate_val,
        "below_cost": below_cost,
        "item_cost": item_cost,
        "approver": auth["approver"],
        "via_pin": auth["via_pin"],
        "grand_total": flt(doc.get("grand_total")),
    }


# =====================================================================
# E2: Open Item & Zero-Price Item (Matrix Row 12, privilege `open_item`)
# =====================================================================


def build_open_item_claim(
    *,
    invoice_name: str,
    item_code: str,
    rate: float,
    qty: float,
    reference_name_text: str = "",
    reason: str = "",
    reason_note: str = "",
) -> dict[str, Any]:
    line_total = round(flt(rate) * flt(qty), 4)
    return {
        "action": "open_item",
        "invoice": invoice_name,
        "item_code": item_code,
        "rate": round(flt(rate), 4),
        "qty": round(flt(qty), 4),
        "amount": line_total,
        "reference": (reference_name_text or "").strip(),
        "reason": (reason or "").strip(),
        "note": (reason_note or "").strip(),
    }


@frappe.whitelist(methods=["POST"])
def add_open_item(
    invoice: str | None = None,
    pos_profile: str | None = None,
    item_code: str | None = None,
    rate: float | None = None,
    qty: float = 1.0,
    reference_name_text: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    approval_token: str | None = None,
    reference: str | None = None,
    order_type: str | None = None,
    table: str | None = None,
    customer: str | None = None,
    no_of_pax: int | None = None,
) -> dict[str, Any]:
    """Add an open-priced (market price / catch weight / special) or zero-price item."""
    assert_not_offline_replay()
    if reference_name_text is None and reference is not None:
        reference_name_text = reference
    rate_val = _parse_finite_float(rate, "PRICE-INVALID", "Enter a valid price.")
    qty_val = _parse_finite_float(qty, "QTY-INVALID", "Enter a valid quantity.")
    if rate_val < 0:
        frappe.throw(
            coded_message("PRICE-NEGATIVE", _("Item price cannot be negative.")),
            frappe.ValidationError,
        )
    if qty_val <= 0:
        frappe.throw(
            coded_message("QTY-INVALID", _("Quantity must be greater than zero.")),
            frappe.ValidationError,
        )

    is_new_invoice = not (invoice and str(invoice).strip())
    if not is_new_invoice:
        doc, profile = _load_invoice_and_profile(invoice, pos_profile, for_update=True)
        if cint(doc.get("docstatus")) != 0:
            frappe.throw(
                coded_message("OPEN-ITEM-CHECK-CLOSED", _("Items can only be added to an open check.")),
                frappe.ValidationError,
            )
        assert_not_efris_fiscalised(doc)
    else:
        if not pos_profile:
            frappe.throw(coded_message("POS-INVOICE-REQUIRED", _("Invoice or POS Profile is required.")), frappe.ValidationError)
        if not frappe.has_permission("POS Invoice", "create"):
            frappe.throw(_("Not permitted to create a check."), frappe.PermissionError)
        profile = frappe.get_doc("POS Profile", pos_profile)
        resolved_order_type = (order_type or ("Dine In" if table else "Take Away")).strip()
        resolved_customer = (customer or "").strip() or (profile.get("customer") or "").strip()
        if not resolved_customer:
            try:
                from ury.ury_pos.default_customer import resolve_default_customer

                resolved_customer = resolve_default_customer(profile, resolved_order_type) or ""
            except Exception:
                resolved_customer = ""
        if not resolved_customer:
            frappe.throw(_("Customer is required to open a check."), frappe.ValidationError)
        default_tpl = None
        if profile.get("restaurant"):
            default_tpl = frappe.db.get_value("URY Restaurant", profile.get("restaurant"), "default_tax_template")
        if not default_tpl:
            default_tpl = profile.get("taxes_and_charges")
        doc = frappe.new_doc("POS Invoice")
        doc.is_pos = 1
        doc.pos_profile = profile.name
        doc.company = profile.get("company")
        doc.branch = profile.get("branch")
        doc.restaurant = profile.get("restaurant")
        doc.customer = resolved_customer
        doc.order_type = resolved_order_type
        if table:
            doc.restaurant_table = table
        doc.no_of_pax = max(1, cint(no_of_pax or 1))
        doc.waiter = frappe.session.user
        doc.cashier = frappe.session.user
        doc.currency = profile.get("currency")
        doc.selling_price_list = profile.get("selling_price_list")
        if default_tpl:
            doc.taxes_and_charges = default_tpl
        if callable(getattr(doc, "set_missing_values", None)):
            try:
                doc.set_missing_values()
            except Exception:
                pass

    item_doc = frappe.get_doc("Item", item_code)
    is_open_priced = cint(item_doc.get("custom_is_open_priced")) == 1
    allow_zero = cint(item_doc.get("custom_allow_zero_price")) == 1
    if not is_open_priced and not allow_zero:
        frappe.throw(
            coded_message(
                "OPEN-ITEM-NOT-CONFIGURED",
                _("Item {0} is not configured as an open-priced or zero-price item.").format(item_code),
            ),
            frappe.ValidationError,
        )

    prec = _doc_precision(doc, "rate")
    rate_val = flt(rate_val, prec)
    if rate_val == 0 and not allow_zero:
        frappe.throw(
            coded_message("OPEN-ITEM-PRICE-REQUIRED", _("Enter a positive price for this open item.")),
            frappe.ValidationError,
        )

    ref_text = (reference_name_text or "").strip()
    if is_open_priced and cint(item_doc.get("custom_open_item_require_reference")) == 1 and not ref_text:
        frappe.throw(
            coded_message(
                "OPEN-ITEM-REFERENCE-REQUIRED",
                _("Enter a dish name or reference for this open item."),
            ),
            frappe.ValidationError,
        )

    halo_max = flt(item_doc.get("custom_halo_max_price"))
    min_price = flt(item_doc.get("custom_min_price"))
    priv_group = cint(item_doc.get("custom_privilege_group"))
    item_cost = _resolve_item_cost(item_code, profile.get("warehouse"), profile.get("buying_price_list"))
    if is_new_invoice:
        approval_currency = profile_approval_currency(profile, frappe, posting_date=doc.get("posting_date"))
        doc.conversion_rate = float(approval_currency[1])
    else:
        approval_currency = invoice_approval_currency(doc, frappe)
    base_rate_val = flt(rate_val * float(approval_currency[1]))

    over_halo = halo_max > 0 and base_rate_val > halo_max
    below_min = min_price > 0 and base_rate_val < min_price and not (rate_val == 0 and allow_zero)
    below_cost = item_cost > 0 and base_rate_val < item_cost and not (rate_val == 0 and allow_zero)
    line_total = Decimal(str(rate_val)) * Decimal(str(qty_val))

    ref_name = doc.get("name") or "NEW"
    claim = build_open_item_claim(
        invoice_name=ref_name,
        item_code=item_code,
        rate=rate_val,
        qty=qty_val,
        reference_name_text=ref_text,
        reason=reason or "",
        reason_note=reason_note or "",
    )

    auth = authorize_or_perform(
        privilege="open_item",
        pos_profile=profile.name,
        reference_doctype="POS Invoice",
        reference_name=ref_name,
        claim=claim,
        token=approval_token,
        amount=company_approval_amount(doc, line_total, frappe, currency_and_rate=approval_currency),
        reason=reason,
        reason_note=reason_note,
        require_reason=None,
        fallback_roles=DEFAULT_MANAGER_ROLES | ({"URY Cashier", "URY Captain"} if priv_group == 0 and not (over_halo or below_min or below_cost) else set()),
        force_pin=bool(over_halo or below_min or below_cost),
        required_privilege_group=priv_group,
    )

    base_name = item_doc.get("item_name") or item_code
    display_name = f"{base_name} - {ref_text}" if ref_text else base_name
    row = doc.append(
        "items",
        {
            "item_code": item_code,
            "item_name": display_name,
            "description": ref_text or item_doc.get("description") or base_name,
            "comment": ref_text,
            "qty": qty_val,
            "rate": rate_val,
            "price_list_rate": rate_val,
            "base_rate": base_rate_val,
            "base_price_list_rate": base_rate_val,
            "cost_center": profile.get("cost_center"),
            "warehouse": profile.get("warehouse"),
            "custom_is_open_item": 1,
            "custom_open_item_reference": ref_text,
            "custom_price_override_approver": auth["approver"],
        },
    )

    with _with_currency_precision(doc, prec):
        sync_service_charge(doc, profile)
        doc.calculate_taxes_and_totals()
        if is_new_invoice:
            _insert_verified(doc)
            if table:
                try:
                    frappe.db.set_value("URY Table", table, {"occupied": 1, "latest_invoice_time": doc.get("creation")})
                except Exception:
                    pass
        else:
            _save_verified(doc)

    return {
        "invoice": doc.name,
        "item_row_name": row.get("name"),
        "item_code": item_code,
        "item_name": display_name,
        "rate": rate_val,
        "qty": qty_val,
        "below_cost": below_cost,
        "approver": auth["approver"],
        "via_pin": auth["via_pin"],
        "grand_total": flt(doc.get("grand_total")),
    }


# =====================================================================
# E3: Complimentary / House / Non-Revenue (Matrix Row 13, `complimentary`)
# =====================================================================


def build_complimentary_claim(
    *,
    invoice_name: str,
    scope: str,
    item_row_name: str | None,
    comp_type: str,
    amount: float,
    reason: str,
    reason_note: str = "",
) -> dict[str, Any]:
    return {
        "action": "complimentary",
        "invoice": invoice_name,
        "scope": scope,
        "item_row_name": item_row_name or "",
        "comp_type": comp_type,
        "amount": round(flt(amount), 4),
        "percent": 100.0 if scope == "check" else 0.0,
        "reason": (reason or "").strip(),
        "note": (reason_note or "").strip(),
    }


@frappe.whitelist(methods=["POST"])
def apply_complimentary(
    invoice: str,
    pos_profile: str | None = None,
    scope: str = "check",
    item_row_name: str | None = None,
    comp_type: str = "Complimentary",
    reason: str | None = None,
    reason_note: str | None = None,
    approval_token: str | None = None,
) -> dict[str, Any]:
    """Apply a Complimentary / House / Non-Revenue comp to a check or single line.

    A full check comp (or an item comp that brings the check to zero) ALWAYS
    requires a second person's manager PIN. Comping a room-charge check is
    strictly refused.
    """
    assert_not_offline_replay()
    if item_row_name and scope == "check":
        scope = "item"
    if scope not in ("check", "item"):
        frappe.throw(coded_message("COMP-SCOPE-INVALID", _("Select Check or Item scope.")), frappe.ValidationError)
    if comp_type not in ALLOWED_COMP_TYPES:
        frappe.throw(
            coded_message("COMP-TYPE-INVALID", _("Choose Complimentary, House, or Non-Revenue.")),
            frappe.ValidationError,
        )

    doc, profile = _load_invoice_and_profile(invoice, pos_profile, for_update=True)
    if cint(doc.get("docstatus")) != 0:
        frappe.throw(
            coded_message("COMP-CHECK-CLOSED", _("Only an open check can be marked complimentary.")),
            frappe.ValidationError,
        )
    assert_not_efris_fiscalised(doc)

    if _invoice_has_room_charge(doc):
        frappe.throw(
            coded_message(
                "COMP-ROOM-CHARGE-FORBIDDEN",
                _("A room-charge check cannot be comped at the till. Adjust it on the guest folio or remove the room charge first."),
            ),
            frappe.ValidationError,
        )

    target_row = None
    if scope == "item":
        target_row = next((r for r in (doc.get("items") or []) if r.get("name") == item_row_name), None)
        if target_row is None:
            frappe.throw(
                coded_message("COMP-ROW-NOT-FOUND", _("Selected line item was not found on this check.")),
                frappe.ValidationError,
            )
        comp_amount = flt(target_row.get("amount")) or (flt(target_row.get("rate")) * flt(target_row.get("qty")))
        remaining_other = sum(
            flt(r.get("amount")) for r in (doc.get("items") or []) if r.get("name") != item_row_name
        )
        becomes_full_comp = remaining_other <= 0
    else:
        comp_amount = flt(doc.get("rounded_total") or doc.get("grand_total") or doc.get("total"))
        becomes_full_comp = True

    claim = build_complimentary_claim(
        invoice_name=doc.name,
        scope=scope,
        item_row_name=item_row_name if scope == "item" else None,
        comp_type=comp_type,
        amount=comp_amount,
        reason=reason or "",
        reason_note=reason_note or "",
    )

    auth = authorize_or_perform(
        privilege="complimentary",
        pos_profile=profile.name,
        reference_doctype="POS Invoice",
        reference_name=doc.name,
        claim=claim,
        token=approval_token,
        percent=100.0 if becomes_full_comp else None,
        amount=company_approval_amount(doc, comp_amount, frappe),
        reason=reason,
        reason_note=reason_note,
        require_reason=True,
        always_second_person=True,
    )

    full_reason = f"{auth['reason']}: {auth['reason_note']}" if auth["reason_note"] else auth["reason"]
    if scope == "item" and target_row is not None:
        if not target_row.get("custom_original_price_list_rate"):
            target_row.custom_original_price_list_rate = flt(target_row.get("price_list_rate") or target_row.get("rate"))
        target_row.discount_percentage = 100.0
        target_row.rate = 0.0
        target_row.custom_is_complimentary = 1
        target_row.custom_comp_type = comp_type
        target_row.custom_comp_reason = full_reason
        target_row.custom_comp_approver = auth["approver"]
        if becomes_full_comp:
            doc.custom_is_complimentary = 1
            doc.custom_comp_type = comp_type
            doc.custom_comp_reason = full_reason
            doc.custom_comp_approver = auth["approver"]
    else:
        doc.additional_discount_percentage = 100.0
        doc.discount_amount = 0.0
        doc.custom_is_complimentary = 1
        doc.custom_comp_type = comp_type
        doc.custom_comp_reason = full_reason
        doc.custom_comp_approver = auth["approver"]
        doc.remarks = f"{comp_type}: {full_reason}"

    sync_service_charge(doc, profile)
    doc.calculate_taxes_and_totals()
    _save_verified(doc)

    return {
        "invoice": doc.name,
        "scope": scope,
        "comp_type": comp_type,
        "approver": auth["approver"],
        "via_pin": auth["via_pin"],
        "grand_total": flt(doc.get("grand_total")),
    }


# =====================================================================
# E4: Tax Exempt via Native Tax Category & Tax Rule (Matrix Row 15)
# =====================================================================


def build_tax_exempt_claim(
    *,
    invoice_name: str,
    scope: str,
    item_row_name: str | None,
    tax_category: str | None,
    item_tax_template: str | None,
    certificate_reference: str,
    efris_tax_category: str,
    reason: str,
    reason_note: str = "",
) -> dict[str, Any]:
    return {
        "action": "tax_exempt",
        "invoice": invoice_name,
        "scope": scope,
        "item_row_name": item_row_name or "",
        "tax_category": (tax_category or "").strip(),
        "item_tax_template": (item_tax_template or "").strip(),
        "certificate_reference": (certificate_reference or "").strip(),
        "efris_tax_category": (efris_tax_category or "03").strip(),
        "reason": (reason or "").strip(),
        "note": (reason_note or "").strip(),
    }


def _resolve_native_tax_rule_template(company: str, tax_category: str) -> str | None:
    """Resolve Sales Taxes and Charges Template strictly from ERPNext native Tax Rule / Tax Category."""
    if not tax_category or not frappe.db.exists("Tax Category", tax_category):
        return None
    if frappe.db.table_exists("Tax Rule"):
        rules = frappe.get_all(
            "Tax Rule",
            filters={
                "tax_type": "Sales",
                "company": company,
                "tax_category": tax_category,
            },
            fields=["name", "sales_tax_template", "priority", "from_date", "to_date"],
            order_by="priority desc",
        )
        today = _getdate(_nowdate())
        for rule in rules or []:
            tpl = rule.get("sales_tax_template")
            if not tpl:
                continue
            from_d = rule.get("from_date")
            to_d = rule.get("to_date")
            if from_d and _getdate(from_d) > today:
                continue
            if to_d and _getdate(to_d) < today:
                continue
            return tpl
    # Fallback to a native Sales Taxes and Charges Template bound directly to this Tax Category
    return frappe.db.get_value(
        "Sales Taxes and Charges Template",
        {"tax_category": tax_category, "company": company},
        "name",
    )


def _load_template_tax_rows(template_name: str) -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "Sales Taxes and Charges",
        filters={"parent": template_name, "parenttype": "Sales Taxes and Charges Template"},
        fields=["charge_type", "account_head", "description", "rate", "tax_amount", "cost_center", "included_in_print_rate"],
        order_by="idx asc",
    )
    return [dict(r) for r in (rows or [])]


def _resolve_efris_code(tax_category: str | None, item_tax_template: str | None, requested: str | None) -> str:
    if requested and str(requested).strip() in ALLOWED_EFRIS_EXEMPT_CODES:
        return str(requested).strip()
    if tax_category:
        code = frappe.db.get_value("Tax Category", tax_category, "custom_efris_tax_category")
        if code and str(code).strip() in ALLOWED_EFRIS_EXEMPT_CODES:
            return str(code).strip()
    if item_tax_template:
        code = frappe.db.get_value("Item Tax Template", item_tax_template, "custom_efris_tax_category")
        if code and str(code).strip() in ALLOWED_EFRIS_EXEMPT_CODES:
            return str(code).strip()
    return "03"  # URA EFRIS "03" = Exempt ("02" = Zero-rated, "04" = Deemed)


@frappe.whitelist(methods=["POST"])
def apply_tax_exemption(
    invoice: str,
    pos_profile: str | None = None,
    scope: str = "check",
    item_row_name: str | None = None,
    tax_category: str | None = None,
    item_tax_template: str | None = None,
    certificate_reference: str | None = None,
    efris_tax_category: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    approval_token: str | None = None,
    certificate: str | None = None,
) -> dict[str, Any]:
    """Apply tax exemption via native Tax Category + Tax Rule or Item Tax Template.

    Never hand-zeroes a VAT row. Records certificate/reference, customer lock,
    and EFRIS tax-category mapping (`02` Zero-rated / `03` Exempt / `04` Deemed).
    """
    assert_not_offline_replay()
    if certificate_reference is None and certificate is not None:
        certificate_reference = certificate
    if item_row_name and scope == "check":
        scope = "item"
    if scope not in ("check", "item"):
        frappe.throw(coded_message("TAX-EXEMPT-SCOPE-INVALID", _("Select Check or Item scope.")), frappe.ValidationError)

    cert = (certificate_reference or "").strip()
    if len(cert) < 3:
        frappe.throw(
            coded_message(
                "TAX-EXEMPT-CERT-REQUIRED",
                _("Enter the tax exemption certificate or diplomatic/NGO reference (at least 3 characters)."),
            ),
            frappe.ValidationError,
        )

    doc, profile = _load_invoice_and_profile(invoice, pos_profile, for_update=True)
    if cint(doc.get("docstatus")) != 0:
        frappe.throw(
            coded_message("TAX-EXEMPT-CHECK-CLOSED", _("Tax exemption can only be applied to an open check.")),
            frappe.ValidationError,
        )
    assert_not_efris_fiscalised(doc)

    resolved_template = None
    resolved_category = (tax_category or profile.get("custom_tax_exempt_category") or "").strip()
    resolved_item_tpl = (item_tax_template or profile.get("custom_exempt_item_tax_template") or "").strip()
    target_row = None

    if scope == "check":
        resolved_template = _resolve_native_tax_rule_template(doc.company, resolved_category)
        if not resolved_template:
            frappe.throw(
                coded_message(
                    "TAX-EXEMPT-NO-RULE",
                    _(
                        "No native Sales Tax Rule or Tax Template is configured for Tax Category '{0}' in {1}. "
                        "Configure a Tax Category and Tax Rule instead of zeroing tax rows."
                    ).format(resolved_category or _("(none)"), doc.company),
                ),
                frappe.ValidationError,
            )
    else:
        target_row = next((r for r in (doc.get("items") or []) if r.get("name") == item_row_name), None)
        if target_row is None:
            frappe.throw(
                coded_message("TAX-EXEMPT-ROW-NOT-FOUND", _("Selected line item was not found on this check.")),
                frappe.ValidationError,
            )
        if not resolved_item_tpl or not frappe.db.exists("Item Tax Template", resolved_item_tpl):
            frappe.throw(
                coded_message(
                    "TAX-EXEMPT-NO-RULE",
                    _("Select a valid native Item Tax Template for item-level tax exemption."),
                ),
                frappe.ValidationError,
            )

    efris_code = _resolve_efris_code(
        resolved_category if scope == "check" else None,
        resolved_item_tpl if scope == "item" else None,
        efris_tax_category,
    )

    claim = build_tax_exempt_claim(
        invoice_name=doc.name,
        scope=scope,
        item_row_name=item_row_name if scope == "item" else None,
        tax_category=resolved_category if scope == "check" else None,
        item_tax_template=resolved_item_tpl if scope == "item" else None,
        certificate_reference=cert,
        efris_tax_category=efris_code,
        reason=reason or "",
        reason_note=reason_note or "",
    )

    auth = authorize_or_perform(
        privilege="tax_exempt",
        pos_profile=profile.name,
        reference_doctype="POS Invoice",
        reference_name=doc.name,
        claim=claim,
        token=approval_token,
        reason=reason,
        reason_note=reason_note,
        require_reason=True,
        fallback_roles=DEFAULT_MANAGER_ROLES,
    )

    full_reason = f"{auth['reason']}: {auth['reason_note']}" if auth["reason_note"] else auth["reason"]

    # Preserve Charged Tip rows while switching tax template; Service Charge & Service Charge Tax
    # are rebuilt cleanly by sync_service_charge() below (dropping SC Tax on exempt checks).
    tip_account = (profile.get("custom_tips_payable_account") or "").strip()

    if scope == "check":
        preserved_tips = [
            r
            for r in (doc.get("taxes") or [])
            if r.get("custom_pos_charge_kind") == "Charged Tip"
            or (tip_account and r.get("account_head") == tip_account)
        ]
        template_rows = _load_template_tax_rows(resolved_template)
        doc.tax_category = resolved_category
        doc.taxes_and_charges = resolved_template
        doc.set("taxes", template_rows + preserved_tips)
    else:
        details = frappe.get_all(
            "Item Tax Template Detail",
            filters={"parent": resolved_item_tpl, "parenttype": "Item Tax Template"},
            fields=["tax_type", "tax_rate"],
        )
        rate_map = {d.get("tax_type"): flt(d.get("tax_rate")) for d in (details or []) if d.get("tax_type")}
        target_row.item_tax_template = resolved_item_tpl
        target_row.item_tax_rate = json.dumps(rate_map)
        target_row.custom_efris_tax_category = efris_code

    doc.custom_tax_exempt = 1
    doc.custom_tax_exempt_reason = full_reason
    doc.custom_tax_exempt_certificate = cert
    doc.custom_tax_exempt_approver = auth["approver"]
    doc.custom_tax_exempt_customer = doc.get("customer") or ""
    doc.custom_efris_tax_category = efris_code

    sync_service_charge(doc, profile)
    doc.calculate_taxes_and_totals()
    _save_verified(doc)

    return {
        "invoice": doc.name,
        "scope": scope,
        "tax_category": doc.get("tax_category"),
        "taxes_and_charges": doc.get("taxes_and_charges"),
        "item_tax_template": resolved_item_tpl if scope == "item" else None,
        "certificate_reference": cert,
        "efris_tax_category": efris_code,
        "approver": auth["approver"],
        "via_pin": auth["via_pin"],
        "total_taxes_and_charges": flt(doc.get("total_taxes_and_charges")),
        "grand_total": flt(doc.get("grand_total")),
    }


@frappe.whitelist(methods=["POST"])
def clear_tax_exemption(
    invoice: str,
    pos_profile: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    approval_token: str | None = None,
) -> dict[str, Any]:
    """Revert a draft check's tax exemption back to the outlet's default tax template."""
    assert_not_offline_replay()
    doc, profile = _load_invoice_and_profile(invoice, pos_profile, for_update=True)
    if cint(doc.get("docstatus")) != 0:
        frappe.throw(
            coded_message("TAX-EXEMPT-CHECK-CLOSED", _("Tax exemption can only be cleared on an open check.")),
            frappe.ValidationError,
        )
    assert_not_efris_fiscalised(doc)

    default_tpl = None
    if doc.get("restaurant"):
        default_tpl = frappe.db.get_value("URY Restaurant", doc.restaurant, "default_tax_template")
    if not default_tpl:
        default_tpl = profile.get("taxes_and_charges")

    tip_account = (profile.get("custom_tips_payable_account") or "").strip()
    preserved_tips = [
        r
        for r in (doc.get("taxes") or [])
        if r.get("custom_pos_charge_kind") == "Charged Tip"
        or (tip_account and r.get("account_head") == tip_account)
    ]

    doc.tax_category = ""
    doc.taxes_and_charges = default_tpl or ""
    doc.set("taxes", (_load_template_tax_rows(default_tpl) if default_tpl else []) + preserved_tips)
    for row in doc.get("items") or []:
        if row.get("item_tax_template"):
            row.item_tax_template = ""
            row.item_tax_rate = "{}"
            row.custom_efris_tax_category = ""

    doc.custom_tax_exempt = 0
    doc.custom_tax_exempt_reason = ""
    doc.custom_tax_exempt_certificate = ""
    doc.custom_tax_exempt_approver = ""
    doc.custom_tax_exempt_customer = ""
    doc.custom_efris_tax_category = ""

    sync_service_charge(doc, profile)
    doc.calculate_taxes_and_totals()
    _save_verified(doc)

    return {
        "invoice": doc.name,
        "taxes_and_charges": doc.get("taxes_and_charges"),
        "total_taxes_and_charges": flt(doc.get("total_taxes_and_charges")),
        "grand_total": flt(doc.get("grand_total")),
    }


@frappe.whitelist(methods=["GET"])
def list_tax_exemption_options(
    pos_profile: str | None = None,
    company: str | None = None,
) -> dict[str, Any]:
    """List native Tax Categories with Sales Tax Rules and Item Tax Templates for this till's company."""
    resolved_company = company
    if pos_profile:
        profile = frappe.get_doc("POS Profile", pos_profile)
        resolved_company = profile.company
    if not resolved_company:
        frappe.throw(_("POS Profile or Company is required."), frappe.ValidationError)
    company = resolved_company
    categories = []
    if frappe.db.table_exists("Tax Rule"):
        rules = frappe.get_all(
            "Tax Rule",
            filters={"tax_type": "Sales", "company": company},
            fields=["tax_category", "sales_tax_template"],
        )
        seen = set()
        for r in rules or []:
            tc = (r.get("tax_category") or "").strip()
            if tc and tc not in seen:
                seen.add(tc)
                efris_code = _resolve_efris_code(tc, None, None)
                categories.append({
                    "name": tc,
                    "tax_category": tc,
                    "sales_tax_template": r.get("sales_tax_template"),
                    "efris_tax_category": efris_code,
                    "custom_efris_tax_category": efris_code,
                })
    item_templates = []
    if frappe.db.table_exists("Item Tax Template"):
        for row in frappe.get_all("Item Tax Template", filters={"company": company, "disabled": 0}, fields=["name", "title"]):
            efris_code = _resolve_efris_code(None, row.get("name"), None)
            item_templates.append({
                "name": row.get("name"),
                "title": row.get("title") or row.get("name"),
                "efris_tax_category": efris_code,
                "custom_efris_tax_category": efris_code,
            })
    return {"tax_categories": categories, "item_tax_templates": item_templates}


# =====================================================================
# E5: Service Charge Rules, Pre/Post-Discount, Taxable Chain & Waive (G19)
# =====================================================================


def _clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _set_row_attr(row: Any, key: str, value: Any) -> None:
    if isinstance(row, dict):
        row[key] = value
    else:
        setattr(row, key, value)


def _doc_precision(doc, fieldname: str = "total_taxes_and_charges") -> int:
    code = (
        (doc.get("currency") if doc is not None and callable(getattr(doc, "get", None)) else getattr(doc, "currency", None))
        or ""
    ).strip().upper()
    if code in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in _THREE_DECIMAL_CURRENCIES:
        return 3
    fn = getattr(doc, "precision", None)
    if callable(fn):
        try:
            p = fn(fieldname)
            if isinstance(p, int) and not isinstance(p, bool) and p >= 0:
                return p
        except Exception:
            pass
    return _currency_precision(doc)


def _effective_tax_row_amount(doc, row: Any) -> float:
    """Return effective post-discount tax/charge amount (matching GL) with site-less fallback."""
    get_r = row.get if callable(getattr(row, "get", None)) else (lambda k, d=None: getattr(row, k, d))
    raw = flt(get_r("tax_amount"))
    after = get_r("tax_amount_after_discount_amount")
    if after is not None:
        after_val = flt(after)
        has_discount = bool(
            doc is not None
            and (flt(doc.get("discount_amount")) != 0 or flt(doc.get("additional_discount_percentage")) != 0)
        )
        if after_val != 0 or has_discount or raw == 0:
            return after_val
    return raw


def resolve_service_charge_rules(profile) -> list[dict[str, Any]]:
    """Return normalized enabled Service Charge rules from POS Profile.

    When `POS Profile.custom_service_charge_rules` is empty, synthesizes
    backward-compatible Percentage rule(s) from #152's scalar fields
    (`custom_service_charge_*`). When child rows exist (even if all disabled),
    only the child table governs.
    """
    if profile is None:
        return []
    raw_rows = profile.get("custom_service_charge_rules") if callable(getattr(profile, "get", None)) else getattr(profile, "custom_service_charge_rules", None)
    if isinstance(raw_rows, (list, tuple)) and len(raw_rows) > 0:
        out: list[dict[str, Any]] = []
        for row in raw_rows:
            get_f = row.get if callable(getattr(row, "get", None)) else (lambda k, d=None: getattr(row, k, d))
            enabled_raw = get_f("enabled")
            if enabled_raw is not None and cint(enabled_raw) != 1:
                continue
            acct = _clean_str(get_f("account_head"))
            order_type = _clean_str(get_f("order_type"))
            if not acct or not order_type:
                continue
            charge_mode = _clean_str(get_f("charge_mode")) or "Percentage"
            rate = flt(get_f("rate"))
            fixed_amount = flt(get_f("fixed_amount"))
            if charge_mode == "Fixed Amount":
                if fixed_amount <= 0:
                    continue
                calc_basis = "Fixed Amount"
            else:
                charge_mode = "Percentage"
                if rate <= 0:
                    continue
                calc_basis = _clean_str(get_f("calculation_basis")) or "Post-Discount Subtotal"
            out.append(
                {
                    "order_type": order_type,
                    "enabled": 1,
                    "charge_mode": charge_mode,
                    "rate": rate,
                    "fixed_amount": fixed_amount,
                    "min_amount_floor": max(0.0, flt(get_f("min_amount_floor"))),
                    "min_pax": max(0, cint(get_f("min_pax"))),
                    "calculation_basis": calc_basis,
                    "taxable": 1 if cint(get_f("taxable")) == 1 else 0,
                    "account_head": acct,
                    "description": _clean_str(get_f("description")) or _("Service Charge"),
                    "efris_item_code": _clean_str(get_f("efris_item_code")),
                }
            )
        return out

    # Legacy scalar fallback when custom_service_charge_rules table is empty
    get_p = profile.get if callable(getattr(profile, "get", None)) else (lambda k, d=None: getattr(profile, k, d))
    legacy_rate = flt(get_p("custom_service_charge_rate"))
    legacy_account = _clean_str(get_p("custom_service_charge_account"))
    if legacy_rate <= 0 or not legacy_account:
        return []

    raw_types = _clean_str(get_p("custom_service_charge_order_types")) or "Dine In"
    order_types = [
        part.strip()
        for line in raw_types.splitlines()
        for part in line.split(",")
        if part.strip()
    ] or ["Dine In"]
    min_pax = max(0, cint(get_p("custom_service_charge_min_pax")))
    taxable = 1 if cint(get_p("custom_service_charge_taxable")) == 1 else 0
    desc = _clean_str(get_p("custom_service_charge_description")) or _("Service Charge")
    return [
        {
            "order_type": ot,
            "enabled": 1,
            "charge_mode": "Percentage",
            "rate": legacy_rate,
            "fixed_amount": 0.0,
            "min_amount_floor": 0.0,
            "min_pax": min_pax,
            "calculation_basis": "Post-Discount Subtotal",
            "taxable": taxable,
            "account_head": legacy_account,
            "description": desc,
            "efris_item_code": "",
        }
        for ot in order_types
    ]


def match_service_charge_rule(doc, profile) -> dict[str, Any] | None:
    """Select the highest-precedence enabled Service Charge rule for (order_type, no_of_pax).

    Precedence: exact `order_type` match beats `*` wildcard; among matches with equal
    specificity, the highest qualifying `min_pax` wins.
    """
    rules = resolve_service_charge_rules(profile)
    if not rules:
        return None
    order_type = _clean_str(doc.get("order_type")).casefold()
    pax = cint(doc.get("no_of_pax"))

    exact_matches: list[dict[str, Any]] = []
    wildcard_matches: list[dict[str, Any]] = []
    for rule in rules:
        if pax < cint(rule["min_pax"]):
            continue
        r_ot = rule["order_type"].casefold()
        if r_ot == order_type and order_type:
            exact_matches.append(rule)
        elif r_ot == "*":
            wildcard_matches.append(rule)

    pool = exact_matches or wildcard_matches
    if not pool:
        return None
    pool.sort(key=lambda r: cint(r["min_pax"]), reverse=True)
    return pool[0]


def _service_charge_applies(doc, profile) -> bool:
    return match_service_charge_rule(doc, profile) is not None


def _all_service_charge_accounts(profile) -> set[str]:
    accounts: set[str] = set()
    if profile is None:
        return accounts
    get_p = profile.get if callable(getattr(profile, "get", None)) else (lambda k, d=None: getattr(profile, k, d))
    legacy = _clean_str(get_p("custom_service_charge_account"))
    if legacy:
        accounts.add(legacy)
    raw_rows = get_p("custom_service_charge_rules")
    if isinstance(raw_rows, (list, tuple)):
        for row in raw_rows:
            get_f = row.get if callable(getattr(row, "get", None)) else (lambda k, d=None: getattr(row, k, d))
            acct = _clean_str(get_f("account_head"))
            if acct:
                accounts.add(acct)
    return accounts


def _resolve_override_rule(doc, profile, matched_rule: dict[str, Any] | None) -> dict[str, Any] | None:
    if matched_rule is not None:
        return matched_rule
    rules = resolve_service_charge_rules(profile)
    order_type = _clean_str(doc.get("order_type")).casefold()
    for rule in rules:
        if rule["order_type"].casefold() == order_type:
            return rule
    if rules:
        return rules[0]
    get_p = profile.get if callable(getattr(profile, "get", None)) else (lambda k, d=None: getattr(profile, k, d))
    legacy_account = _clean_str(get_p("custom_service_charge_account"))
    if not legacy_account:
        return None
    return {
        "order_type": _clean_str(doc.get("order_type")) or "*",
        "enabled": 1,
        "charge_mode": "Percentage",
        "rate": flt(get_p("custom_service_charge_rate")),
        "fixed_amount": 0.0,
        "min_amount_floor": 0.0,
        "min_pax": 0,
        "calculation_basis": "Post-Discount Subtotal",
        "taxable": 1 if cint(get_p("custom_service_charge_taxable")) == 1 else 0,
        "account_head": legacy_account,
        "description": _clean_str(get_p("custom_service_charge_description")) or _("Service Charge"),
        "efris_item_code": "",
    }


def _partition_tax_rows(doc, profile) -> tuple[list[Any], list[Any]]:
    """Split existing doc.taxes into (base_item_taxes, tip_rows), stripping old SC & SC Tax rows."""
    taxes = list(doc.get("taxes") or [])
    sc_accounts = _all_service_charge_accounts(profile)
    get_p = profile.get if callable(getattr(profile, "get", None)) else (lambda k, d=None: getattr(profile, k, d))
    tip_account = _clean_str(get_p("custom_tips_payable_account"))

    old_sc_indices: set[str] = set()
    for pos, row in enumerate(taxes, start=1):
        kind = _clean_str(row.get("custom_pos_charge_kind"))
        acct = _clean_str(row.get("account_head"))
        idx_val = str(cint(row.get("idx")) or pos)
        if kind == "Service Charge" or (not kind and acct and acct in sc_accounts):
            old_sc_indices.add(idx_val)

    base_rows: list[Any] = []
    tip_rows: list[Any] = []
    for pos, row in enumerate(taxes, start=1):
        kind = _clean_str(row.get("custom_pos_charge_kind"))
        acct = _clean_str(row.get("account_head"))
        idx_val = str(cint(row.get("idx")) or pos)
        if kind == "Service Charge" or kind == "Service Charge Tax":
            continue
        if not kind and idx_val in old_sc_indices:
            continue
        if (
            not kind
            and row.get("charge_type") == "On Previous Row Amount"
            and str(row.get("row_id") or "").strip() in old_sc_indices
        ):
            continue
        if kind == "Charged Tip" or (tip_account and acct == tip_account):
            _set_row_attr(row, "custom_pos_charge_kind", "Charged Tip")
            tip_rows.append(row)
            continue
        base_rows.append(row)

    return base_rows, tip_rows


def _billable_items(doc) -> list[Any]:
    out = []
    for row in doc.get("items") or []:
        if cint(row.get("is_disposable")) == 1 or cint(row.get("custom_is_complimentary")) == 1:
            continue
        qty = flt(row.get("qty"))
        rate = flt(row.get("rate"))
        if qty > 0 and rate > 0:
            out.append(row)
    return out


def _compute_pre_discount_net(doc, base_tax_rows: list[Any], prec: int) -> float:
    """Compute item net subtotal BEFORE any header additional discount (exclusive & inclusive safe)."""
    items = _billable_items(doc)
    if not items:
        return 0.0

    incl_rate_sum = sum(
        flt(t.get("rate"))
        for t in base_tax_rows
        if cint(t.get("included_in_print_rate")) == 1 and flt(t.get("rate")) > 0
    )
    has_header_discount = flt(doc.get("additional_discount_percentage")) > 0 or flt(doc.get("discount_amount")) > 0

    total_net = 0.0
    for row in items:
        gross = flt(row.get("qty")) * flt(row.get("rate"))
        if not has_header_discount and flt(row.get("net_amount")) > 0:
            total_net += flt(row.get("net_amount"))
            continue
        # Handle tax-inclusive line item (checking item_tax_rate JSON if present)
        line_incl_rate = incl_rate_sum
        raw_itr = row.get("item_tax_rate")
        if raw_itr and isinstance(raw_itr, str) and raw_itr.strip() not in ("", "{}"):
            try:
                parsed = json.loads(raw_itr)
                if isinstance(parsed, dict) and parsed:
                    incl_accts = {
                        _clean_str(t.get("account_head"))
                        for t in base_tax_rows
                        if cint(t.get("included_in_print_rate")) == 1
                    }
                    if incl_accts:
                        matched_rates = [flt(v) for k, v in parsed.items() if k in incl_accts]
                        if matched_rates:
                            line_incl_rate = sum(matched_rates)
            except Exception:
                pass
        if line_incl_rate > 0:
            total_net += gross / (1.0 + line_incl_rate / 100.0)
        else:
            total_net += gross
    return flt(total_net, prec)


def _resolve_effective_vat(doc, base_tax_rows: list[Any]) -> tuple[str, float, str] | None:
    """Find the active output VAT/GST account, rate, and short label on the check."""
    if cint(doc.get("custom_tax_exempt")) == 1:
        return None

    def _is_vat_like(row) -> bool:
        txt = f"{row.get('description') or ''} {row.get('account_head') or ''}".casefold()
        return any(tok in txt for tok in ("vat", "gst", "tax"))

    candidates = [r for r in base_tax_rows if _clean_str(r.get("account_head"))]
    # Prefer VAT/GST-named percentage rows first, then any percentage row
    ordered = sorted(candidates, key=lambda r: (0 if _is_vat_like(r) else 1))
    for row in ordered:
        acct = _clean_str(row.get("account_head"))
        rate = flt(row.get("rate"))
        if rate <= 0:
            # ERPNext item-wise tax templates keep header row rate=0 and put rates in item.item_tax_rate JSON
            item_rates: list[float] = []
            for item in _billable_items(doc):
                raw_itr = item.get("item_tax_rate")
                if raw_itr and isinstance(raw_itr, str):
                    try:
                        parsed = json.loads(raw_itr)
                        if isinstance(parsed, dict) and acct in parsed and flt(parsed[acct]) > 0:
                            item_rates.append(flt(parsed[acct]))
                    except Exception:
                        pass
            if item_rates:
                rate = max(item_rates)
        if rate > 0:
            label = _clean_str(row.get("description")) or _("VAT")
            return acct, rate, label
    return None


def _normalize_tax_indices(doc) -> None:
    for idx, row in enumerate(doc.get("taxes") or [], start=1):
        _set_row_attr(row, "idx", idx)


def sync_service_charge(doc, profile=None) -> None:
    """Keep native Sales Taxes and Charges Service Charge (+ optional Taxable chain) in sync.

    Always normalizes `doc.taxes` order to:
        `[base_item_taxes..., Service Charge (idx S), Service Charge Tax (idx S+1, row_id=S)?, Charged Tip...]`
    so `row_id` never shifts across tip updates, discounts, order-type switches, splits, or merges.
    """
    if not doc or cint(doc.get("is_return")) == 1:
        return
    if profile is None:
        prof_name = _clean_str(doc.get("pos_profile"))
        if not prof_name:
            return
        try:
            if hasattr(frappe, "db") and callable(getattr(frappe.db, "exists", None)):
                if not frappe.db.exists("POS Profile", prof_name):
                    return
            profile = frappe.get_doc("POS Profile", prof_name)
        except Exception:
            return
        # Guard site-less unit tests where frappe.get_doc is an unconfigured MagicMock
        if not isinstance(profile, dict) and not isinstance(getattr(profile, "name", None), str):
            return

    base_rows, tip_rows = _partition_tax_rows(doc, profile)

    # On a brand-new check (e.g. sync_order or direct Desk insert before set_missing_values),
    # doc.taxes may still be empty even though taxes_and_charges is set. Hydrate template VAT
    # rows first so ERPNext set_taxes() is never suppressed and taxable SC can chain via row_id.
    if not base_rows and cint(doc.get("custom_tax_exempt")) != 1:
        get_p = profile.get if callable(getattr(profile, "get", None)) else (lambda k, d=None: getattr(profile, k, d))
        tpl_name = _clean_str(doc.get("taxes_and_charges")) or _clean_str(get_p("taxes_and_charges"))
        if tpl_name:
            try:
                base_rows = _load_template_tax_rows(tpl_name)
                if base_rows and not _clean_str(doc.get("taxes_and_charges")):
                    doc.taxes_and_charges = tpl_name
            except Exception:
                base_rows = []

    if cint(doc.get("custom_service_charge_waived")) == 1 or cint(doc.get("custom_is_complimentary")) == 1:
        doc.set("taxes", base_rows + tip_rows)
        _normalize_tax_indices(doc)
        return

    override_rate = doc.get("custom_service_charge_override_rate")
    override_amount = doc.get("custom_service_charge_override_amount")
    # MariaDB DECIMAL(21,9) NOT NULL DEFAULT 0.0 loads unset Percent/Currency fields as 0.0;
    # only strictly positive values represent an active override (zeroing uses waive).
    has_override_rate = flt(override_rate) > 0
    has_override_amount = flt(override_amount) > 0
    has_override = has_override_rate or has_override_amount

    matched_rule = match_service_charge_rule(doc, profile)
    effective_rule = _resolve_override_rule(doc, profile, matched_rule) if has_override else matched_rule

    if effective_rule is None:
        doc.set("taxes", base_rows + tip_rows)
        _normalize_tax_indices(doc)
        return

    billable = _billable_items(doc)
    if not billable and not has_override_amount:
        doc.set("taxes", base_rows + tip_rows)
        _normalize_tax_indices(doc)
        return

    prec = _doc_precision(doc, "total_taxes_and_charges")
    account = effective_rule["account_head"]
    desc = effective_rule["description"]
    taxable = cint(effective_rule.get("taxable")) == 1
    cost_center = (
        (profile.get("cost_center") if callable(getattr(profile, "get", None)) else getattr(profile, "cost_center", None))
        or doc.get("cost_center")
    )

    pre_disc_net = _compute_pre_discount_net(doc, base_rows, prec)

    if has_override_amount and not has_override_rate:
        sc_row = {
            "charge_type": "Actual",
            "account_head": account,
            "description": desc,
            "rate": 0.0,
            "tax_amount": flt(override_amount, prec),
            "cost_center": cost_center,
            "included_in_print_rate": 0,
            "custom_pos_charge_kind": "Service Charge",
        }
    elif not has_override and effective_rule["charge_mode"] == "Fixed Amount":
        sc_row = {
            "charge_type": "Actual",
            "account_head": account,
            "description": desc,
            "rate": 0.0,
            "tax_amount": flt(effective_rule["fixed_amount"], prec),
            "cost_center": cost_center,
            "included_in_print_rate": 0,
            "custom_pos_charge_kind": "Service Charge",
        }
    else:
        eff_rate = flt(override_rate) if has_override_rate else flt(effective_rule["rate"])
        calc_basis = effective_rule.get("calculation_basis") or "Post-Discount Subtotal"
        floor_amt = 0.0 if has_override else flt(effective_rule.get("min_amount_floor"), prec)

        if calc_basis == "Pre-Discount Subtotal":
            computed_amt = flt(pre_disc_net * eff_rate / 100.0, prec)
            if floor_amt > 0 and computed_amt < floor_amt:
                computed_amt = floor_amt
            sc_row = {
                "charge_type": "Actual",
                "account_head": account,
                "description": desc,
                "rate": 0.0,
                "tax_amount": computed_amt,
                "cost_center": cost_center,
                "included_in_print_rate": 0,
                "custom_pos_charge_kind": "Service Charge",
            }
        else:
            # Post-Discount Subtotal: On Net Total unless Option 17 floor binds on non-empty non-comp check
            disc_pct = flt(doc.get("additional_discount_percentage"))
            disc_amt = flt(doc.get("discount_amount"))
            if disc_pct > 0:
                est_post_net = flt(pre_disc_net * max(0.0, 1.0 - disc_pct / 100.0), prec)
            elif disc_amt > 0:
                est_post_net = flt(max(0.0, pre_disc_net - disc_amt), prec)
            else:
                est_post_net = pre_disc_net

            if floor_amt > 0 and flt(est_post_net * eff_rate / 100.0, prec) < floor_amt:
                sc_row = {
                    "charge_type": "Actual",
                    "account_head": account,
                    "description": desc,
                    "rate": 0.0,
                    "tax_amount": floor_amt,
                    "cost_center": cost_center,
                    "included_in_print_rate": 0,
                    "custom_pos_charge_kind": "Service Charge",
                }
            else:
                sc_row = {
                    "charge_type": "On Net Total",
                    "account_head": account,
                    "description": desc,
                    "rate": eff_rate,
                    "tax_amount": 0.0,
                    "cost_center": cost_center,
                    "included_in_print_rate": 0,
                    "custom_pos_charge_kind": "Service Charge",
                }

    sc_idx = len(base_rows) + 1
    appended = [sc_row]
    if taxable:
        vat_info = _resolve_effective_vat(doc, base_rows)
        if vat_info is not None:
            vat_account, vat_rate, vat_label = vat_info
            appended.append(
                {
                    "charge_type": "On Previous Row Amount",
                    "row_id": str(sc_idx),
                    "account_head": vat_account,
                    "rate": vat_rate,
                    "tax_amount": 0.0,
                    "description": f"{vat_label} on {desc}",
                    "cost_center": cost_center,
                    "included_in_print_rate": 0,
                    "custom_pos_charge_kind": "Service Charge Tax",
                }
            )

    doc.set("taxes", base_rows + appended + tip_rows)
    _normalize_tax_indices(doc)


def _sc_signature(inv_doc) -> list[tuple[str, str, float, float]]:
    rows = []
    for t in inv_doc.get("taxes") or []:
        if _clean_str(t.get("custom_pos_charge_kind")) == "Service Charge":
            rows.append(
                (
                    _clean_str(t.get("charge_type")),
                    _clean_str(t.get("account_head")),
                    round(flt(t.get("rate")), 4),
                    round(flt(t.get("tax_amount")), 4),
                )
            )
    return rows


def _sc_context_unchanged(doc, before) -> bool:
    before_items = [
        (r.get("item_code"), round(flt(r.get("qty")), 4), round(flt(r.get("rate")), 4))
        for r in (before.get("items") or [])
        if cint(r.get("is_disposable")) != 1
    ]
    curr_items = [
        (r.get("item_code"), round(flt(r.get("qty")), 4), round(flt(r.get("rate")), 4))
        for r in (doc.get("items") or [])
        if cint(r.get("is_disposable")) != 1
    ]
    return (
        _clean_str(doc.get("order_type")) == _clean_str(before.get("order_type"))
        and cint(doc.get("no_of_pax")) == cint(before.get("no_of_pax"))
        and _clean_str(doc.get("pos_profile")) == _clean_str(before.get("pos_profile"))
        and round(flt(doc.get("additional_discount_percentage")), 4) == round(flt(before.get("additional_discount_percentage")), 4)
        and round(flt(doc.get("discount_amount")), 4) == round(flt(before.get("discount_amount")), 4)
        and before_items == curr_items
    )


def _is_unverified_sc_tamper(doc, before) -> bool:
    """True when a direct REST/Desk save mutates SC flags or strips SC rows with unchanged context."""
    if before is None:
        return False
    if getattr(getattr(doc, "flags", None), "ury_privileged_action_verified", False):
        return False
    if cint(doc.get("custom_service_charge_waived")) != cint(before.get("custom_service_charge_waived")):
        return True
    for ov_field in ("custom_service_charge_override_rate", "custom_service_charge_override_amount"):
        if abs(flt(doc.get(ov_field)) - flt(before.get(ov_field))) > 1e-6:
            return True
    before_sc = _sc_signature(before)
    curr_sc = _sc_signature(doc)
    return bool(before_sc and before_sc != curr_sc and _sc_context_unchanged(doc, before))


def sync_service_charge_on_before_validate(doc) -> None:
    """Keep Service Charge in sync on direct Desk/REST saves (e.g. changing order_type/pax/items).

    Skips syncing if the payload looks like an unverified REST tamper (flag flip or stripping SC rows
    with unchanged context) so `validate_privileged_pricing_mutations` still refuses it with 403.
    """
    if not doc:
        return
    if getattr(frappe.flags, "in_patch", False) or getattr(frappe.flags, "in_migrate", False):
        return
    if cint(doc.get("is_return")) == 1 or cint(doc.get("docstatus")) != 0:
        return
    before = doc.get_doc_before_save() if callable(getattr(doc, "get_doc_before_save", None)) else None
    if _is_unverified_sc_tamper(doc, before):
        return
    sync_service_charge(doc)


def calculate_taxes_and_totals_for_service_charge(doc, calculate_fn):
    """Ensure partial-return SC proration and Grand-Total discount isolation around Actual SC rows.

    1. On return checks (`is_return == 1`), prorates `Actual` Service Charge BEFORE ERPNext totals
       are calculated so `grand_total` reflects the prorated refund prior to `POSInvoice.validate_pos()`.
    2. On non-return checks with a header discount (`additional_discount_percentage` or `discount_amount`)
       and an `Actual` Service Charge row (Pre-Discount percentage, Fixed Amount, floor-bound, or
       fixed override), detaches `Actual` SC + chained `Service Charge Tax` during Pass 1 so ERPNext's
       `set_discount_amount()` computes `discount_amount` on items + item VAT only (never inflating
       the discount base by Service Charge), then reattaches SC + SC Tax in canonical order for Pass 2.
    """
    if getattr(doc, "_ury_sc_calc_guard", False):
        return calculate_fn()

    if cint(doc.get("is_return")) == 1:
        doc._ury_sc_calc_guard = True
        try:
            prorate_return_service_charge(doc, recalc=False)
            return calculate_fn()
        finally:
            doc._ury_sc_calc_guard = False

    has_discount = bool(flt(doc.get("additional_discount_percentage")) or flt(doc.get("discount_amount")))
    if not has_discount:
        return calculate_fn()

    taxes = list(doc.get("taxes") or [])
    actual_sc_indices: set[str] = set()
    for pos, row in enumerate(taxes, start=1):
        if _clean_str(row.get("custom_pos_charge_kind")) == "Service Charge" and row.get("charge_type") == "Actual":
            actual_sc_indices.add(str(cint(row.get("idx")) or pos))
    if not actual_sc_indices:
        return calculate_fn()

    sc_rows: list[Any] = []
    base_rows: list[Any] = []
    tip_rows: list[Any] = []
    for pos, row in enumerate(taxes, start=1):
        kind = _clean_str(row.get("custom_pos_charge_kind"))
        idx_str = str(cint(row.get("idx")) or pos)
        if idx_str in actual_sc_indices:
            sc_rows.append(row)
        elif kind == "Service Charge Tax" or (
            row.get("charge_type") == "On Previous Row Amount"
            and str(row.get("row_id") or "").strip() in actual_sc_indices
        ):
            sc_rows.append(row)
        elif kind == "Charged Tip":
            tip_rows.append(row)
        else:
            base_rows.append(row)

    def _reattach_canonical(current_taxes: list[Any]) -> None:
        non_tip = [r for r in current_taxes if _clean_str(r.get("custom_pos_charge_kind")) != "Charged Tip"]
        tips = [r for r in current_taxes if _clean_str(r.get("custom_pos_charge_kind")) == "Charged Tip"]
        sc_idx = len(non_tip) + 1
        for r in sc_rows:
            if _clean_str(r.get("custom_pos_charge_kind")) == "Service Charge Tax" or r.get("charge_type") == "On Previous Row Amount":
                _set_row_attr(r, "row_id", str(sc_idx))
        combined = non_tip + sc_rows + tips
        doc.set("taxes", combined)
        _normalize_tax_indices(doc)

    saved_pct = doc.get("additional_discount_percentage")
    reattached = False
    doc._ury_sc_calc_guard = True
    try:
        pass1_taxes = base_rows + tip_rows
        doc.set("taxes", pass1_taxes)
        _normalize_tax_indices(doc)
        calculate_fn()

        computed_discount = doc.get("discount_amount")
        _reattach_canonical(list(doc.get("taxes") or []))
        reattached = True

        doc.additional_discount_percentage = 0
        doc.discount_amount = computed_discount
        return calculate_fn()
    finally:
        if not reattached:
            _reattach_canonical(list(doc.get("taxes") or []))
        doc.additional_discount_percentage = saved_pct
        doc._ury_sc_calc_guard = False


def prorate_return_service_charge(doc, *, recalc: bool = True) -> None:
    """Prorate Actual/Fixed Service Charge on partial returns and cap cumulative refunded SC at 100%."""
    if not doc or cint(doc.get("is_return")) != 1:
        return
    orig_name = _clean_str(doc.get("return_against"))
    if not orig_name:
        return
    try:
        orig = frappe.get_doc("POS Invoice", orig_name)
    except Exception:
        return

    prec = _doc_precision(doc, "total_taxes_and_charges")
    profile = None
    prof_name = _clean_str(doc.get("pos_profile") or orig.get("pos_profile"))
    if prof_name:
        try:
            profile = frappe.get_doc("POS Profile", prof_name)
        except Exception:
            profile = None
    sc_accounts = _all_service_charge_accounts(profile)

    def _is_sc_row(r) -> bool:
        kind = _clean_str(r.get("custom_pos_charge_kind"))
        if kind == "Service Charge":
            return True
        return not kind and _clean_str(r.get("account_head")) in sc_accounts

    def _is_sc_tax_row(r) -> bool:
        return _clean_str(r.get("custom_pos_charge_kind")) == "Service Charge Tax"

    # If the original check had Service Charge waived, a return must refund 0 SC
    if cint(orig.get("custom_service_charge_waived")) == 1:
        mutated = False
        for row in doc.get("taxes") or []:
            if _is_sc_row(row) or _is_sc_tax_row(row):
                if flt(row.get("tax_amount")) != 0 or flt(row.get("rate")) != 0:
                    _set_row_attr(row, "tax_amount", 0.0)
                    _set_row_attr(row, "rate", 0.0)
                    mutated = True
        if mutated and recalc and callable(getattr(doc, "calculate_taxes_and_totals", None)):
            doc.calculate_taxes_and_totals()
        return

    orig_sc_actual = sum(
        abs(flt(r.get("tax_amount")))
        for r in (orig.get("taxes") or [])
        if _is_sc_row(r) and r.get("charge_type") == "Actual"
    )
    if orig_sc_actual <= 0:
        return

    def _line_abs_total(r) -> float:
        qty = r.get("qty")
        rate = r.get("rate")
        if qty is not None and rate is not None:
            return abs(flt(qty) * flt(rate))
        return abs(flt(r.get("amount")))

    orig_items_total = sum(
        _line_abs_total(r)
        for r in (orig.get("items") or [])
        if cint(r.get("is_disposable")) != 1
    )
    ret_items_total = sum(
        _line_abs_total(r)
        for r in (doc.get("items") or [])
        if cint(r.get("is_disposable")) != 1
    )

    prior_returns = frappe.get_all(
        "POS Invoice",
        filters={
            "is_return": 1,
            "return_against": orig_name,
            "docstatus": 1,
            **({"name": ["!=", doc.name]} if doc.get("name") else {}),
        },
        pluck="name",
    ) or []
    prior_returned_sc = 0.0
    for r_name in prior_returns:
        try:
            prev_ret = r_name if isinstance(r_name, dict) else frappe.get_doc("POS Invoice", r_name)
        except Exception:
            continue
        prior_returned_sc += sum(
            abs(flt(t.get("tax_amount")))
            for t in (prev_ret.get("taxes") or [])
            if _is_sc_row(t) and t.get("charge_type") == "Actual"
        )

    remaining_sc = max(0.0, flt(orig_sc_actual - prior_returned_sc, prec))
    ratio = min(1.0, ret_items_total / orig_items_total) if orig_items_total > 0 else 0.0
    prorated = flt(orig_sc_actual * ratio, prec)
    target_refund = min(prorated, remaining_sc)

    mutated = False
    allocated = False
    for row in doc.get("taxes") or []:
        if _is_sc_row(row) and row.get("charge_type") == "Actual":
            desired = -target_refund if not allocated else 0.0
            allocated = True
            if abs(flt(row.get("tax_amount")) - desired) > 1e-6:
                _set_row_attr(row, "tax_amount", desired)
                mutated = True

    if mutated and recalc and callable(getattr(doc, "calculate_taxes_and_totals", None)):
        doc.calculate_taxes_and_totals()


@frappe.whitelist(methods=["GET"])
def current_service_charge_amount(doc) -> float:
    return sum(
        _effective_tax_row_amount(doc, row)
        for row in (doc.get("taxes") or [])
        if _clean_str(row.get("custom_pos_charge_kind")) == "Service Charge"
    )


def get_service_charge_status(invoice: str, pos_profile: str | None = None) -> dict[str, Any]:
    """Return active matched Service Charge rule, waived/override state, and current amounts for UI."""
    doc = frappe.get_doc("POS Invoice", invoice)
    if not frappe.has_permission("POS Invoice", "read", doc=doc):
        frappe.throw(_("Not permitted to view this check."), frappe.PermissionError)
    eff_profile = pos_profile or doc.get("pos_profile")
    profile = frappe.get_doc("POS Profile", eff_profile) if eff_profile else None
    matched = match_service_charge_rule(doc, profile) if profile else None
    sc_amount = current_service_charge_amount(doc)
    sc_tax_amount = sum(
        _effective_tax_row_amount(doc, t)
        for t in (doc.get("taxes") or [])
        if _clean_str(t.get("custom_pos_charge_kind")) == "Service Charge Tax"
    )
    raw_ov_rate = flt(doc.get("custom_service_charge_override_rate"))
    raw_ov_amount = flt(doc.get("custom_service_charge_override_amount"))
    return {
        "invoice": doc.name,
        "waived": cint(doc.get("custom_service_charge_waived")) == 1,
        "override_rate": raw_ov_rate if raw_ov_rate > 0 else None,
        "override_amount": raw_ov_amount if raw_ov_amount > 0 else None,
        "reason": doc.get("custom_service_charge_reason") or "",
        "approver": doc.get("custom_service_charge_approver") or "",
        "service_charge_amount": sc_amount,
        "service_charge_tax_amount": sc_tax_amount,
        "matched_rule": matched,
    }


def build_service_charge_claim(
    *,
    invoice_name: str,
    action: str,
    new_rate: float | None,
    new_amount: float | None,
    reason: str,
    reason_note: str = "",
) -> dict[str, Any]:
    return {
        "action": "service_charge_waive",
        "invoice": invoice_name,
        "sc_action": action,
        "new_rate": round(flt(new_rate), 4) if new_rate is not None else None,
        "new_amount": round(flt(new_amount), 4) if new_amount is not None else None,
        "reason": (reason or "").strip(),
        "note": (reason_note or "").strip(),
    }


@frappe.whitelist(methods=["POST"])
def adjust_service_charge(
    invoice: str,
    pos_profile: str | None = None,
    action: str | None = None,
    new_rate: float | None = None,
    new_amount: float | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    approval_token: str | None = None,
    mode: str | None = None,
    rate: float | None = None,
    amount: float | None = None,
) -> dict[str, Any]:
    """Waive, change rate/amount, or restore service charge on an open check."""
    assert_not_offline_replay()
    if action is None:
        if mode == "override":
            action = "change"
        elif mode is not None:
            action = mode
        else:
            action = "waive"
    if new_rate is None and rate is not None:
        new_rate = rate
    if new_amount is None and amount is not None:
        new_amount = amount
    if action not in ("waive", "change", "restore"):
        frappe.throw(
            coded_message("SERVICE-CHARGE-ACTION-INVALID", _("Choose waive, change, or restore.")),
            frappe.ValidationError,
        )

    rate_val = None
    amount_val = None
    if action == "change":
        if new_rate is not None and new_rate != "":
            rate_val = _parse_finite_float(new_rate, "SERVICE-CHARGE-INVALID", "Enter a valid service charge percentage.")
            if rate_val < 0:
                frappe.throw(
                    coded_message("SERVICE-CHARGE-NEGATIVE", _("Service charge rate cannot be negative.")),
                    frappe.ValidationError,
                )
        elif new_amount is not None and new_amount != "":
            amount_val = _parse_finite_float(new_amount, "SERVICE-CHARGE-INVALID", "Enter a valid service charge amount.")
            if amount_val < 0:
                frappe.throw(
                    coded_message("SERVICE-CHARGE-NEGATIVE", _("Service charge amount cannot be negative.")),
                    frappe.ValidationError,
                )
        else:
            frappe.throw(
                coded_message("SERVICE-CHARGE-INVALID", _("Enter a new service charge rate or amount.")),
                frappe.ValidationError,
            )

    doc, profile = _load_invoice_and_profile(invoice, pos_profile, for_update=True)
    if cint(doc.get("docstatus")) != 0:
        frappe.throw(
            coded_message("SERVICE-CHARGE-CHECK-CLOSED", _("Service charge can only be changed on an open check.")),
            frappe.ValidationError,
        )
    assert_not_efris_fiscalised(doc)

    approval_currency = invoice_approval_currency(doc, frappe)
    claim = build_service_charge_claim(
        invoice_name=doc.name,
        action=action,
        new_rate=rate_val,
        new_amount=amount_val,
        reason=reason or "",
        reason_note=reason_note or "",
    )

    approval_amount = current_service_charge_amount(doc) if action == "waive" else amount_val
    auth = authorize_or_perform(
        privilege="service_charge_waive",
        pos_profile=profile.name,
        reference_doctype="POS Invoice",
        reference_name=doc.name,
        claim=claim,
        token=approval_token,
        percent=rate_val,
        amount=(
            company_approval_amount(doc, approval_amount, frappe, currency_and_rate=approval_currency)
            if approval_amount is not None else None
        ),
        reason=reason,
        reason_note=reason_note,
        require_reason=(action != "restore"),
        fallback_roles=DEFAULT_MANAGER_ROLES,
    )

    full_reason = f"{auth['reason']}: {auth['reason_note']}" if auth["reason_note"] else auth["reason"]
    if action == "waive":
        doc.custom_service_charge_waived = 1
        doc.custom_service_charge_override_rate = 0.0
        doc.custom_service_charge_override_amount = 0.0
        doc.custom_service_charge_reason = full_reason
        doc.custom_service_charge_approver = auth["approver"]
    elif action == "change":
        is_zero_override = (rate_val is not None and flt(rate_val) == 0) or (
            amount_val is not None and flt(amount_val) == 0
        )
        if is_zero_override:
            doc.custom_service_charge_waived = 1
            doc.custom_service_charge_override_rate = 0.0
            doc.custom_service_charge_override_amount = 0.0
        else:
            doc.custom_service_charge_waived = 0
            doc.custom_service_charge_override_rate = flt(rate_val) if rate_val is not None else 0.0
            doc.custom_service_charge_override_amount = flt(amount_val) if amount_val is not None else 0.0
        doc.custom_service_charge_reason = full_reason
        doc.custom_service_charge_approver = auth["approver"]
    else:
        doc.custom_service_charge_waived = 0
        doc.custom_service_charge_override_rate = 0.0
        doc.custom_service_charge_override_amount = 0.0
        doc.custom_service_charge_reason = ""
        doc.custom_service_charge_approver = ""

    prec = _doc_precision(doc, "total_taxes_and_charges")
    with _with_currency_precision(doc, prec):
        sync_service_charge(doc, profile)
        doc.calculate_taxes_and_totals()
        _save_verified(doc)

    sc_amount = sum(
        _effective_tax_row_amount(doc, t)
        for t in (doc.get("taxes") or [])
        if _clean_str(t.get("custom_pos_charge_kind")) == "Service Charge"
    )
    sc_tax_amount = sum(
        _effective_tax_row_amount(doc, t)
        for t in (doc.get("taxes") or [])
        if _clean_str(t.get("custom_pos_charge_kind")) == "Service Charge Tax"
    )
    return {
        "invoice": doc.name,
        "action": action,
        "waived": cint(doc.get("custom_service_charge_waived")) == 1,
        "service_charge_amount": sc_amount,
        "service_charge_tax_amount": sc_tax_amount,
        "approver": auth["approver"],
        "via_pin": auth["via_pin"],
        "grand_total": flt(doc.get("grand_total")),
    }


# =====================================================================
# E6: Post-Close Tip Adjustment (#145 Tip Model, Matrix Row 30)
# =====================================================================


def build_tip_adjust_claim(
    *,
    invoice_name: str,
    old_tip: float,
    new_tip: float,
    tip_server: str,
    payment_mode: str,
    reason: str,
    reason_note: str = "",
) -> dict[str, Any]:
    delta = round(flt(new_tip) - flt(old_tip), 4)
    return {
        "action": "tip_adjust",
        "invoice": invoice_name,
        "old_tip": round(flt(old_tip), 4),
        "new_tip": round(flt(new_tip), 4),
        "delta": delta,
        "amount": abs(delta),
        "tip_server": (tip_server or "").strip(),
        "payment_mode": (payment_mode or "").strip(),
        "reason": (reason or "").strip(),
        "note": (reason_note or "").strip(),
    }


def _validate_tips_liability_account(company: str, account: str | None) -> str:
    ledger = (
        frappe.db.get_value("Account", account, ["company", "root_type", "is_group", "disabled"], as_dict=True)
        if account
        else None
    )
    if (
        not ledger
        or ledger.get("company") != company
        or ledger.get("root_type") != "Liability"
        or cint(ledger.get("is_group")) == 1
        or cint(ledger.get("disabled")) == 1
    ):
        frappe.throw(
            coded_message(
                "TIP-ACCOUNT",
                _(
                    "Set the POS Profile's Tips Payable Account to an enabled liability account of {0} "
                    "before adjusting tips."
                ).format(company),
            ),
            frappe.ValidationError,
        )
    return str(account)


@frappe.whitelist(methods=["POST"])
def adjust_closed_check_tip(
    invoice: str,
    pos_profile: str | None = None,
    new_tip_amount: float | None = None,
    tip_server: str | None = None,
    payment_mode: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    approval_token: str | None = None,
    mode_of_payment: str | None = None,
) -> dict[str, Any]:
    """Adjust charged tip on a submitted (closed) POS Invoice via balanced Liability JE.

    Follows #145's Tips Payable liability model: tips sit outside sales revenue
    and outside VAT, so posting the delta as a Journal Entry between the tender
    clearing account and Tips Payable never mutates a fiscalised check's items,
    taxes, or EFRIS FDN.
    """
    assert_not_offline_replay()
    if payment_mode is None and mode_of_payment is not None:
        payment_mode = mode_of_payment
    new_tip = _parse_finite_float(new_tip_amount, "TIP-INVALID", "Enter a valid tip amount.")
    if new_tip < 0:
        frappe.throw(
            coded_message("TIP-NEGATIVE", _("A tip cannot be negative.")),
            frappe.ValidationError,
        )

    doc, profile = _load_invoice_and_profile(invoice, pos_profile, for_update=True)
    if cint(doc.get("docstatus")) != 1:
        frappe.throw(
            coded_message("TIP-CHECK-NOT-CLOSED", _("Post-close tip adjustment applies only to a closed check.")),
            frappe.ValidationError,
        )
    if cint(doc.get("is_return")) == 1:
        frappe.throw(
            coded_message("TIP-RETURN-FORBIDDEN", _("Cannot adjust a tip on a sales return.")),
            frappe.ValidationError,
        )

    precision = doc.precision("grand_total") if callable(getattr(doc, "precision", None)) else 2
    old_tip = flt(doc.get("custom_charged_tip"), precision)
    new_tip = flt(new_tip, precision)
    delta = flt(new_tip - old_tip, precision)
    if delta == 0:
        frappe.throw(
            coded_message("TIP-NO-CHANGE", _("The new tip matches the existing tip amount.")),
            frappe.ValidationError,
        )

    tips_account = _validate_tips_liability_account(doc.company, profile.get("custom_tips_payable_account"))

    # Resolve non-cash tender clearing account from payment_mode or invoice's payments
    chosen_mode = (payment_mode or "").strip()
    if not chosen_mode:
        for row in doc.get("payments") or []:
            if flt(row.get("amount")) > 0 and row.get("mode_of_payment"):
                chosen_mode = row.get("mode_of_payment")
                break
    tender_account = (
        frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": chosen_mode, "company": doc.company},
            "default_account",
        )
        if chosen_mode
        else None
    )
    if not tender_account:
        frappe.throw(
            coded_message(
                "TIP-TENDER-ACCOUNT",
                _("No clearing account is configured for payment mode {0} in {1}.").format(
                    chosen_mode or _("(none)"), doc.company
                ),
            ),
            frappe.ValidationError,
        )

    server = (
        (tip_server or "").strip()
        or doc.get("custom_tip_server")
        or doc.get("waiter")
        or doc.get("owner")
        or frappe.session.user
    )
    pre_tip_total = max(0.0, flt(doc.get("grand_total")) - old_tip)
    tip_pct = (new_tip / pre_tip_total * 100.0) if pre_tip_total > 0 else 100.0
    max_tip_pct = flt(profile.get("custom_max_tip_percent"))
    over_tip_cap = max_tip_pct > 0 and tip_pct > max_tip_pct

    claim = build_tip_adjust_claim(
        invoice_name=doc.name,
        old_tip=old_tip,
        new_tip=new_tip,
        tip_server=server,
        payment_mode=chosen_mode,
        reason=reason or "",
        reason_note=reason_note or "",
    )

    auth = authorize_or_perform(
        privilege="tip_adjust",
        pos_profile=profile.name,
        reference_doctype="POS Invoice",
        reference_name=doc.name,
        claim=claim,
        token=approval_token,
        percent=tip_pct,
        amount=company_approval_amount(doc, delta, frappe),
        reason=reason,
        reason_note=reason_note,
        require_reason=True,
        fallback_roles=DEFAULT_MANAGER_ROLES,
        force_pin=over_tip_cap,
    )

    cost_center = profile.get("cost_center") or doc.get("cost_center")
    abs_delta = abs(delta)
    if delta > 0:
        accounts = [
            {
                "account": tender_account,
                "debit_in_account_currency": abs_delta,
                "credit_in_account_currency": 0.0,
                "cost_center": cost_center,
            },
            {
                "account": tips_account,
                "debit_in_account_currency": 0.0,
                "credit_in_account_currency": abs_delta,
                "cost_center": cost_center,
            },
        ]
    else:
        accounts = [
            {
                "account": tips_account,
                "debit_in_account_currency": abs_delta,
                "credit_in_account_currency": 0.0,
                "cost_center": cost_center,
            },
            {
                "account": tender_account,
                "debit_in_account_currency": 0.0,
                "credit_in_account_currency": abs_delta,
                "cost_center": cost_center,
            },
        ]

    full_reason = f"{auth['reason']}: {auth['reason_note']}" if auth["reason_note"] else auth["reason"]
    je = frappe.get_doc(
        {
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "company": doc.company,
            "posting_date": _nowdate(),
            "user_remark": (
                f"POS tip adjustment on {doc.name} ({old_tip} -> {new_tip}, server={server}, "
                f"reason={full_reason}, approver={auth['approver']})"
            ),
            "accounts": accounts,
        }
    )
    je.insert(ignore_permissions=True)
    je.submit()

    doc.db_set(
        {
            "custom_charged_tip": new_tip,
            "custom_tip_server": server,
            "custom_tip_adjustment_je": je.get("name"),
        },
        update_modified=False,
    )

    return {
        "invoice": doc.name,
        "old_tip": old_tip,
        "new_tip": new_tip,
        "delta": delta,
        "tip_server": server,
        "journal_entry": je.get("name"),
        "approver": auth["approver"],
        "via_pin": auth["via_pin"],
    }


# =====================================================================
# Mutation-Site Guard (POS Invoice `validate` hook — REST / Desk / Old Clients)
# =====================================================================


def validate_privileged_pricing_mutations(doc, method=None) -> None:
    """Enforce Scope e pricing invariants on EVERY POS Invoice save (REST, Desk, API)."""
    if getattr(frappe.flags, "in_patch", False) or getattr(frappe.flags, "in_migrate", False):
        return
    if cint(doc.get("is_return")) == 1:
        return

    # 1. Negative line rates are NEVER permitted on non-return checks.
    for row in doc.get("items") or []:
        if cint(row.get("is_disposable")) == 1:
            continue
        if flt(row.get("rate")) < 0:
            frappe.throw(
                coded_message("PRICE-NEGATIVE", _("Item price cannot be negative.")),
                frappe.ValidationError,
            )

    # 2. Comp vs Room Charge collision guard (both directions).
    if cint(doc.get("custom_is_complimentary")) == 1 and _invoice_has_room_charge(doc):
        frappe.throw(
            coded_message(
                "COMP-ROOM-CHARGE-FORBIDDEN",
                _("A complimentary check cannot carry a room charge."),
            ),
            frappe.ValidationError,
        )

    # 3. Tax-exempt customer lock: changing customer after exempting is refused.
    if cint(doc.get("custom_tax_exempt")) == 1:
        exempt_customer = (doc.get("custom_tax_exempt_customer") or "").strip()
        current_customer = (doc.get("customer") or "").strip()
        if exempt_customer and current_customer and current_customer != exempt_customer:
            frappe.throw(
                coded_message(
                    "TAX-EXEMPT-CUSTOMER-CHANGED",
                    _(
                        "Cannot change customer on a tax-exempt check from {0} to {1}. "
                        "Clear the tax exemption and re-authorise for the new customer."
                    ).format(exempt_customer, current_customer),
                ),
                frappe.ValidationError,
            )

    if getattr(getattr(doc, "flags", None), "ury_privileged_action_verified", False):
        return
    if (
        getattr(frappe.flags, "ury_bill_split", False)
        or getattr(frappe.flags, "ury_qty_reduction", False)
        or getattr(frappe.flags, "in_bill_merge_sync", False)
    ):
        return

    before = doc.get_doc_before_save() if callable(getattr(doc, "get_doc_before_save", None)) else None
    server_disc_write = bool(getattr(frappe.flags, "ury_server_discount_write", False))
    has_disc_approval = bool(
        getattr(getattr(doc, "flags", None), "discount_approval", None)
        or getattr(getattr(doc, "flags", None), "discount_removal", None)
    )
    if not server_disc_write and not has_disc_approval and validate_manual_line_discount_cap is not None:
        validate_manual_line_discount_cap(doc, before)
    if before is None:
        return

    prec = _doc_precision(doc, "rate")
    rate_tol = max(1e-2, 10 ** (-prec))
    price_list = _clean_str(doc.get("selling_price_list"))

    # 4. Prevent direct REST/Desk tampering with line rates, new rows, open items,
    #    discount-percentage rate smuggling, header discounts, comp/exempt flags, or SC rows.
    before_items = {r.get("name"): r for r in (before.get("items") or []) if r.get("name")}
    for row in doc.get("items") or []:
        if cint(row.get("is_disposable")) == 1:
            continue
        prev = before_items.get(row.get("name"))
        curr_rate = flt(row.get("rate"))
        curr_plr = flt(row.get("price_list_rate"))
        curr_disc_pct = flt(row.get("discount_percentage"))
        curr_disc_amt = flt(row.get("discount_amount"))

        if prev is None:
            # New row appended directly on an existing invoice via REST/Desk
            if (
                cint(row.get("custom_price_overridden")) == 1
                or cint(row.get("custom_is_open_item")) == 1
                or cint(row.get("custom_is_complimentary")) == 1
            ):
                frappe.throw(
                    coded_message(
                        "POS-AUTH-REQUIRED",
                        _("Privileged item flags must go through the POS authorisation flow."),
                    ),
                    frappe.PermissionError,
                )
            item_code = _clean_str(row.get("item_code"))
            if item_code and hasattr(frappe, "db") and callable(getattr(frappe.db, "get_value", None)):
                try:
                    raw_open = frappe.db.get_value("Item", item_code, "custom_is_open_priced")
                except Exception:
                    raw_open = 0
                if isinstance(raw_open, (int, float, str, bool)) and cint(raw_open) == 1:
                    frappe.throw(
                        coded_message(
                            "POS-AUTH-REQUIRED",
                            _("Open-priced items must be added through the POS open-item flow."),
                        ),
                        frappe.PermissionError,
                    )
                if price_list and not server_disc_write:
                    try:
                        raw_ip = frappe.db.get_value(
                            "Item Price",
                            {"item_code": item_code, "price_list": price_list},
                            "price_list_rate",
                        )
                    except Exception:
                        raw_ip = None
                    if isinstance(raw_ip, (int, float, str)) and flt(raw_ip) > 0:
                        auth_plr = flt(raw_ip)
                        valid_rates = [auth_plr]
                        if callable(getattr(frappe, "get_all", None)):
                            try:
                                extra = frappe.get_all(
                                    "Item Price",
                                    filters={"item_code": item_code, "price_list": price_list},
                                    pluck="price_list_rate",
                                ) or []
                                valid_rates.extend(flt(p) for p in extra if isinstance(p, (int, float, str)) and flt(p) > 0)
                            except Exception:
                                pass
                        if curr_disc_pct == 0 and curr_disc_amt == 0:
                            if not any(abs(curr_rate - vp) <= rate_tol for vp in valid_rates):
                                frappe.throw(
                                    coded_message(
                                        "POS-AUTH-REQUIRED",
                                        _("Price overrides must go through the POS price override authorisation flow."),
                                    ),
                                    frappe.PermissionError,
                                )
                        else:
                            expected_ok = any(
                                abs(curr_rate - flt(vp * (1.0 - curr_disc_pct / 100.0), prec)) <= rate_tol
                                or (curr_disc_amt > 0 and abs(curr_rate - flt(vp - curr_disc_amt, prec)) <= rate_tol)
                                for vp in valid_rates
                            )
                            if not expected_ok:
                                frappe.throw(
                                    coded_message(
                                        "POS-AUTH-REQUIRED",
                                        _("Price overrides must go through the POS price override authorisation flow."),
                                    ),
                                    frappe.PermissionError,
                                )
            if curr_plr > 0 and curr_disc_pct == 0 and curr_disc_amt == 0 and abs(curr_rate - curr_plr) > rate_tol:
                frappe.throw(
                    coded_message(
                        "POS-AUTH-REQUIRED",
                        _("Price overrides must go through the POS price override authorisation flow."),
                    ),
                    frappe.PermissionError,
                )
            continue

        # Existing row: refuse unverified privileged item flag flips
        for item_flag in ("custom_price_overridden", "custom_is_open_item", "custom_is_complimentary"):
            if cint(row.get(item_flag)) != cint(prev.get(item_flag)):
                frappe.throw(
                    coded_message(
                        "POS-AUTH-REQUIRED",
                        _("Privileged item flags must go through the POS authorisation flow."),
                    ),
                    frappe.PermissionError,
                )

        prev_rate = flt(prev.get("rate"))
        prev_plr = flt(prev.get("price_list_rate") if prev.get("price_list_rate") is not None else prev_rate)
        if abs(curr_rate - prev_rate) > 1e-6 and not server_disc_write:
            plr_unchanged = prev_plr > 0 and abs(curr_plr - prev_plr) <= rate_tol
            expected_by_pct = flt(prev_plr * (1.0 - curr_disc_pct / 100.0), prec)
            expected_by_amt = flt(prev_plr - curr_disc_amt, prec)
            matches_discount = plr_unchanged and (
                (curr_disc_pct > 0 and abs(curr_rate - expected_by_pct) <= rate_tol)
                or (curr_disc_amt > 0 and abs(curr_rate - expected_by_amt) <= rate_tol)
                or (curr_disc_pct == 0 and curr_disc_amt == 0 and abs(curr_rate - prev_plr) <= rate_tol)
            )
            if not matches_discount:
                frappe.throw(
                    coded_message(
                        "POS-AUTH-REQUIRED",
                        _("Price overrides must go through the POS price override authorisation flow."),
                    ),
                    frappe.PermissionError,
                )

    if not server_disc_write and not has_disc_approval:
        prev_add_pct = flt(before.get("additional_discount_percentage"))
        curr_add_pct = flt(doc.get("additional_discount_percentage"))
        prev_disc_amt = flt(before.get("discount_amount"))
        curr_disc_amt = flt(doc.get("discount_amount"))
        add_pct_raised = curr_add_pct > prev_add_pct + 1e-6
        disc_amt_raised = curr_disc_amt > prev_disc_amt + 1e-6
        if disc_amt_raised and prev_add_pct > 0 and not add_pct_raised:
            # Allow ERPNext's proportional recalculation of an unchanged % header discount
            # when items are added to an already-discounted check (never exceeding curr_add_pct).
            apply_on = (doc.get("apply_discount_on") or "Grand Total").strip()
            if apply_on == "Net Total":
                doc_total = flt(doc.get("total"))
                net_plus = flt(doc.get("net_total")) + curr_disc_amt
                curr_basis = min(doc_total, net_plus) if doc_total > 0 else net_plus
            else:
                curr_basis = flt(doc.get("grand_total")) + curr_disc_amt
            if curr_basis <= 0:
                curr_basis = flt(doc.get("total"))
            expected_max = flt(curr_basis * curr_add_pct / 100.0, prec)
            if curr_basis > 0 and curr_disc_amt <= expected_max + rate_tol:
                disc_amt_raised = False
        if disc_amt_raised or add_pct_raised:
            frappe.throw(
                coded_message(
                    "POS-AUTH-REQUIRED",
                    _("Discounts must go through the POS discount authorisation flow."),
                ),
                frappe.PermissionError,
            )

    for flag_field in ("custom_is_complimentary", "custom_tax_exempt", "custom_service_charge_waived"):
        if cint(doc.get(flag_field)) != cint(before.get(flag_field)):
            frappe.throw(
                coded_message(
                    "POS-AUTH-REQUIRED",
                    _("This privileged pricing change requires POS manager authorisation."),
                ),
                frappe.PermissionError,
            )

    for ov_field in ("custom_service_charge_override_rate", "custom_service_charge_override_amount"):
        if abs(flt(doc.get(ov_field)) - flt(before.get(ov_field))) > 1e-6:
            frappe.throw(
                coded_message(
                    "POS-AUTH-REQUIRED",
                    _("Service charge overrides must go through the POS service charge authorisation flow."),
                ),
                frappe.PermissionError,
            )

    # Guard against direct REST/Desk stripping or mutating Service Charge rows without PIN
    # when the check's order_type, covers, profile, items, and discounts did not change.
    before_sc = _sc_signature(before)
    curr_sc = _sc_signature(doc)
    if before_sc and before_sc != curr_sc and _sc_context_unchanged(doc, before):
        frappe.throw(
            coded_message(
                "POS-AUTH-REQUIRED",
                _("Service charge rows cannot be stripped or altered without POS manager authorisation."),
            ),
            frappe.PermissionError,
        )
