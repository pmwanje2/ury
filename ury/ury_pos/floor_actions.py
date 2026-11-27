"""Floor privileged POS actions (Scope f — Simphony & SAP parity).

Covers:
- Cross-outlet / RVC check transfer (`transfer_check_outlet`, Simphony Opt 46)
- Pick up another server's check (`pickup_other_check`, Simphony Opt 18, waiter stays sticky)
- Server-authoritative open bill print counting & reprint PIN gate (`authorize_and_count_bill_print`)
- Occupied-table detection across both table flags and open draft checks (`assert_table_free_for_transfer`)
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.brand import coded_message
from ury.ury_pos.privileged_auth import (
    DEFAULT_MANAGER_ROLES,
    _is_valid_str,
    _table_exists,
    assert_not_efris_fiscalised,
    assert_not_offline_replay,
    authorize_or_perform,
)


def is_table_occupied_for_transfer(table_name: str, *, exclude_invoice: str | None = None) -> bool:
    """True when `table_name` is flagged occupied OR has any open draft POS Invoice."""
    if not table_name:
        return False
    occupied = frappe.db.get_value("URY Table", table_name, "occupied")
    if cint(occupied) == 1:
        return True
    filters: dict[str, Any] = {"restaurant_table": table_name, "docstatus": 0}
    if exclude_invoice:
        filters["name"] = ["!=", exclude_invoice]
    if frappe.db.exists("POS Invoice", filters):
        return True
    return False


def assert_table_free_for_transfer(table_doc_or_name, *, exclude_invoice: str | None = None) -> None:
    """Refuse transfer onto an occupied table with `[TRANSFER-TABLE-OCCUPIED]`."""
    table_name = (
        table_doc_or_name
        if isinstance(table_doc_or_name, str)
        else getattr(table_doc_or_name, "name", None)
    )
    if not table_name:
        return
    occupied_flag = (
        cint(getattr(table_doc_or_name, "occupied", 0))
        if not isinstance(table_doc_or_name, str)
        else cint(frappe.db.get_value("URY Table", table_name, "occupied"))
    )
    if occupied_flag == 1 or is_table_occupied_for_transfer(table_name, exclude_invoice=exclude_invoice):
        frappe.throw(
            coded_message(
                "TRANSFER-TABLE-OCCUPIED",
                _("Table {0} is already occupied.").format(table_name),
            ),
            frappe.ValidationError,
        )


def authorize_and_count_bill_print(
    invoice_name: str,
    *,
    approval_token: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    pos_profile: str | None = None,
) -> dict[str, Any]:
    """Enforce DB-locked print count vs `custom_max_free_bill_prints` and return print metadata.

    Locks the `POS Invoice` row (`FOR UPDATE`) so concurrent prints across devices
    never double-spend free prints. Prints up to `custom_max_free_bill_prints`
    (default 1) are free; subsequent prints require `reprint_bill` privilege / PIN + reason
    and carry `COPY #N` watermark metadata.
    """
    assert_not_offline_replay()

    if not _table_exists("POS Invoice"):
        return {
            "print_number": 1,
            "is_reprint": False,
            "copy_number": None,
            "reprint_label": "",
            "approver": None,
            "reason": "",
        }

    row = frappe.db.get_value(
        "POS Invoice",
        invoice_name,
        ["pos_profile", "company", "invoice_printed", "custom_bill_print_count", "docstatus"],
        as_dict=True,
        for_update=True,
    )
    if not row:
        return {
            "print_number": 1,
            "is_reprint": False,
            "copy_number": None,
            "reprint_label": "",
            "approver": None,
            "reason": "",
        }

    resolved_profile = row.get("pos_profile") or pos_profile
    company = row.get("company") if _is_valid_str(row.get("company")) else None

    raw_max_free = (
        frappe.db.get_value("POS Profile", resolved_profile, "custom_max_free_bill_prints")
        if _is_valid_str(resolved_profile)
        else None
    )
    max_free = 1 if raw_max_free is None else max(0, cint(raw_max_free))

    prior_count = max(
        cint(row.get("custom_bill_print_count")),
        1 if cint(row.get("invoice_printed")) == 1 else 0,
    )
    next_print_number = prior_count + 1
    is_reprint = prior_count > 0

    approver = None
    validated_reason = ""
    if prior_count >= max_free:
        auth = authorize_or_perform(
            privilege="reprint_bill",
            pos_profile=resolved_profile,
            company=company,
            reference_doctype="POS Invoice",
            reference_name=invoice_name,
            claim={
                "action": "reprint_open_bill",
                "invoice": invoice_name,
                "print_number": next_print_number,
            },
            token=approval_token,
            reason=reason,
            reason_note=reason_note,
            require_reason=True,
            fallback_roles=DEFAULT_MANAGER_ROLES,
        )
        approver = auth.get("approver")
        validated_reason = auth.get("reason") or ""

    frappe.db.set_value(
        "POS Invoice",
        invoice_name,
        "custom_bill_print_count",
        next_print_number,
        update_modified=False,
    )

    reprint_label = f"COPY #{next_print_number} / REPRINT" if is_reprint else ""
    return {
        "print_number": next_print_number,
        "is_reprint": is_reprint,
        "copy_number": next_print_number if is_reprint else None,
        "reprint_label": reprint_label,
        "approver": approver,
        "reason": validated_reason,
    }


def check_currency(inv):
    return (inv.get("currency") or frappe.db.get_value("POS Profile", inv.pos_profile, "currency")
            or frappe.db.get_value("Company", inv.company, "default_currency"))


def validate_outlet_transfer(inv, target_profile, *, table_branch=None):
    from ury.ury_pos.outlet_context import assert_outlet_access

    assert_outlet_access(inv.pos_profile)
    assert_outlet_access(target_profile.name)
    if not inv.company or inv.company != target_profile.company:
        frappe.throw(_("Cannot transfer checks between companies."), frappe.PermissionError)
    target_currency = target_profile.get("currency") or frappe.db.get_value("Company", inv.company, "default_currency")
    if check_currency(inv) != target_currency:
        frappe.throw(_("Choose an outlet using the check currency."))
    changing_outlet = inv.pos_profile != target_profile.name or inv.branch != (table_branch or target_profile.branch)
    if changing_outlet and any(cint(row.get("custom_tender_received")) and flt(row.get("amount")) for row in inv.get("payments") or []):
        frappe.throw(_("Cannot move a check with received tenders."))


def rebind_outlet(inv, target, *, table_branch=None):
    from ury.ury.doctype.ury_order.ury_order import _dayparts_mod, invoice_pricing_date, price_items_for_invoice
    from ury.ury_pos.tax_determination import apply_order_type_tax

    destination_branch = table_branch or target.branch
    if destination_branch != inv.branch:
        held = [row for row in inv.get("items") or [] if cint(row.get("ury_held"))
                and not row.get("custom_kitchen_anchor") and flt(row.qty) > 0]
        if held:
            from ury.ury.api.ury_stock_policy import get_branch_stock_policy

            reserve = get_branch_stock_policy(branch=destination_branch, company=inv.company).reservation_control_enabled
            if reserve or frappe.db.exists("URY Stock Reservation", {"order_ref": inv.name, "status": "Reserved"}):
                from ury.ury.api.ury_order_reservation_service import transfer_held_reservations

                transfer_held_reservations(inv, held, destination_branch, reserve)

    # Remove the source outlet's liability row before the invoice save hook
    # rebuilds it using the destination account.
    if flt(inv.get("custom_charged_tip")):
        old_account = frappe.db.get_value("POS Profile", inv.pos_profile, "custom_tips_payable_account")
        if old_account:
            inv.set("taxes", [row for row in inv.get("taxes") or [] if row.account_head != old_account])
    inv.pos_profile = target.name
    inv.branch = destination_branch
    inv.restaurant = target.get("restaurant")
    resolved = _dayparts_mod().resolve_menu_and_price_list(
        inv.branch, restaurant=inv.restaurant, table=inv.get("restaurant_table"),
        room=inv.get("custom_restaurant_room"), order_type=inv.get("order_type"),
        customer=inv.get("customer"),
    )
    inv.custom_serving_period = (resolved.get("serving_period") or {}).get("period_name")
    price_list = resolved.get("price_list") or target.get("selling_price_list")
    priced_warehouses = {}
    if price_list:
        inv.selling_price_list = price_list
        inv.flags.ury_authoritative_price_list = price_list
        inv.flags.ury_trusted_server_pricing = True
        menu = resolved.get("menu")
        rows = [row for row in inv.get("items") or []
                if not any(cint(row.get(flag)) for flag in
                           ("is_disposable", "custom_price_overridden", "custom_is_open_item", "custom_is_complimentary", "custom_is_modifier"))
                and not row.get("custom_parent_line_key")
                and row.get("custom_line_role") != "Combo Upcharge"]
        priced = price_items_for_invoice(
            [{"item": row.item_code, "qty": row.qty} for row in rows],
            price_list, target.name, inv.branch, menu,
            posting_date=invoice_pricing_date(inv), customer=inv.customer,
        )
        for row, prices in zip(rows, priced):
            row.rate = prices["rate"]
            row.price_list_rate = prices["price_list_rate"]
            priced_warehouses[row.item_code] = prices.get("warehouse")

    # Issued stock stays with the sending station. Held rows, including fixed-price
    # modifiers, will fire and post stock at the receiving outlet.
    from ury.ury.doctype.ury_order.ury_order import _department_warehouse_for_item
    for row in inv.get("items") or []:
        if cint(row.get("ury_held")):
            if row.item_code not in priced_warehouses:
                priced_warehouses[row.item_code] = _department_warehouse_for_item(row.item_code, inv.branch, inv.company)
            row.warehouse = priced_warehouses[row.item_code] or target.get("warehouse")

    if not cint(inv.get("custom_tax_exempt")):
        inv.taxes_and_charges = target.get("taxes_and_charges") or ""
    apply_order_type_tax(inv, target, reset_non_exempt_item_taxes=True)


def validate_table_restaurant(table, profile, company):
    restaurant = table.get("restaurant")
    if (restaurant or None) != (profile.get("restaurant") or None):
        frappe.throw(_("The destination table and POS Profile must belong to the same restaurant."))
    if company:
        branch_company = frappe.db.get_value("Branch", table.branch, "company")
        owners = {branch_company} if branch_company else set()
        if restaurant:
            scope = frappe.db.get_value("URY Restaurant", restaurant, ["branch", "company"], as_dict=True)
            if not scope or scope.branch != table.branch:
                frappe.throw(_("The destination restaurant does not belong to the table branch."))
            if scope.company:
                owners.add(scope.company)
        if owners != {company}:
            frappe.throw(_("The destination table must belong to the check company."))


def destination_profiles(outlets, branch, company, currency, restaurant=None):
    return [o["pos_profile"] for o in outlets if o["branch"] == branch and o["company"] == company
            and (restaurant or None) == (o.get("restaurant") or None)
            and (not currency or currency == o["currency"])]


@frappe.whitelist()
def get_transfer_destinations(branch, exclude_table=None, invoice=None):
    from ury.ury_pos.outlet_context import get_authorized_outlets

    outlets = get_authorized_outlets()
    companies = {o["company"] for o in outlets if o["branch"] == branch}
    currency = source_profile = None
    if invoice:
        check = frappe.get_doc("POS Invoice", invoice)
        check.check_permission("read")
        if check.branch != branch or check.company not in companies:
            frappe.throw(_("Not permitted to transfer this check."), frappe.PermissionError)
        companies = {check.company}
        source_profile = check.pos_profile
        currency = check_currency(check)
    branches = sorted({o["branch"] for o in outlets if o["company"] in companies and o["branch"]})
    if not branches:
        return []
    tables = frappe.get_list("URY Table", filters={"branch": ["in", branches], "name": ["!=", exclude_table or ""]},
                             fields=["name", "branch", "restaurant", "restaurant_room", "occupied", "merged_with",
                                     "no_of_seats", "table_shape"], order_by="branch, restaurant_room, name",
                             limit_page_length=0)
    if not invoice:
        scopes = {(o["branch"], o.get("restaurant") or None) for o in outlets if o["company"] in companies}
        return [table for table in tables if (table.branch, table.get("restaurant") or None) in scopes]
    result = []
    for table in tables:
        profiles = destination_profiles(outlets, table.branch, check.company, currency, table.get("restaurant"))
        if profiles:
            if profiles != [source_profile]:
                table.transfer_profiles = sorted(profiles, key=lambda name: name != source_profile)
            result.append(table)
    return result


@frappe.whitelist(methods=["POST"])
def transfer_check_outlet(
    invoice: str,
    target_pos_profile: str,
    target_table: str | None = None,
    approval_token: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    join_roster: int = 0,
) -> dict[str, Any]:
    """Transfer an open check to another outlet / POS Profile (Simphony Opt 46)."""
    assert_not_offline_replay()
    if not invoice or not target_pos_profile:
        frappe.throw(_("Invoice and target POS Profile are required."), frappe.ValidationError)

    if target_table:
        source = frappe.db.get_value("POS Invoice", invoice, "restaurant_table")
        if source != target_table:
            from ury.ury.doctype.ury_order.ury_order import table_transfer

            return table_transfer(source, target_table, invoice, approval_token, reason, reason_note,
                                  join_roster=join_roster, target_pos_profile=target_pos_profile)

    inv = frappe.get_doc("POS Invoice", invoice, for_update=True)
    if not frappe.has_permission("POS Invoice", "write", doc=inv):
        frappe.throw(_("Not permitted to transfer this check."), frappe.PermissionError)
    if cint(inv.docstatus) != 0:
        frappe.throw(_("Only open draft checks can be transferred between outlets."), frappe.ValidationError)
    assert_not_efris_fiscalised(inv)

    source_profile_name = inv.pos_profile
    if not source_profile_name:
        frappe.throw(_("Check has no POS Profile."), frappe.ValidationError)
    if source_profile_name == target_pos_profile and not target_table:
        frappe.throw(_("Select a different outlet POS Profile."), frappe.ValidationError)

    source_profile = frappe.get_doc("POS Profile", source_profile_name)
    target_profile = frappe.get_doc("POS Profile", target_pos_profile)
    if cint(target_profile.get("disabled")) == 1:
        frappe.throw(_("Target POS Profile is disabled."), frappe.ValidationError)
    if source_profile.company != target_profile.company:
        frappe.throw(_("Cannot transfer a check to a POS Profile in another company."), frappe.PermissionError)

    validate_outlet_transfer(inv, target_profile)
    old_table_name = inv.get("restaurant_table")
    tables_to_lock = sorted({t for t in (old_table_name, target_table) if t})
    locked_tables: dict[str, Any] = {}
    for t_name in tables_to_lock:
        locked_tables[t_name] = frappe.get_doc("URY Table", t_name, for_update=True)

    target_table_doc = locked_tables.get(target_table) if target_table else None
    if target_table_doc is not None:
        validate_table_restaurant(target_table_doc, target_profile, inv.company)
        if not target_table_doc.branch or target_table_doc.branch != target_profile.get("branch"):
            frappe.throw(_("Target table does not belong to the target outlet branch."), frappe.ValidationError)
        # Check occupied BEFORE consuming any manager PIN token
        assert_table_free_for_transfer(target_table_doc, exclude_invoice=inv.name)

    from ury.ury.doctype.ury_order.ury_order import _has_role

    transfer_roles = {row.role for row in (source_profile.get("transfer_role_permissions") or []) if getattr(row, "role", None)}
    fallback_allowed = _has_role(frappe.get_roles(), source_profile.get("transfer_role_permissions")) or bool(
        set(frappe.get_roles()) & (DEFAULT_MANAGER_ROLES | transfer_roles)
    )

    auth = authorize_or_perform(
        privilege="transfer_check",
        pos_profile=source_profile_name,
        company=source_profile.company,
        reference_doctype="POS Invoice",
        reference_name=inv.name,
        claim={
            "action": "transfer_check_outlet",
            "invoice": inv.name,
            "from_profile": source_profile_name,
            "to_profile": target_pos_profile,
            "to_table": target_table or "",
        },
        token=approval_token,
        reason=reason,
        reason_note=reason_note,
        allow_fallback_predicate=fallback_allowed,
    )

    if target_table_doc is not None:
        inv.restaurant_table = target_table_doc.name
        inv.custom_restaurant_room = target_table_doc.get("restaurant_room")
        frappe.db.set_value(
            "URY Table",
            target_table_doc.name,
            {"occupied": 1, "latest_invoice_time": inv.get("creation")},
        )
    elif old_table_name:
        inv.restaurant_table = None
        inv.custom_restaurant_room = None

    if old_table_name and old_table_name != target_table:
        other_open = frappe.db.exists(
            "POS Invoice",
            {"restaurant_table": old_table_name, "docstatus": 0, "name": ["!=", inv.name]},
        )
        if not other_open:
            frappe.db.set_value(
                "URY Table",
                old_table_name,
                {"occupied": 0, "seating_checks": "[]", "latest_invoice_time": None},
            )

    rebind_outlet(inv, target_profile)
    from ury.ury_pos.pricing_actions import sync_service_charge

    sync_service_charge(inv, target_profile)
    if callable(getattr(inv, "calculate_taxes_and_totals", None)):
        inv.calculate_taxes_and_totals()
    inv.flags.ury_privileged_action_verified = True
    from ury.ury_pos.transfer_context import outlet_transfer_save
    try:
        with outlet_transfer_save(inv):
            inv.save()
    finally:
        inv.flags.ury_privileged_action_verified = False

    if target_table_doc is not None:
        from ury.ury.doctype.ury_order.ury_order import change_table_in_kot

        change_table_in_kot(inv.name, target_table_doc.name, inv.branch)

    return {
        "invoice": inv.name,
        "pos_profile": inv.pos_profile,
        "branch": inv.branch,
        "restaurant_table": inv.get("restaurant_table"),
        "approver": auth["approver"],
    }


@frappe.whitelist(methods=["POST"])
def pickup_other_check(
    invoice: str,
    approval_token: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
) -> dict[str, Any]:
    """Pick up another server's open check (Simphony Opt 18) while keeping `waiter` sticky."""
    assert_not_offline_replay()
    if not invoice:
        frappe.throw(_("Invoice is required."), frappe.ValidationError)

    inv = frappe.get_doc("POS Invoice", invoice, for_update=True)
    if not frappe.has_permission("POS Invoice", "read", doc=inv):
        frappe.throw(_("Not permitted to access this check."), frappe.PermissionError)
    if cint(inv.docstatus) != 0:
        frappe.throw(_("Only open draft checks can be picked up."), frappe.ValidationError)

    user = frappe.session.user
    owner_waiter = inv.get("waiter") or ""
    if owner_waiter and owner_waiter == user:
        return {
            "invoice": inv.as_dict(),
            "owner_waiter": owner_waiter,
            "picked_up_by": user,
            "approver": user,
        }

    pos_profile_name = inv.get("pos_profile")
    company = inv.get("company") if _is_valid_str(inv.get("company")) else None

    from ury.ury.doctype.ury_order.ury_order import _order_ownership_flags

    flags = _order_ownership_flags(inv, pos_profile_name)
    is_super_user = user == "Administrator" or "System Manager" in frappe.get_roles()
    fallback_allowed = bool(is_super_user or flags["has_elevated_access"] or flags["is_billing_user"])

    auth = authorize_or_perform(
        privilege="pickup_other_check",
        pos_profile=pos_profile_name,
        company=company,
        reference_doctype="POS Invoice",
        reference_name=inv.name,
        claim={
            "action": "pickup_other_check",
            "invoice": inv.name,
            "owner_waiter": owner_waiter,
        },
        token=approval_token,
        reason=reason,
        reason_note=reason_note,
        allow_fallback_predicate=fallback_allowed,
    )

    return {
        "invoice": inv.as_dict(),
        "owner_waiter": owner_waiter,
        "picked_up_by": user,
        "approver": auth["approver"],
    }
