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

import json
import math
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from ury.brand import coded_message
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
    below_cost = item_cost > 0 and rate_val < item_cost
    delta = abs(rate_val - original_rate)
    pct = (delta / original_rate * 100.0) if original_rate > 0 else 100.0

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
        amount=delta,
        reason=reason,
        reason_note=reason_note,
        require_reason=True,
        fallback_roles=DEFAULT_MANAGER_ROLES,
        always_second_person=is_zero_override,
        force_pin=below_cost,
    )

    # Preserve original released list price for audit/reporting; set price_list_rate = rate
    # so subsequent bill/line discounts and native returns apply cleanly to the agreed rate.
    if not target_row.get("custom_original_price_list_rate"):
        target_row.custom_original_price_list_rate = original_rate
    target_row.rate = rate_val
    target_row.price_list_rate = rate_val
    target_row.base_price_list_rate = rate_val
    target_row.ignore_pricing_rule = 1
    target_row.custom_price_overridden = 1
    target_row.custom_price_override_reason = (
        f"{auth['reason']}: {auth['reason_note']}" if auth["reason_note"] else auth["reason"]
    )
    target_row.custom_price_override_approver = auth["approver"]

    doc.calculate_taxes_and_totals()
    _save_verified(doc)

    return {
        "invoice": doc.name,
        "item_row_name": item_row_name,
        "original_rate": original_rate,
        "rate": rate_val,
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
    invoice: str,
    pos_profile: str | None = None,
    item_code: str | None = None,
    rate: float | None = None,
    qty: float = 1.0,
    reference_name_text: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    approval_token: str | None = None,
    reference: str | None = None,
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

    doc, profile = _load_invoice_and_profile(invoice, pos_profile, for_update=True)
    if cint(doc.get("docstatus")) != 0:
        frappe.throw(
            coded_message("OPEN-ITEM-CHECK-CLOSED", _("Items can only be added to an open check.")),
            frappe.ValidationError,
        )
    assert_not_efris_fiscalised(doc)

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

    over_halo = halo_max > 0 and rate_val > halo_max
    below_min = min_price > 0 and rate_val < min_price and not (rate_val == 0 and allow_zero)
    below_cost = item_cost > 0 and rate_val < item_cost and not (rate_val == 0 and allow_zero)
    line_total = rate_val * qty_val

    claim = build_open_item_claim(
        invoice_name=doc.name,
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
        reference_name=doc.name,
        claim=claim,
        token=approval_token,
        amount=line_total,
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
            "base_price_list_rate": rate_val,
            "cost_center": profile.get("cost_center"),
            "warehouse": profile.get("warehouse"),
            "custom_is_open_item": 1,
            "custom_open_item_reference": ref_text,
            "custom_price_override_approver": auth["approver"],
        },
    )

    doc.calculate_taxes_and_totals()
    _save_verified(doc)

    return {
        "invoice": doc.name,
        "item_row_name": row.get("name"),
        "item_code": item_code,
        "item_name": display_name,
        "rate": rate_val,
        "qty": qty_val,
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
        amount=comp_amount,
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
        today = getdate(nowdate())
        for rule in rules or []:
            tpl = rule.get("sales_tax_template")
            if not tpl:
                continue
            from_d = rule.get("from_date")
            to_d = rule.get("to_date")
            if from_d and getdate(from_d) > today:
                continue
            if to_d and getdate(to_d) < today:
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

    # Preserve non-VAT operational charge rows (e.g. Service Charge, Charged Tip) when switching tax template
    sc_account = profile.get("custom_service_charge_account")
    tip_account = profile.get("custom_tips_payable_account")
    non_tax_accounts = {a for a in (sc_account, tip_account) if a}

    if scope == "check":
        preserved = [r for r in (doc.get("taxes") or []) if r.get("account_head") in non_tax_accounts]
        template_rows = _load_template_tax_rows(resolved_template)
        doc.tax_category = resolved_category
        doc.taxes_and_charges = resolved_template
        doc.set("taxes", template_rows + preserved)
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

    sc_account = profile.get("custom_service_charge_account")
    tip_account = profile.get("custom_tips_payable_account")
    non_tax_accounts = {a for a in (sc_account, tip_account) if a}
    preserved = [r for r in (doc.get("taxes") or []) if r.get("account_head") in non_tax_accounts]

    doc.tax_category = ""
    doc.taxes_and_charges = default_tpl or ""
    doc.set("taxes", (_load_template_tax_rows(default_tpl) if default_tpl else []) + preserved)
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
# E5: Service Charge Auto-Apply, Waive & Change (Matrix Row 14)
# =====================================================================


def _service_charge_applies(doc, profile) -> bool:
    rate = flt(profile.get("custom_service_charge_rate"))
    account = (profile.get("custom_service_charge_account") or "").strip()
    if rate <= 0 or not account:
        return False
    raw_types = (profile.get("custom_service_charge_order_types") or "Dine In").strip()
    allowed_types = {
        part.strip()
        for line in raw_types.splitlines()
        for part in line.split(",")
        if part.strip()
    }
    if allowed_types and (doc.get("order_type") or "") not in allowed_types:
        return False
    min_pax = cint(profile.get("custom_service_charge_min_pax"))
    if min_pax > 0 and cint(doc.get("no_of_pax")) < min_pax:
        return False
    return True


def sync_service_charge(doc, profile=None) -> None:
    """Keep the check's native Sales Taxes and Charges service-charge row in sync with POS Profile."""
    if not doc or cint(doc.get("is_return")) == 1:
        return
    if profile is None:
        if not doc.get("pos_profile"):
            return
        profile = frappe.get_doc("POS Profile", doc.pos_profile)

    account = (profile.get("custom_service_charge_account") or "").strip()
    if not account:
        return

    existing_non_sc = [r for r in (doc.get("taxes") or []) if r.get("account_head") != account]

    if cint(doc.get("custom_service_charge_waived")) == 1:
        doc.set("taxes", existing_non_sc)
        return

    override_rate = doc.get("custom_service_charge_override_rate")
    override_amount = doc.get("custom_service_charge_override_amount")
    has_override = override_rate not in (None, "") or override_amount not in (None, "")

    if not has_override and not _service_charge_applies(doc, profile):
        doc.set("taxes", existing_non_sc)
        return

    desc = (profile.get("custom_service_charge_description") or _("Service Charge")).strip()
    taxable = cint(profile.get("custom_service_charge_taxable")) == 1
    if override_amount not in (None, "") and flt(override_amount) >= 0 and override_rate in (None, ""):
        sc_row = {
            "charge_type": "Actual",
            "account_head": account,
            "description": desc,
            "tax_amount": flt(override_amount),
            "cost_center": profile.get("cost_center") or doc.get("cost_center"),
            "included_in_print_rate": 0,
        }
    else:
        eff_rate = flt(override_rate) if override_rate not in (None, "") else flt(profile.get("custom_service_charge_rate"))
        sc_row = {
            "charge_type": "On Net Total",
            "account_head": account,
            "description": desc,
            "rate": eff_rate,
            "cost_center": profile.get("cost_center") or doc.get("cost_center"),
            "included_in_print_rate": 0,
        }

    # If service charge is taxable, place it before VAT rows; otherwise after VAT rows.
    combined = ([sc_row] + existing_non_sc) if taxable else (existing_non_sc + [sc_row])
    doc.set("taxes", combined)


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

    claim = build_service_charge_claim(
        invoice_name=doc.name,
        action=action,
        new_rate=rate_val,
        new_amount=amount_val,
        reason=reason or "",
        reason_note=reason_note or "",
    )

    auth = authorize_or_perform(
        privilege="service_charge_waive",
        pos_profile=profile.name,
        reference_doctype="POS Invoice",
        reference_name=doc.name,
        claim=claim,
        token=approval_token,
        percent=rate_val,
        amount=amount_val,
        reason=reason,
        reason_note=reason_note,
        require_reason=(action != "restore"),
        fallback_roles=DEFAULT_MANAGER_ROLES,
    )

    full_reason = f"{auth['reason']}: {auth['reason_note']}" if auth["reason_note"] else auth["reason"]
    if action == "waive":
        doc.custom_service_charge_waived = 1
        doc.custom_service_charge_override_rate = None
        doc.custom_service_charge_override_amount = None
        doc.custom_service_charge_reason = full_reason
        doc.custom_service_charge_approver = auth["approver"]
    elif action == "change":
        doc.custom_service_charge_waived = 0
        doc.custom_service_charge_override_rate = rate_val
        doc.custom_service_charge_override_amount = amount_val
        doc.custom_service_charge_reason = full_reason
        doc.custom_service_charge_approver = auth["approver"]
    else:
        doc.custom_service_charge_waived = 0
        doc.custom_service_charge_override_rate = None
        doc.custom_service_charge_override_amount = None
        doc.custom_service_charge_reason = ""
        doc.custom_service_charge_approver = ""

    sync_service_charge(doc, profile)
    doc.calculate_taxes_and_totals()
    _save_verified(doc)

    return {
        "invoice": doc.name,
        "action": action,
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
        amount=abs(delta),
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
            "posting_date": nowdate(),
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

    if doc.flags.get("ury_privileged_action_verified"):
        return
    if getattr(frappe.flags, "ury_bill_split", False) or getattr(frappe.flags, "ury_qty_reduction", False):
        return

    before = doc.get_doc_before_save() if callable(getattr(doc, "get_doc_before_save", None)) else None
    if before is None:
        return

    # 4. Prevent direct REST/Desk tampering with line rates, comp flags, tax-exempt flags, or service charge waive.
    before_items = {r.get("name"): r for r in (before.get("items") or []) if r.get("name")}
    for row in doc.get("items") or []:
        if cint(row.get("is_disposable")) == 1:
            continue
        prev = before_items.get(row.get("name"))
        if prev is None:
            continue
        prev_rate = flt(prev.get("rate"))
        curr_rate = flt(row.get("rate"))
        # Allow rate changes driven purely by an explicit discount_percentage or unchanged rate.
        if abs(curr_rate - prev_rate) > 1e-6 and flt(row.get("discount_percentage")) == flt(prev.get("discount_percentage")):
            frappe.throw(
                coded_message(
                    "POS-AUTH-REQUIRED",
                    _("Price overrides must go through the POS price override authorisation flow."),
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
