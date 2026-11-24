# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import hashlib
import json
from collections import defaultdict
from uuid import UUID
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, get_system_timezone, now_datetime
from erpnext.controllers.queries import item_query
from ury.brand import coded_message
from ury.ury_pos.api import getBranch, getBranchRoom, getRoom, posOpening
from ury.ury_pos.default_customer import get_default_customer_config, resolve_default_customer
from ury.ury_pos.discount import (
    apply_discount_to_invoice,
    currency_precision,
    enforce_remove_discount_auth,
    get_check_discounts as _get_check_discounts_impl,
    list_discount_options as _list_discount_options_impl,
    parse_discount_request,
    _restore_doc_discount_state,
    _snapshot_doc_discount_state,
)
from ury.ury.api.ury_kot_generate import _execute_kot as kot_execute
from ury.ury.api.ury_kot_generate import process_items_for_cancel_kot
from ury.ury.api.ury_order_reservation_service import (
    _line_key,
    _line_ref,
    reconcile_order_reservations,
    release_order_reservations,
    resolve_production_context,
    _warehouse_for_context,
)
from ury.ury.api.ury_stock_policy import get_branch_stock_policy
from ury.ury.doctype.alert_settings.alert_settings import get_alert_rule
from ury.ury.api.ury_kot_notification import create_system_notification, get_users_with_role
from ury.ury.api.item_price import item_price_release_states, price_valid_on

from frappe import cache


class URYOrder(Document):
    pass

@frappe.whitelist()
def merge_free_tables(table1, table2):
    """Merges two tables in the same room; allows one occupied and one free."""
    return merge_tables_batch(table1, [table2])


@frappe.whitelist()
def merge_tables_batch(anchor_table, tables):

    if isinstance(tables, str):
        tables = json.loads(tables)

    targets = list(
        dict.fromkeys(
            t
            for t in tables
            if t and t != anchor_table
        )
    )

    if not targets:
        frappe.throw(
            _("Select at least one table to merge.")
        )

    room = frappe.db.get_value(
        "URY Table",
        anchor_table,
        "restaurant_room",
    )

    if not room:
        frappe.throw(
            _("Table not found.")
        )

    # Start from anchor cluster only
    cluster, table_map = _get_merge_cluster(
        anchor_table
    )

    cluster = set(cluster)

    for target in targets:

        target_room = frappe.db.get_value(
            "URY Table",
            target,
            "restaurant_room",
        )

        if target_room != room:
            frappe.throw(
                _("Cannot merge tables from different rooms.")
            )

        target_cluster, _target_table_map = _get_merge_cluster(
            target
        )

        # Prevent importing another merged group
        if len(target_cluster) > 1:
            frappe.throw(
                _(
                    "Cannot merge an already merged table."
                )
            )

        # Prevent occupied table merge
        occupied = frappe.db.get_value(
            "URY Table",
            target,
            "occupied",
        )

        if occupied:
            frappe.throw(
                _("Occupied tables cannot be merged.")
            )

        cluster.add(target)

    if _count_separate_active_orders(cluster) > 1:
        frappe.throw(
            _(
                "Cannot merge tables with separate active orders."
            )
        )

    cluster = sorted(cluster)

    # Make relationships symmetric
    for table in cluster:

        partners = [
            t
            for t in cluster
            if t != table
        ]

        frappe.db.set_value(
            "URY Table",
            table,
            "merged_with",
            ",".join(partners)
            if partners
            else None,
            update_modified=False,
        )

    # Sync order only to selected cluster
    _sync_active_order_with_merge_cluster(
        anchor_table
    )

    _reconcile_open_invoices_for_tables(
        cluster
    )

    frappe.db.commit()

    return True
def _append_merged_partner(table_name, partner):
    merged = frappe.db.get_value(
        "URY Table",
        table_name,
        "merged_with"
    ) or ""
    partners = _parse_merged_with(merged)
    if partner not in partners:
        partners.append(partner)
    frappe.db.set_value(
        "URY Table",
        table_name,
        "merged_with",
        ",".join(sorted(set(partners))),
    )

def _sync_active_order_with_merge_cluster(table):

    members = _get_cluster_table_names(table)

    invoices = frappe.get_all(
        "POS Invoice",
        filters={
            "docstatus": 0,
            "invoice_printed": 0,
        },
        fields=[
            "name",
            "restaurant_table",
            "creation",
        ],
    )

    active = [
        x
        for x in invoices
        if x.restaurant_table in members
    ]

    if not active:
        return

    primary = sorted(
        active,
        key=lambda x: x.creation
    )[0]

    merged_tables = ",".join(
        sorted(
            [
                x
                for x in members
                if x != primary.restaurant_table
            ]
        )
    )

    frappe.db.set_value(
        "POS Invoice",
        primary.name,
        "custom_merged_tables",
        merged_tables,
        update_modified=False,
    )

    for table_name in members:

        frappe.db.set_value(
            "URY Table",
            table_name,
            {
                "occupied": 1,
                "latest_invoice_time": primary.creation,
            },
        )

def _parse_merged_with(merged_with):
    if not merged_with:
        return []
    return [partner.strip() for partner in merged_with.split(",") if partner.strip()]


def _table_has_active_order(table_name):
    return bool(
        frappe.db.exists(
            "POS Invoice",
            {
                "docstatus": 0,
                "restaurant_table": table_name,
                "invoice_printed": 0,
            },
        )
    )


def _count_separate_active_orders(table_names):
    return sum(1 for name in table_names if _table_has_active_order(name))


def _get_merge_cluster(table):
    room = frappe.db.get_value("URY Table", table, "restaurant_room")
    if not room:
        frappe.throw(_("Table not found."))

    room_tables = frappe.get_all(
        "URY Table",
        filters={"restaurant_room": room},
        fields=["name", "merged_with", "occupied"],
    )
    table_by_name = {row.name: row for row in room_tables}

    if table not in table_by_name:
        frappe.throw(_("Table not found."))

    visited = set()
    members = []
    queue = [table]

    while queue:
        name = queue.pop(0)
        if name in visited:
            continue
        visited.add(name)
        members.append(name)

        row = table_by_name.get(name)
        if not row:
            continue

        for partner in _parse_merged_with(row.merged_with):
            if partner in table_by_name and partner not in visited:
                queue.append(partner)

    return members, table_by_name


def _get_cluster_table_names(table):
    if not table:
        return []
    try:
        members, _table_map = _get_merge_cluster(table)
        return members
    except Exception:
        return [table]


def _merged_partners_for_primary(primary_table):
    if not primary_table:
        return []
    members = _get_cluster_table_names(primary_table)
    return sorted(name for name in members if name != primary_table)


def _merged_partners_csv(primary_table):
    partners = _merged_partners_for_primary(primary_table)
    return ",".join(partners) if partners else None


def _normalize_merged_partners_csv(csv_value):
    if not csv_value:
        return []
    return sorted(_parse_merged_with(csv_value))


def _reconcile_invoice_merged_tables(invoice, persist=False):
    """Align custom_merged_tables on an invoice with the live table merge cluster."""
    primary = invoice.get("restaurant_table")
    if not primary:
        return invoice

    expected = _merged_partners_for_primary(primary)
    current = _normalize_merged_partners_csv(invoice.get("custom_merged_tables"))

    if expected == current:
        return invoice

    partners_value = ",".join(expected) if expected else None
    invoice.custom_merged_tables = partners_value

    if persist and invoice.get("name"):
        frappe.db.set_value(
            "POS Invoice",
            invoice.name,
            "custom_merged_tables",
            partners_value,
            update_modified=False,
        )

    return invoice


def _open_invoice_names_for_table(table):
    names = set()
    for row in frappe.get_all(
        "POS Invoice",
        filters={"docstatus": 0, "restaurant_table": table},
        fields=["name"],
    ):
        names.add(row.name)
    for row in frappe.get_all(
        "POS Invoice",
        filters={"docstatus": 0, "custom_merged_tables": ["like", f"%{table}%"]},
        fields=["name"],
    ):
        names.add(row.name)
    return names


def _reconcile_open_invoices_for_tables(table_names):
    seen = set()
    for table in table_names:
        for invoice_name in _open_invoice_names_for_table(table):
            if invoice_name in seen:
                continue
            seen.add(invoice_name)
            invoice = frappe.get_doc("POS Invoice", invoice_name)
            _reconcile_invoice_merged_tables(invoice, persist=True)


TABLE_RELEASE_FIELDS = {
    "occupied": 0,
    "latest_invoice_time": None,
    "merged_with": None,
}


def release_merge_cluster_tables(table_or_tables, commit=True):
    # Pass commit=False from document hooks: Frappe v16 forbids commits inside doc events.

    if isinstance(table_or_tables, (list, tuple, set)):
        cluster = list(table_or_tables)
    else:
        cluster = _get_table_group(table_or_tables)

    for member in cluster:
        frappe.db.set_value(
            "URY Table",
            member,
            TABLE_RELEASE_FIELDS,
            update_modified=False,
        )

    if commit:
        frappe.db.commit()

@frappe.whitelist()
def release_tables_after_print(invoice):

    invoice_doc = frappe.get_doc(
        "POS Invoice",
        invoice,
    )

    if not frappe.has_permission("POS Invoice", "write", doc=invoice_doc):
        frappe.throw(_("Not permitted to release tables for this invoice"), frappe.PermissionError)

    # Same rule as the POS print paths: a table stays occupied while another
    # draft check (e.g. the other half of a split) is still unprinted.
    tables = _get_table_group(invoice_doc.restaurant_table, invoice_doc.custom_merged_tables)
    if not _has_open_pos_invoices_for_cluster(tables):
        for table in tables:
            frappe.db.set_value("URY Table", table, {"occupied": 0, "latest_invoice_time": None})

    return True


def _has_open_pos_invoices_for_cluster(tables):

    if not tables:
        return False

    for table in tables:

        invoices = frappe.get_all(
            "POS Invoice",
            filters={
                "docstatus": 0,
                "restaurant_table": table,
            },
            fields=[
                "name",
                "invoice_printed",
            ],
        )

        for inv in invoices:
            if inv.invoice_printed == 0:
                return True


        merged = frappe.get_all(
            "POS Invoice",
            filters={
                "docstatus": 0,
                "custom_merged_tables": ["like", f"%{table}%"],
            },
            fields=[
                "name",
                "invoice_printed",
            ],
        )

        for inv in merged:

            if inv.invoice_printed == 0:
                return True

    return False


@frappe.whitelist()
def unmerge_tables(table):

    cluster = _get_cluster_table_names(
        table
    )

    if len(cluster) <= 1:
        frappe.throw(
            _("Table is not merged.")
        )

    if _has_open_pos_invoices_for_cluster(
        cluster
    ):
        frappe.throw(
            _("Cannot unmerge active tables.")
        )

    for member in cluster:

        frappe.db.set_value(
            "URY Table",
            member,
            "merged_with",
            None,
        )

    frappe.db.commit()

    return True


def _get_table_group(restaurant_table, custom_merged_tables=None):
    tables = _get_cluster_table_names(restaurant_table)
    if custom_merged_tables:
        for table_name in custom_merged_tables.split(","):
            table_name = table_name.strip()
            if table_name and table_name not in tables:
                tables.append(table_name)
    return tables


def _has_open_pos_invoices_for_tables(tables):
    return _has_open_pos_invoices_for_cluster(tables)


def _free_tables_if_no_open_invoices(
    restaurant_table,
    custom_merged_tables=None,
):

    if not restaurant_table:
        return

    tables = _get_table_group(
        restaurant_table,
        custom_merged_tables,
    )

    if _has_open_pos_invoices_for_cluster(tables):
        return

    release_merge_cluster_tables(tables)


def _copy_invoice_item_fields(item_row, qty):
    data = dict(
        item_code=item_row.item_code,
        item_name=item_row.item_name,
        qty=qty,
        rate=item_row.rate,
        price_list_rate=item_row.price_list_rate,
        base_price_list_rate=item_row.base_price_list_rate,
        discount_percentage=flt(item_row.get("discount_percentage")),
        discount_amount=flt(item_row.get("discount_amount")),
        comment=item_row.get("comment"),
        custom_course=item_row.get("custom_course"),
        ury_held=cint(item_row.get("ury_held")),
        cost_center=item_row.cost_center,
        uom=item_row.uom,
        conversion_factor=item_row.conversion_factor,
        warehouse=item_row.warehouse,
        # Preserve the stable line identity the reservation/KOT layers key
        # on (see ury_order_reservation_service.LINE_REF_FIELDS). A row with
        # a real persisted key carries it forward as-is (sa-arch-splitbill).
        # A LEGACY row with no key (saved before this field existed) must
        # NOT fall back to its own row name: `_previous_line_snapshot` no
        # longer falls back to `name` either (see B02b), so a name-derived
        # key here would be a bogus, client-unmatchable value that also
        # poisons `_backfill_previous_line_keys`'s `claimed` set for any
        # sibling row of the same item_code -- leaving `None` lets the
        # split invoice's own backfill logic rescue it on the next sync,
        # the same way an un-split legacy invoice already gets rescued.
        reservation_line_key=item_row.get("reservation_line_key") or None,
    )
    if item_row.get("margin_type"):
        data["margin_type"] = item_row.get("margin_type")
        data["margin_rate_or_amount"] = flt(item_row.get("margin_rate_or_amount"))
    return data


GUEST_LABEL_MAX = 40


def _guest_label(value):
    """A short name for an order or check (Susan, Seat 2): trimmed, at most
    GUEST_LABEL_MAX characters, None when blank. It is not a Customer."""
    label = (value or "").strip()
    if len(label) > GUEST_LABEL_MAX:
        frappe.throw(
            coded_message(
                "GUEST-LABEL-TOO-LONG",
                _("A guest label can be at most {0} characters.").format(GUEST_LABEL_MAX),
            ),
            frappe.ValidationError,
        )
    return label or None


def _split_kots_to_move(source, move_map, reservations_enabled):
    whole, selected, known = set(), set(), set()
    released_dishes = set()
    for item in source.items:
        keys = {item.get("reservation_line_key") or item.name, _line_key(item.item_code, item, 1)}
        known.update(keys)
        qty = move_map.get(item.name, 0)
        if qty > 0:
            selected.update(keys)
            if not cint(item.get("ury_held")):
                released_dishes.add(item.item_code)
            if qty == item.qty:
                whole.update(keys)

    movable = []
    for ticket in frappe.get_all("URY KOT", filters={"invoice": source.name}, pluck="name"):
        rows = frappe.get_all("URY KOT Items", filters={"parent": ticket, "parenttype": "URY KOT"},
                              fields=["reservation_line_key", "item"])
        keys = {row.reservation_line_key for row in rows}
        if keys and keys <= whole:
            movable.append(ticket)
        elif reservations_enabled and (keys & selected or any(
            row.reservation_line_key not in known and row.item in released_dishes for row in rows
        )):
            # Fulfilment looks up stock by the single invoice on the kitchen ticket.
            frappe.throw(
                coded_message("SPLIT-RESERVED-KOT", _("Move the entire released kitchen ticket together.")),
                frappe.ValidationError,
            )
    return movable


@frappe.whitelist()
def split_bill(source_invoice, items_to_move, customer=None, guest_label=None):
    """Move selected line items from a printed draft bill to a new sibling POS Invoice."""
    if isinstance(items_to_move, str):
        items_to_move = json.loads(items_to_move)
    guest_label = _guest_label(guest_label)

    source = frappe.get_doc("POS Invoice", source_invoice)

    if not frappe.has_permission("POS Invoice", "write", doc=source):
        frappe.throw(_("Not permitted to split this invoice."), frappe.PermissionError)

    try:
        user_branch = getBranch()
    except frappe.ValidationError:
        if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
            user_branch = None
        else:
            raise

    if user_branch and source.branch != user_branch:
        frappe.throw(_("Not permitted to split invoices from another branch."), frappe.PermissionError)

    # Same ownership / elevated / billing / billed / room gates as sync_order.
    _enforce_order_access(
        source,
        pos_profile_name=source.get("pos_profile"),
        require_modify=True,
        deny_message=coded_message("SPLIT-NOT-PERMITTED", _("Not permitted to split this invoice.")),
    )

    if source.docstatus != 0:
        frappe.throw(_("Only draft invoices can be split."))

    move_map = {
        row["name"]: float(row["qty"])
        for row in items_to_move
        if row.get("name") and float(row.get("qty", 0)) > 0
    }
    if not move_map:
        frappe.throw(_("Select at least one item to move."))

    total_moving_qty = 0.0
    total_remaining_qty = 0.0
    for item in source.items:
        move_qty = move_map.get(item.name, 0)
        if move_qty > item.qty:
            frappe.throw(
                _("Cannot move more than available quantity for {0}.").format(item.item_name)
            )
        total_moving_qty += move_qty
        total_remaining_qty += item.qty - move_qty

    if total_moving_qty <= 0:
        frappe.throw(_("Select at least one item to move."))
    if total_remaining_qty <= 0:
        frappe.throw(_("At least one item must remain on the original bill."))

    company = source.company or frappe.db.get_value("Branch", source.branch, "company")
    reservations_enabled = get_branch_stock_policy(branch=source.branch, company=company).reservation_control_enabled
    movable_kots = _split_kots_to_move(source, move_map, reservations_enabled)

    new_invoice = frappe.new_doc("POS Invoice")
    header_fields = [
        "is_pos",
        "update_stock",
        "naming_series",
        "restaurant",
        "branch",
        "restaurant_table",
        "custom_restaurant_room",
        "custom_merged_tables",
        "waiter",
        "cashier",
        "pos_profile",
        "order_type",
        "no_of_pax",
        "customer",
        "customer_name",
        "selling_price_list",
        "taxes_and_charges",
        "company",
        "currency",
        "conversion_rate",
        "price_list_currency",
    ]
    for field in header_fields:
        if source.get(field) is not None:
            new_invoice.set(field, source.get(field))

    split_group = source.get("custom_split_group") or frappe.generate_hash(length=10)
    source.custom_split_group = split_group

    new_invoice.custom_split_from = source.name
    new_invoice.custom_split_group = split_group
    new_invoice.invoice_printed = 0
    new_invoice.invoice_created = 0
    # the new check is named for who it is for, else keeps the source label
    new_invoice.custom_guest_label = guest_label or source.get("custom_guest_label")

    if customer:
        new_invoice.customer = customer
        new_invoice.customer_name = frappe.db.get_value("Customer", customer, "customer_name")
        new_invoice.mobile_number = frappe.db.get_value("Customer", customer, "mobile_no")

    # Snapshot the pre-split source lines (stable reservation_line_key ->
    # item_code/qty) before anything is mutated/removed below. This is the
    # "previous_items" side of a reconcile_order_reservations() call further
    # down, exactly like sync_order's `past_item` (sa-arch-splitbill: without
    # this, reservations tied to lines that move to the new invoice are
    # never released from the source invoice, and the moved lines arrive on
    # the new invoice with zero reservation coverage).
    previous_source_items = [
        {
            "reservation_line_key": item.get("reservation_line_key") or item.name,
            "item_code": item.item_code,
            "item_name": item.item_name,
            "qty": item.qty,
        }
        for item in source.items
    ]

    # Snapshot pre-split header discount and gross shares for exact proration on flat discounts.
    orig_pct = flt(source.get("additional_discount_percentage"))
    orig_flat = flt(source.get("discount_amount")) if not orig_pct else 0.0
    orig_basis = source.get("apply_discount_on") or "Grand Total"
    orig_total_gross = sum(
        flt(item.get("rate")) * flt(item.get("qty"))
        for item in source.items
        if not cint(item.get("is_disposable")) and flt(item.get("qty")) > 0
    )
    moved_gross = sum(
        flt(item.get("rate")) * min(flt(move_map.get(item.name, 0)), flt(item.get("qty")))
        for item in source.items
        if move_map.get(item.name, 0) > 0 and not cint(item.get("is_disposable"))
    )

    items_to_remove = []
    for item in source.items:
        move_qty = move_map.get(item.name, 0)
        if move_qty <= 0:
            continue
        if move_qty >= item.qty:
            new_invoice.append("items", _copy_invoice_item_fields(item, item.qty))
            items_to_remove.append(item)
        else:
            new_invoice.append("items", _copy_invoice_item_fields(item, move_qty))
            item.qty -= move_qty

    for item in items_to_remove:
        source.remove(item)

    # Carry bill-level discount & audit metadata cleanly across the split halves.
    has_header_discount = orig_pct > 0 or orig_flat > 0
    has_moved_line_discount = any(
        flt(r.get("discount_percentage")) > 0 or flt(r.get("discount_amount")) > 0
        for r in new_invoice.items
    )
    if has_header_discount:
        new_invoice.apply_discount_on = orig_basis
        if orig_pct > 0:
            new_invoice.additional_discount_percentage = orig_pct
            new_invoice.discount_amount = 0
        elif orig_flat > 0 and orig_total_gross > 0:
            prec = currency_precision(source)
            child_disc = flt(orig_flat * (moved_gross / orig_total_gross), prec)
            source_disc = flt(orig_flat - child_disc, prec)
            new_invoice.additional_discount_percentage = 0
            new_invoice.discount_amount = child_disc
            source.additional_discount_percentage = 0
            source.discount_amount = source_disc
            # Avoid double-counting a coupon use on split children
            if hasattr(new_invoice, "coupon_code"):
                new_invoice.coupon_code = None

    if has_header_discount or has_moved_line_discount:
        for field in (
            "custom_discount_reason",
            "custom_discount_note",
            "custom_discount_approver",
            "custom_discount_approved_percent",
            "ury_discount_meta",
        ):
            val = source.get(field)
            if val not in (None, "", 0, 0.0):
                new_invoice.set(field, val)
        if source.get("custom_discount_approver"):
            approval_payload = frappe._dict(
                approver=source.get("custom_discount_approver"),
                percent=flt(source.get("custom_discount_approved_percent")),
            )
            source.flags.discount_approval = approval_payload
            new_invoice.flags.discount_approval = approval_payload

    payment_mode = source.payments[0].mode_of_payment if source.payments else None
    source_received = _received_payment_rows(source)
    received_total = sum(flt(row.get("amount")) for row in source_received)

    frappe.flags.ury_bill_split = True
    prev_tender_flag = getattr(frappe.flags, "ury_server_tender_received", False)
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    frappe.flags.ury_server_discount_write = True
    try:
        source.set_missing_values()
        source.run_method("set_missing_values")
        source.calculate_taxes_and_totals()

        if source_received:
            precision = source.precision("paid_amount") if callable(getattr(source, "precision", None)) else 2
            if not isinstance(precision, int) or isinstance(precision, bool):
                precision = 2
            remaining_total = flt(source.rounded_total or source.grand_total)
            if flt(received_total, precision) > flt(remaining_total, precision):
                frappe.throw(
                    _(
                        "Cannot split check {0}: remaining total ({1}) is less than tenders already received ({2})."
                    ).format(source.name, flt(remaining_total, precision), flt(received_total, precision))
                )
            # Keep every received row on the source check (never lost).
            current_received = [
                row for row in (source.get("payments") or [])
                if flt(row.get("amount")) > 0 and cint(row.get("custom_tender_received")) == 1
            ]
            if len(current_received) < len(source_received):
                source.set("payments", [])
                for row in source_received:
                    data = _copy_payment_row(row)
                    data["custom_tender_received"] = 1
                    source.append("payments", data)
            if callable(getattr(source, "set_paid_amount", None)):
                source.set_paid_amount()
            if callable(getattr(source, "set_outstanding_amount", None)):
                source.set_outstanding_amount()

        new_invoice.set_missing_values()
        new_invoice.run_method("set_missing_values")
        new_invoice.calculate_taxes_and_totals()

        # Never copy received tenders onto the split child check.
        for row in new_invoice.get("payments") or []:
            row.amount = 0
            row.custom_tender_received = 0
        if payment_mode and new_invoice.invoice_created == 0 and not (new_invoice.get("payments") or []):
            new_invoice.append(
                "payments",
                dict(mode_of_payment=payment_mode, amount=0, custom_tender_received=0),
            )
            new_invoice.invoice_created = 1

        new_invoice.insert()
        new_invoice.reload()
        frappe.db.set_value(
            "POS Invoice",
            new_invoice.name,
            {
                "custom_split_from": source.name,
                "custom_split_group": split_group,
            },
            update_modified=False,
        )
        if source_received:
            frappe.flags.ury_server_tender_received = True
        source.save()

        # Reconcile reservations for both sides of the split, reusing the
        # same reconcile_order_reservations() primitive sync_order already
        # relies on (sa-arch-splitbill) rather than inventing a new
        # reservation lifecycle state or mutating existing `URY Stock
        # Reservation` rows' order_ref in place (that doctype's rows are
        # meant to be an append-only audit trail of what was reserved when
        # -- see ury_stock_reservation.py docstring -- so "moving" a
        # reservation to a different invoice is modelled the same way any
        # other quantity change is: release/shrink the old side, create the
        # new side).
        accepted_source_items = [
            {
                "reservation_line_key": item.get("reservation_line_key") or item.name,
                "item_code": item.item_code,
                "item_name": item.item_name,
                "qty": item.qty,
            }
            for item in source.items
        ]
        if reservations_enabled:
            reconcile_order_reservations(
                order_ref=source.name,
                previous_items=previous_source_items,
                accepted_items=accepted_source_items,
                branch=source.branch,
                company=company,
                actor=frappe.session.user,
            )

        new_invoice_items = [
            {
                "reservation_line_key": item.get("reservation_line_key") or item.name,
                "item_code": item.item_code,
                "item_name": item.item_name,
                "qty": item.qty,
            }
            for item in new_invoice.items
        ]
        if reservations_enabled:
            reconcile_order_reservations(
                order_ref=new_invoice.name,
                previous_items=[],
                accepted_items=new_invoice_items,
                branch=new_invoice.branch,
                company=company,
                actor=frappe.session.user,
            )

        for ticket in movable_kots:
            frappe.db.set_value("URY KOT", ticket, "invoice", new_invoice.name, update_modified=False)
    finally:
        frappe.flags.ury_server_tender_received = prev_tender_flag
        frappe.flags.ury_server_discount_write = prev_disc_flag
        frappe.flags.ury_bill_split = False

    return {
        "source_invoice": source.name,
        "new_invoice": new_invoice.name,
    }



def _reservation_lines(rows):
    return [
        {
            "reservation_line_key": row.get("reservation_line_key") or row.name,
            "item_code": row.item_code,
            "item_name": row.item_name,
            "qty": row.qty,
        }
        for row in rows
    ]


def _prepare_consolidated_discount(destination, sources):
    """Resolve ONE bill-level discount for the consolidated check (Simphony Add Check).

    Matching percentages keep that percentage; differing discounts (such as merging a 0%
    check with a 10%-discounted check) sum their exact discount amounts on the shared
    apply_discount_on basis so neither an overcharge nor an unintended discount expansion
    occurs. Conflicting apply_discount_on bases (Net Total vs Grand Total) are refused.
    """
    all_bills = [destination, *sources]
    discounted = []
    for bill in all_bills:
        pct = flt(bill.get("additional_discount_percentage"))
        amt = flt(bill.get("discount_amount"))
        if pct > 0 and amt <= 0:
            bill.calculate_taxes_and_totals()
            amt = flt(bill.get("discount_amount"))
        if pct > 0 or amt > 0:
            discounted.append((bill, pct, amt))

    if not discounted:
        return None

    bases = {bill.get("apply_discount_on") or "Grand Total" for bill, _, _ in discounted}
    if len(bases) > 1:
        frappe.throw(
            _("Cannot merge checks with bill discounts applied on different bases ({0}).").format(
                ", ".join(sorted(bases))
            )
        )
    basis = next(iter(bases))

    all_pcts = [flt(bill.get("additional_discount_percentage")) for bill in all_bills]
    coupons = {bill.get("coupon_code") for bill in all_bills if bill.get("coupon_code")}
    reasons = [bill.get("custom_discount_reason") for bill, _, _ in discounted if bill.get("custom_discount_reason")]
    notes = [bill.get("custom_discount_note") for bill, _, _ in discounted if bill.get("custom_discount_note")]
    approvers = [
        (bill.get("custom_discount_approver"), flt(bill.get("custom_discount_approved_percent")))
        for bill, _, _ in discounted
        if bill.get("custom_discount_approver")
    ]
    carry_reason = reasons[0] if reasons else None
    carry_note = "; ".join(dict.fromkeys(notes)) if notes else None
    carry_approver, carry_approved_pct = approvers[0] if approvers else (None, 0.0)

    if all(p > 0 for p in all_pcts) and len({round(p, 6) for p in all_pcts}) == 1:
        return {
            "apply_discount_on": basis,
            "additional_discount_percentage": all_pcts[0],
            "discount_amount": None,
            "clear_coupon": len(coupons) > 1,
            "reason": carry_reason,
            "note": carry_note,
            "approver": carry_approver,
            "approved_percent": carry_approved_pct,
        }

    return {
        "apply_discount_on": basis,
        "additional_discount_percentage": 0,
        "discount_amount": sum(amt for _, _, amt in discounted),
        "clear_coupon": True,
        "reason": carry_reason,
        "note": carry_note,
        "approver": carry_approver,
        "approved_percent": carry_approved_pct,
    }


def _validate_and_collect_source_tenders(destination, sources):
    """Validate and collect source tenders that move onto the destination check.

    Refuses before mutating anything if a source carries a negative tender, an external
    payment reference, a payment mode not configured on destination's POS Profile, or a
    conflicting room-charge reference.
    """
    rooms = {
        str(bill.get("room_number")).strip()
        for bill in [destination, *sources]
        if bill.get("room_number") and str(bill.get("room_number")).strip()
    }
    if len(rooms) > 1:
        frappe.throw(
            _("Cannot merge checks with different room-charge references ({0}).").format(
                ", ".join(sorted(rooms))
            )
        )

    allowed_modes = (
        set(
            frappe.get_all(
                "POS Payment Method",
                filters={"parent": destination.pos_profile},
                pluck="mode_of_payment",
            )
        )
        if destination.get("pos_profile")
        else set()
    )

    for row in destination.get("payments") or []:
        if flt(row.get("amount")) < 0:
            frappe.throw(
                _("Cannot merge check {0}: negative tender ({1}) cannot be merged.").format(
                    destination.name, row.get("mode_of_payment")
                )
            )
    for row in _received_payment_rows(destination):
        mode = row.get("mode_of_payment")
        if row.get("reference_no") or row.get("clearance_date"):
            frappe.throw(
                _(
                    "Cannot merge check {0}: tender {1} carries an external payment reference and cannot be merged."
                ).format(destination.name, mode)
            )

    movable = []
    for source in sources:
        for row in source.get("payments") or []:
            if flt(row.get("amount")) < 0:
                frappe.throw(
                    _("Cannot merge check {0}: negative tender ({1}) cannot be moved.").format(
                        source.name, row.get("mode_of_payment")
                    )
                )
        for row in _received_payment_rows(source):
            amt = flt(row.get("amount"))
            mode = row.get("mode_of_payment")
            if row.get("reference_no") or row.get("clearance_date"):
                frappe.throw(
                    _(
                        "Cannot merge check {0}: tender {1} carries an external payment reference and cannot be moved."
                    ).format(source.name, mode)
                )
            if allowed_modes and mode not in allowed_modes:
                frappe.throw(
                    _(
                        "Cannot merge check {0}: tender {1} is not configured on POS Profile {2}."
                    ).format(source.name, mode, destination.pos_profile)
                )
            movable.append((row, amt))

    carried_room = next(iter(rooms)) if len(rooms) == 1 else None
    return movable, carried_room


def consolidate_checks(destination, sources):
    """Add Check (Oracle Simphony Add/Transfer Check): move every line, bill-level discount
    and movable received tender of the source checks onto the destination check, so the guest
    has one POS Invoice: one tax calculation, one discount set, one tender set, one fiscal
    receipt and a native return. No merge link is written.

    Each line keeps its rate, line identity and attribution. The kitchen tickets and stock
    reservations follow the lines. The destination must be printed again, since its printed
    bill no longer shows what the guest owes. Each source check is discarded with an audit
    note (firing POS Invoice on_discard hooks such as release_reservation_check_binding), and
    its table is freed once no open check remains on it. The caller validates the checks and
    commits.
    """
    discount_plan = _prepare_consolidated_discount(destination, sources)
    source_tenders, carried_room = _validate_and_collect_source_tenders(destination, sources)

    company = destination.company or frappe.db.get_value("Branch", destination.branch, "company")
    reservations_enabled = get_branch_stock_policy(
        branch=destination.branch, company=company
    ).reservation_control_enabled
    destination_before = _reservation_lines(destination.items)

    for source in sources:
        for row in source.items:
            destination.append(
                "items",
                row.as_dict(no_default_fields=True, no_child_table_fields=True, no_private_properties=True),
            )

    if discount_plan is not None:
        destination.apply_discount_on = discount_plan["apply_discount_on"]
        destination.additional_discount_percentage = discount_plan["additional_discount_percentage"]
        if discount_plan["discount_amount"] is not None:
            destination.discount_amount = discount_plan["discount_amount"]
        if discount_plan.get("clear_coupon") and hasattr(destination, "coupon_code"):
            destination.coupon_code = None
        if discount_plan.get("reason"):
            destination.set("custom_discount_reason", discount_plan["reason"])
        if discount_plan.get("note"):
            destination.set("custom_discount_note", discount_plan["note"])
        if discount_plan.get("approver"):
            destination.flags.discount_approval = frappe._dict(
                approver=discount_plan["approver"],
                percent=discount_plan.get("approved_percent") or 0.0,
            )

    if source_tenders:
        by_mode = {}
        kept_rows = []
        for row in _received_payment_rows(destination):
            mode = row.get("mode_of_payment")
            if not row.get("reference_no") and not row.get("clearance_date"):
                by_mode[mode] = row
            kept_rows.append(row)
        destination.set("payments", kept_rows)
        for row, amt in source_tenders:
            mode = row.get("mode_of_payment")
            if mode in by_mode:
                by_mode[mode].amount = flt(by_mode[mode].amount) + amt
            else:
                data = _copy_payment_row(row)
                data["mode_of_payment"] = mode
                data["amount"] = amt
                data["custom_tender_received"] = 1
                destination.append("payments", data)
                by_mode[mode] = destination.payments[-1]

    if carried_room and not destination.get("room_number"):
        destination.room_number = carried_room

    destination.invoice_printed = 0
    destination.calculate_taxes_and_totals()
    destination.set_paid_amount()
    destination.set_outstanding_amount()
    prev_tender_flag = getattr(frappe.flags, "ury_server_tender_received", False)
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    frappe.flags.ury_server_tender_received = True
    frappe.flags.ury_server_discount_write = True
    try:
        destination.save()
    finally:
        frappe.flags.ury_server_tender_received = prev_tender_flag
        frappe.flags.ury_server_discount_write = prev_disc_flag

    for source in sources:
        for ticket in frappe.get_all("URY KOT", filters={"invoice": source.name}, pluck="name"):
            frappe.db.set_value("URY KOT", ticket, "invoice", destination.name, update_modified=False)
        if reservations_enabled:
            reconcile_order_reservations(
                order_ref=source.name,
                previous_items=_reservation_lines(source.items),
                accepted_items=[],
                branch=destination.branch,
                company=company,
                actor=frappe.session.user,
            )
        source.discard()
        source.db_set("cancel_reason", _("Added to check {0}").format(destination.name))

    if reservations_enabled:
        reconcile_order_reservations(
            order_ref=destination.name,
            previous_items=destination_before,
            accepted_items=_reservation_lines(destination.items),
            branch=destination.branch,
            company=company,
            actor=frappe.session.user,
        )

    for source in sources:
        if source.restaurant_table and source.restaurant_table != destination.restaurant_table:
            _free_tables_if_no_open_invoices(source.restaurant_table, source.custom_merged_tables)


def _resolve_pos_invoice_naming_series(restaurant):
    """Resolve the POS Invoice naming series for `restaurant`.

    Single source of truth for both the dine-in (table) and non-dine-in
    (Take Away/Aggregator) new-invoice paths in `_resolve_or_create_pos_invoice`,
    so they cannot structurally drift apart on this field again. Mirrors
    `frappe.db.get_value` behavior of returning None when the restaurant has
    no `invoice_series_prefix` configured, so a new POS Invoice falls back to
    ERPNext's default POS Profile series exactly as the dine-in path already
    does in that case.
    """
    return frappe.db.get_value("URY Restaurant", restaurant, "invoice_series_prefix")


def _resolve_or_create_pos_invoice(table, invoiceNo, order_type, is_payment, check_permission=True, override_branch=None):
    """Find an existing draft POS Invoice or build a new (unsaved) one.

    Contains the same lookup/creation logic that used to live inline in
    `get_order_invoice`. When `check_permission` is True (the default, used
    by the staff-facing `get_order_invoice`), the same read-permission and
    branch-match checks `get_order_invoice` used to perform run at the exact
    same point they used to — immediately after an existing invoice is
    loaded, BEFORE any of the unconditional branch/restaurant/price-list
    resolution below — so an unauthorized caller still gets rejected before
    any of that extra work happens, not after.

    Pass `check_permission=False` for a customer-safe caller that has
    already authorized the request through its own mechanism (e.g. a
    verified ordering-session token) rather than a Frappe user session —
    `frappe.has_permission`/`getBranch()` assume a real staff session and
    are not meaningful for an elevated/technical caller.

    `override_branch` lets such a caller supply the branch directly for the
    no-`table` (pickup) path instead of relying on `getBranch()`, which
    resolves branch from `frappe.session.user`'s `URY User` assignment —
    meaningless for an elevated technical user with no such assignment.
    Ignored for the `table` path, which always derives branch from the table.

    Returns (invoice, invoice_name) where invoice_name is None when a new,
    unsaved invoice was created.
    """

    def _enforce_read_permission(invoice):
        if not check_permission:
            return
        if not frappe.has_permission("POS Invoice", "read", doc=invoice):
            frappe.throw(frappe._("Not permitted to view this order"), frappe.PermissionError)
        user_branch = getBranch()
        if invoice.branch and user_branch and invoice.branch != user_branch:
            frappe.throw(frappe._("Not permitted to view orders outside your active branch"), frappe.PermissionError)

    if table:
        filters = {"docstatus": 0}
        if invoiceNo:
            filters["name"] = invoiceNo
        elif is_payment != "Payments":
            filters["invoice_printed"] = 0

        or_filters = {
            "restaurant_table": table,
            "custom_merged_tables": ["like", f"%{table}%"]
        }

        invoices = frappe.get_all(
            "POS Invoice",
            filters=filters,
            or_filters=or_filters,
            order_by="creation asc",
            limit=1,
        )
        invoice_name = invoices[0].name if invoices else None
        branch, menu_name, restaurant = get_restaurant_and_menu_name(table)

        if invoice_name:
            invoice = frappe.get_doc("POS Invoice", invoice_name)
            _enforce_read_permission(invoice)

            # Captain/waiter ownership + room-access validation (Phase 3): a
            # Captain without elevated/billing access may only view this
            # table's order if they're its assigned waiter and it's within
            # their assigned room. Skipped for a customer-safe/technical
            # caller (check_permission=False) the same way
            # _enforce_read_permission is above — getRoom()/frappe.session
            # assume a real staff session, meaningless for that caller.
            if check_permission:
                _enforce_order_access(
                    invoice, deny_message=frappe._("Not permitted to view this order")
                )

        else:
            invoice = frappe.new_doc("POS Invoice")

            invoice.naming_series = _resolve_pos_invoice_naming_series(restaurant)

            invoice.is_pos = 1
            invoice.restaurant = restaurant
            invoice.branch = branch

            is_take_away = frappe.db.get_value("URY Table", table, "is_take_away")
            if is_take_away == 1:
                invoice.order_type = "Take Away"
            else:
                invoice.order_type= "Dine In"

        invoice.taxes_and_charges = frappe.db.get_value(
            "URY Restaurant", restaurant, "default_tax_template"
        )

        invoice.selling_price_list = frappe.db.get_value(
            "Price List", dict(restaurant_menu=menu_name, enabled=1)
        )

        if not getattr(invoice, "customer", None) or invoice.customer == "Guest":
            from ury.ury.api.table_reservation import (
                get_completed_reservation_for_table,
                reservations_enabled,
            )

            completed_res = get_completed_reservation_for_table(table, branch) if reservations_enabled(branch) else None
            if completed_res and completed_res.get("customer"):
                invoice.customer = completed_res.get("customer")
                if completed_res.get("customer_name"):
                    invoice.customer_name = completed_res.get("customer_name")
                if completed_res.get("customer_phone"):
                    invoice.mobile_number = completed_res.get("customer_phone")
                if completed_res.get("no_of_pax"):
                    invoice.no_of_pax = completed_res.get("no_of_pax")
                if completed_res.get("name") and not completed_res.get("pos_invoice") and invoice_name:
                    frappe.db.set_value("URY Table Reservation", completed_res.get("name"), "pos_invoice", invoice_name)

        if invoice_name and invoice.restaurant_table:
            _reconcile_invoice_merged_tables(invoice, persist=True)

    else:

        if is_payment == "Payments":
            invoice_name = frappe.get_value(
                "POS Invoice", dict(restaurant_table=table, docstatus=0, name=invoiceNo)
            )

        else:
            invoice_name = frappe.get_value(
                "POS Invoice", dict(docstatus=0, name=invoiceNo)
            )

        if invoice_name:
            invoice = frappe.get_doc("POS Invoice", invoice_name)
            _enforce_read_permission(invoice)

            # Same ownership + room-access validation as the table-lookup
            # branch above, skipped for a customer-safe/technical caller.
            if check_permission:
                _enforce_order_access(
                    invoice, deny_message=frappe._("Not permitted to view this order")
                )

        else:
            invoice = frappe.new_doc("POS Invoice")
            invoice.is_pos = 1

        branch = override_branch or getBranch()
        invoice.branch = branch
        restaurant = frappe.db.get_value("URY Restaurant", {"branch": branch}, "name")

        if not invoice_name:
            invoice.naming_series = _resolve_pos_invoice_naming_series(restaurant)

        menu = get_menu_name(order_type, branch=branch)

        if (order_type == "Aggregators" and frappe.db.get_value("Branch", branch, "custom_no_taxes") == 0) or order_type != "Aggregators":
            invoice.taxes_and_charges = frappe.db.get_value("URY Restaurant", restaurant, "default_tax_template")

        invoice.selling_price_list = frappe.db.get_value(
            "Price List", dict(restaurant_menu=menu, enabled=1)
        )

        if invoice_name and invoice.restaurant_table:
            _reconcile_invoice_merged_tables(invoice, persist=True)

    return invoice, invoice_name


@frappe.whitelist()
def get_order_invoice(table=None, invoiceNo=None, order_type=None, is_payment=None):
    """returns the active invoice linked to the given table"""

    # check_permission defaults to True: read-permission and branch-match
    # checks run inside the helper, immediately after an existing invoice is
    # loaded — the same point they ran at before this function was split.
    invoice, invoice_name = _resolve_or_create_pos_invoice(table, invoiceNo, order_type, is_payment)
    return invoice


def _department_warehouse_for_item(item_code, branch, company):
    """sa-architecture-closure (Gap B): resolve the same department warehouse
    the availability/reservation/fulfilment pipeline uses for this item, via
    `resolve_production_context` + `_warehouse_for_context` -- the exact pair
    `ury_order_reservation_service._reserve_line` already uses to pick a
    warehouse for reservation.

    Returns `None` when the item has no resolvable department-controlled
    production config (e.g. a genuinely direct-retail item, or the
    production-config table/row is missing) -- callers should fall back to
    ERPNext's own default (POS Profile warehouse) in that case, which is
    correct for direct-retail items that aren't tied to a department.
    """
    if not item_code or not branch:
        return None
    try:
        context = resolve_production_context(item_code, branch, company)
    except Exception:
        return None
    if not context:
        return None
    return _warehouse_for_context(context)


def invoice_pricing_date(invoice):
    """The pricing date of an invoice: its posting date.

    ERPNext resets posting_date to today on validate unless set_posting_time
    is on, so an open till invoice prices on today.
    """
    if cint(getattr(invoice, "set_posting_time", 0)) and getattr(invoice, "posting_date", None):
        return frappe.utils.getdate(invoice.posting_date)
    return frappe.utils.getdate()


def price_items_for_invoice(items, price_list, pos_profile, branch, menu, posting_date=None, customer=None):
    """Resolve course and price for each item and build the invoice item dicts.

    Returns a list of dicts in the same shape previously passed directly to
    `invoice.append("items", ...)` inside `sync_order`. Does not append to
    the invoice itself. The price is the Item Price valid on posting_date
    (default today) and released by the site's Item Price workflow, if any,
    chosen for the invoice customer and the item's selling UOM as ERPNext
    does (ury.ury.api.item_price.price_valid_on).

    sa-architecture-closure (Gap B): also resolves each item's
    `department_warehouse` (the same warehouse availability/reservation/
    fulfilment already agree on) and sets it explicitly on the item dict.

    This is load-bearing for stock correctness, not tidiness. The POS
    Invoice itself never posts a stock ledger entry -- `POS Invoice` has no
    `update_stock` field at all. The sale-side deduction happens once per
    session, at POS Closing Entry: consolidation copies each POS Invoice
    Item onto a consolidated `Sales Invoice` **with its warehouse carried
    over verbatim** and sets `update_stock = 1` on that document, whose
    submit writes the SLEs. So the warehouse set here is the warehouse the
    sale is ultimately deducted from, hours later.

    That is what makes a made-to-order item net out correctly: the
    fulfilment posting service receives the finished good into this same
    department warehouse at KOT READY, and closing then issues it from
    there. Weaken this resolution and the two land in different warehouses.

    Items with no resolvable department (genuinely direct-retail) are left
    unset, so ERPNext's own POS Profile default still applies for them --
    they are deducted once, at closing, from that default warehouse.
    """
    company = frappe.db.get_value("Branch", branch, "company")
    priced_items = []
    posting_date = frappe.utils.getdate(posting_date) if posting_date else frappe.utils.getdate()
    release_field, released = item_price_release_states()

    for d in items:
        item_code = d.get("item")
        item_name = d.get("item_name")

        course = frappe.db.get_value("URY Menu Item", {"item": item_code, "parent": menu}, "course")

        item_price = price_valid_on(
            item_code, price_list, posting_date, release_field, released, customer=customer,
        )

        if not item_price:
            frappe.throw(
                coded_message(
                    "PRICE-NOT-VALID",
                    _("No released item price valid on {2} found for Item: {0} in Price List: {1}. Please check the price list settings.").format(item_code, price_list, posting_date),
                ),
                frappe.ValidationError,
            )

        else:
            if not item_name or item_name == item_code:
                item_name = frappe.db.get_value("Item", item_code, "item_name") or item_name or item_code
            department_warehouse = _department_warehouse_for_item(item_code, branch, company)
            priced_items.append(
                dict(
                    item_code=item_code,
                    item_name=item_name,
                    qty=d.get("qty"),
                    reservation_line_key=d.get("reservation_line_key"),
                    **({"custom_course": course} if course else {}),
                    **({"warehouse": department_warehouse} if department_warehouse else {}),
                    comment=d.get("comment"),
                    rate = item_price.price_list_rate,
                    price_list_rate = item_price.price_list_rate,
                    base_price_list_rate = item_price.price_list_rate,
                    cost_center = frappe.db.get_value(
                        "POS Profile", pos_profile, "cost_center"
                        ),
                )
            )

    return priced_items


def _has_role(user_roles, role_permitted_rows):
    """True if any role in `user_roles` appears in a POS Profile Table MultiSelect
    of "Role Permitted" rows (e.g. transfer_role_permissions, role_allowed_for_billing,
    role_restricted_for_table_order)."""
    return any(row.role in user_roles for row in (role_permitted_rows or []))


def _ensure_invoice_reservation_ref(invoice):
    if invoice.name:
        return invoice.name

    set_new_name = getattr(invoice, "set_new_name", None)
    if callable(set_new_name):
        set_new_name()

    if not invoice.name:
        frappe.throw(_("Unable to allocate an order reference for stock reservation."), frappe.ValidationError)

    return invoice.name


def _order_ownership_flags(invoice, pos_profile_name=None):
    """Compute the same is_mine / has_elevated_access / is_billing_user /
    can_view / can_modify booleans that `get_table_order_context()` derives
    (Phase 2), for reuse by other endpoints (`get_order_invoice`, `sync_order`,
    `reprint_kot`) without touching that already-reviewed function's body.

    An "order" only has an owner to protect once it actually exists
    (`invoice.name` truthy) — mirrors the `if order: ... else: can_view = True`
    branch in `get_table_order_context()`.
    """
    order_exists = bool(invoice.name)
    is_mine = order_exists and frappe.session.user == invoice.waiter

    pos_profile_name = pos_profile_name or invoice.get("pos_profile")

    has_elevated_access = False
    is_billing_user = False
    if pos_profile_name:
        pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
        user_roles = frappe.get_roles()
        has_elevated_access = _has_role(user_roles, pos_profile.transfer_role_permissions)
        is_billing_user = _has_role(user_roles, pos_profile.role_allowed_for_billing)

    # Administrator/System Manager is exempt from the ownership formula
    # itself, matching this file's own established convention for every
    # getBranch()/getRoom() call elsewhere (e.g. _enforce_order_access below).
    # Without this, an admin account not personally listed in the POS
    # Profile's transfer/billing role tables and not the order's waiter would
    # be hard-denied by checks that previously didn't exist at all.
    is_super_user = frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()

    can_view = is_super_user or ((is_mine or has_elevated_access or is_billing_user) if order_exists else True)

    invoice_billed = order_exists and bool(invoice.get("invoice_printed"))
    can_modify = is_super_user or (can_view and (not invoice_billed or is_billing_user))

    return {
        "is_mine": is_mine,
        "has_elevated_access": has_elevated_access,
        "is_billing_user": is_billing_user,
        "can_view": can_view,
        "can_modify": can_modify,
    }


def _can_remove_sent_items(pos_profile, invoice=None, pos_profile_name=None):
    """Whether the acting user may reduce or remove an already-sent line.

    The POS Profile `remove_items` flag grants this to everyone on the
    profile. Managers - the elevated roles named in
    `transfer_role_permissions`, the same set that gates table and captain
    transfer - may also remove sent items without that flag, since they are
    the people who correct a mis-keyed order on the floor.
    """
    if bool(pos_profile.remove_items):
        return True
    if invoice is None:
        return False
    flags = _order_ownership_flags(invoice, pos_profile_name or pos_profile.name)
    return bool(flags["has_elevated_access"])


def _enforce_order_access(invoice, pos_profile_name=None, require_modify=False, deny_message=None):
    """Fail-closed ownership + room-scoping gate for reading/modifying an
    existing order. Reuses `_order_ownership_flags()` (Phase 2's ownership
    formula) for the Captain/waiter check, then additionally requires the
    acting user's assigned room (via `getRoom()`, the same `URY User`
    source used elsewhere) to include the order's table room — unless the
    user already has elevated or billing access.
    """
    flags = _order_ownership_flags(invoice, pos_profile_name)
    allowed = flags["can_modify"] if require_modify else flags["can_view"]

    if not allowed:
        frappe.throw(
            deny_message or _("Not permitted to access this order"),
            frappe.PermissionError,
        )

    if flags["has_elevated_access"] or flags["is_billing_user"]:
        return flags

    table = invoice.get("restaurant_table")
    if not table:
        return flags

    table_room = frappe.db.get_value("URY Table", table, "restaurant_room")
    if not table_room:
        return flags

    try:
        assigned_rooms = {row.get("name") for row in getRoom()}
    except frappe.ValidationError:
        if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
            return flags
        raise

    # getRoom() returns room=None for a branch-level (not room-specific)
    # `URY User` assignment — that's "access to every room in the branch",
    # not "assigned to a room named None". Found via live E2E test: a real
    # branch-assigned Captain was wrongly denied here before this check.
    if None not in assigned_rooms and table_room not in assigned_rooms:
        frappe.throw(
            _("Not permitted to access orders outside your assigned room"),
            frappe.PermissionError,
        )

    return flags


def check_split_target_access(invoice, permission):
    if not (invoice.get("custom_split_group") or invoice.get("custom_split_from")):
        return
    if not frappe.has_permission("POS Invoice", permission, doc=invoice):
        frappe.throw(_("Not permitted to access this check"), frappe.PermissionError)
    if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
        if not invoice.branch or getBranch() != invoice.branch:
            frappe.throw(_("Not permitted outside your active branch"), frappe.PermissionError)
    _enforce_order_access(invoice, require_modify=False)


def _branch_has_open_pos(branch):
    """True when the branch has at least one submitted Open POS Opening Entry."""
    return bool(
        frappe.db.exists(
            "POS Opening Entry",
            {"branch": branch, "status": "Open", "docstatus": 1},
        )
    )


def _room_has_open_pos(branch, room):
    """True when a multi-cashier opening covers `room` on `branch`."""
    if not room:
        return False
    rows = frappe.db.sql(
        """
        SELECT DISTINCT `tabPOS Opening Entry`.name
        FROM `tabPOS Opening Entry`
        INNER JOIN `tabMultiple Rooms`
            ON `tabMultiple Rooms`.parent = `tabPOS Opening Entry`.name
        WHERE `tabPOS Opening Entry`.branch = %s
            AND `tabPOS Opening Entry`.status = 'Open'
            AND `tabPOS Opening Entry`.docstatus = 1
            AND `tabMultiple Rooms`.room = %s
        LIMIT 1
        """,
        (branch, room),
    )
    return bool(rows)


def _resolve_sync_opening_room(table=None, room=None, invoice=None):
    """Prefer the table's restaurant room over a client-supplied room string."""
    if table:
        table_room = frappe.db.get_value("URY Table", table, "restaurant_room")
        if table_room:
            return table_room
    if invoice and invoice.get("restaurant_table"):
        table_room = frappe.db.get_value(
            "URY Table", invoice.restaurant_table, "restaurant_room"
        )
        if table_room:
            return table_room
    if room:
        return room
    if invoice and invoice.get("custom_restaurant_room"):
        return invoice.custom_restaurant_room
    return None


def _require_open_cashier_session(
    pos_profile,
    branch,
    room=None,
    *,
    billing_user=False,
    order_type=None,
):
    """Fail closed unless a valid open cashier session covers this sync.

    Single-cashier: any open POS Opening Entry on the branch.
    Multi-cashier: open entry must include the table/order room.
    Billing takeaway (no room, non-Dine-In) still requires a branch opening so
    cashier attribution remains valid without inventing a room scope.
    """
    if not branch:
        frappe.throw(_("Cannot sync order without a branch."))

    multi = bool(pos_profile.custom_enable_multiple_cashier)
    is_takeaway_flow = (order_type or "") != "Dine In" and not room

    if multi and billing_user and is_takeaway_flow:
        if not _branch_has_open_pos(branch):
            frappe.throw(
                _("POS is closed. Please open a POS entry before taking orders.")
            )
        return

    if multi:
        if not room or not _room_has_open_pos(branch, room):
            frappe.throw(
                _("POS is closed or no cashier session is open for this room.")
            )
        return

    if not _branch_has_open_pos(branch):
        frappe.throw(_("POS is closed. Please open a POS entry before taking orders."))


def _resolve_menu_for_sync(branch, table=None, room=None, order_type=None):
    """Authoritative menu for sync validation (room-wise / order-type-wise)."""
    restaurant = frappe.db.get_value("URY Restaurant", {"branch": branch}, "name")
    if not restaurant:
        return None

    if table:
        _branch, menu, _restaurant = get_restaurant_and_menu_name(table)
        return menu

    if room:
        room_wise_menu = frappe.db.get_value(
            "URY Restaurant", restaurant, "room_wise_menu"
        )
        if room_wise_menu:
            menu = frappe.db.get_value(
                "Menu for Room",
                {"parent": restaurant, "room": room},
                "menu",
            )
            if menu:
                return menu

    if order_type:
        order_type_wise_menu = frappe.db.get_value(
            "URY Restaurant", restaurant, "order_type_wise_menu"
        )
        if order_type_wise_menu:
            menu = frappe.db.get_value(
                "Order Type Menu",
                {"parent": restaurant, "order_type": order_type},
                "menu",
            )
            if menu:
                return menu

    return frappe.db.get_value("URY Restaurant", restaurant, "active_menu")


def _validate_sync_items_against_menu(
    items,
    past_item,
    branch,
    table=None,
    room=None,
    order_type=None,
):
    """Reject newly added disabled / off-menu lines; keep historic + aggregators.

    Aggregator flows price from Aggregator Settings (not restaurant menu).
    Historic item codes already on the invoice may stay even if later disabled
    or removed from the menu so captains can still update notes/qty of sent lines.
    """
    historic_codes = {prev["item_code"] for prev in (past_item or []) if prev.get("item_code")}
    new_codes = []
    for row in items or []:
        code = row.get("item")
        if code and code not in historic_codes and code not in new_codes:
            new_codes.append(code)

    if not new_codes:
        return

    if order_type == "Aggregators":
        for code in new_codes:
            if frappe.db.get_value("Item", code, "disabled"):
                frappe.throw(
                    _("Item {0} is disabled and cannot be added to the order.").format(code)
                )
        return

    menu = _resolve_menu_for_sync(branch, table=table, room=room, order_type=order_type)
    if not menu:
        frappe.throw(_("Please set an active menu for this restaurant."))

    menu_rows = frappe.get_all(
        "URY Menu Item",
        filters={"parent": menu, "item": ["in", new_codes], "disabled": 0},
        fields=["item"],
    )
    allowed = {row.item for row in menu_rows}

    for code in new_codes:
        if code not in allowed:
            frappe.throw(
                _("Item {0} is not available on the menu.").format(code)
            )
        if frappe.db.get_value("Item", code, "disabled"):
            frappe.throw(
                _("Item {0} is disabled and cannot be added to the order.").format(code)
            )


def _validate_dine_in_pax(no_of_pax, order_type):
    """Dine In requires a positive integer guest count."""
    if order_type != "Dine In":
        return no_of_pax

    try:
        pax = cint(no_of_pax)
    except Exception:
        pax = 0

    # cint("abc") / None / "" -> 0; reject non-positive and non-integral floats.
    if no_of_pax is None or no_of_pax == "" or pax < 1:
        frappe.throw(_("Number of guests must be a positive integer for Dine In orders."))

    if isinstance(no_of_pax, float) and no_of_pax != int(no_of_pax):
        frappe.throw(_("Number of guests must be a positive integer for Dine In orders."))

    if isinstance(no_of_pax, str) and "." in no_of_pax.strip():
        frappe.throw(_("Number of guests must be a positive integer for Dine In orders."))

    return pax


def _authorize_captain_transfer(invoice):
    pos_invoice_doc = frappe.get_doc("POS Invoice", invoice)

    if not frappe.has_permission("POS Invoice", "write", doc=pos_invoice_doc):
        frappe.throw(_("Not permitted to transfer this order"), frappe.PermissionError)

    pos_profile = pos_invoice_doc.pos_profile
    if not pos_profile:
        frappe.throw(
            _("Cannot verify transfer permission: order has no POS Profile."),
            frappe.PermissionError,
        )

    pos_profile_doc = frappe.get_doc("POS Profile", pos_profile)
    if not _has_role(frappe.get_roles(), pos_profile_doc.transfer_role_permissions):
        frappe.throw(
            _("You are not permitted to transfer captains."),
            frappe.PermissionError,
        )

    return pos_invoice_doc, pos_profile_doc


def _captain_transfer_refusal(pos_invoice_doc, pos_profile_doc, user):
    if user == pos_invoice_doc.waiter:
        return _("The selected user is already the captain of this order.")

    branch_rows = frappe.db.sql(
        """
            SELECT room
            FROM `tabURY User`
            WHERE parent=%s AND user=%s AND parenttype='Branch'
        """,
        (pos_invoice_doc.branch, user),
        as_dict=True,
    )
    if not branch_rows:
        return _("The selected user is not assigned to this branch.")

    if user in frappe.STANDARD_USERS:
        return _("The selected user is not permitted to take table orders.")

    if _has_role(
        frappe.get_roles(user), pos_profile_doc.role_restricted_for_table_order
    ):
        return _("The selected user is not permitted to take table orders.")

    user_doc = frappe.get_doc("User", user)
    if not user_doc.enabled:
        return _("The selected user is not active.")

    if pos_profile_doc.custom_enable_multiple_cashier:
        current_room = frappe.get_value(
            "URY Table", pos_invoice_doc.restaurant_table, "restaurant_room"
        )
        if not any(row.get("room") == current_room for row in branch_rows):
            return _("Captain transfer is not allowed between different rooms")

    return None


def _get_captain_transfer_candidates(pos_invoice_doc, pos_profile_doc):
    rows = frappe.get_all(
        "URY User",
        filters={"parent": pos_invoice_doc.branch, "parenttype": "Branch"},
        fields=["user"],
    )
    candidates = []
    seen = set()
    for row in rows:
        user = row.get("user")
        if not user or user in seen:
            continue
        seen.add(user)
        if _captain_transfer_refusal(pos_invoice_doc, pos_profile_doc, user):
            continue
        user_doc = frappe.get_doc("User", user)
        candidates.append(
            {
                "name": user_doc.name,
                "full_name": getattr(user_doc, "full_name", "") or "",
            }
        )

    return sorted(candidates, key=lambda row: (row["full_name"] or row["name"]).casefold())


def _has_eligible_captain_transfer_target(pos_invoice_doc, pos_profile_doc):
    return bool(_get_captain_transfer_candidates(pos_invoice_doc, pos_profile_doc))


@frappe.whitelist()
def get_table_order_context(table):
    """Return table + active order + assignment + a computed permission map for `table`.

    This is the server-authoritative replacement for the ownership check V1
    performed client-side only (`urypos/src/stores/Table.js:379-397`): a
    Captain without elevated (`transfer_role_permissions`) or billing
    (`role_allowed_for_billing`) access may only view/modify a table's order
    if they are its assigned `waiter`. Combines Frappe DocType permission,
    branch/room scoping, and POS Profile capability fields into one payload
    so the frontend doesn't have to re-derive this per component.
    """
    if not table:
        frappe.throw(_("Please select a table"))

    table_doc = frappe.get_doc("URY Table", table)

    if not frappe.has_permission("URY Table", "read", doc=table_doc):
        frappe.throw(_("Not permitted to view this table"), frappe.PermissionError)

    denied_permissions = {
        "view": False,
        "modify": False,
        "reduce_items": False,
        "remove_items": False,
        "transfer_table": False,
        "transfer_captain": False,
        "print_bill": False,
        "split_bill": False,
        "reprint_kot": False,
        "settle": False,
        "cancel": False,
    }

    try:
        user_branch = getBranch()
    except frappe.ValidationError:
        if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
            user_branch = None
        else:
            raise

    # Branch scoping: deny outright (mirrors get_order_invoice's branch check)
    # rather than leaking table/order details for a table outside the user's
    # active branch.
    if user_branch and table_doc.branch != user_branch:
        return {
            "table": table_doc.as_dict(),
            "order": None,
            "assignment": None,
            "permissions": denied_permissions,
        }

    # Resolve the active order the same way get_order_invoice does: a draft,
    # not-yet-printed POS Invoice on this table or a table merged into it.
    invoices = frappe.get_all(
        "POS Invoice",
        filters={"docstatus": 0, "invoice_printed": 0},
        or_filters={
            "restaurant_table": table,
            "custom_merged_tables": ["like", f"%{table}%"],
        },
        order_by="creation asc",
        limit=1,
    )
    invoice_name = invoices[0].name if invoices else None

    order = None
    invoice = None
    is_mine = False

    if invoice_name:
        invoice = frappe.get_doc("POS Invoice", invoice_name)

        if not frappe.has_permission("POS Invoice", "read", doc=invoice):
            frappe.throw(_("Not permitted to view this order"), frappe.PermissionError)

        if invoice.branch and user_branch and invoice.branch != user_branch:
            frappe.throw(
                _("Not permitted to view orders outside your active branch"),
                frappe.PermissionError,
            )

        order = invoice.as_dict()
        is_mine = frappe.session.user == invoice.waiter

    assignment = (
        {"waiter": invoice.waiter, "is_mine": is_mine} if invoice else None
    )

    pos_profile_name = invoice.pos_profile if invoice else None
    if not pos_profile_name:
        pos_profile_name = frappe.db.exists("POS Profile", {"branch": table_doc.branch})

    if not pos_profile_name:
        # No POS Profile resolvable for this table's branch: fail closed.
        return {
            "table": table_doc.as_dict(),
            "order": order,
            "assignment": assignment,
            "permissions": denied_permissions,
        }

    pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
    user_roles = frappe.get_roles()

    has_elevated_access = _has_role(user_roles, pos_profile.transfer_role_permissions)
    is_billing_user = _has_role(user_roles, pos_profile.role_allowed_for_billing)

    # Ported V1 ownership check (Table.js:379-397): only applies once a table
    # is occupied. A free table has no owner to protect.
    if order:
        can_view = is_mine or has_elevated_access or is_billing_user
    else:
        can_view = True

    # Room scoping: _enforce_order_access() (used by get_order_invoice/
    # sync_order) additionally requires the acting user's assigned room to
    # cover this table's room, unless elevated/billing — mirror that here so
    # this reported permission map can't say "view: true" for a table the
    # mutation APIs would then hard-deny on the next call.
    if can_view and order and not (has_elevated_access or is_billing_user):
        table_room = table_doc.restaurant_room
        if table_room:
            try:
                assigned_rooms = {row.get("name") for row in getRoom()}
            except frappe.ValidationError:
                assigned_rooms = set() if not (
                    frappe.session.user == "Administrator" or "System Manager" in user_roles
                ) else None
            # getRoom() returns room=None for a branch-level assignment —
            # that means access to every room in the branch, not "assigned
            # to a room named None" (found via live E2E test).
            if assigned_rooms is not None and None not in assigned_rooms and table_room not in assigned_rooms:
                can_view = False

    invoice_billed = bool(order) and order.get("invoice_printed") == 1
    can_modify = can_view and (not order or not invoice_billed or is_billing_user)

    order_items = (invoice.get("items") or []) if invoice else []
    can_split_bill = bool(order and can_modify and frappe.has_permission("POS Invoice", "write", doc=invoice)
                          and (len(order_items) >= 2 or any(flt(row.get("qty")) > 1 for row in order_items)))

    # Managers (elevated roles) may correct a sent order even when the
    # profile withholds `remove_items` from ordinary captains.
    may_remove_sent = bool(pos_profile.remove_items) or has_elevated_access
    can_reduce_items = can_modify and may_remove_sent
    can_remove_items = can_modify and may_remove_sent

    can_transfer_table = can_modify and has_elevated_access

    can_transfer_captain = bool(order) and has_elevated_access and bool(
        frappe.has_permission("POS Invoice", "write", doc=invoice)
        and _has_eligible_captain_transfer_target(invoice, pos_profile)
    )

    can_reprint_kot = (
        bool(order)
        and bool(pos_profile.custom_enable_kot_reprint)
        and (is_mine or has_elevated_access)
    )

    # Gated by can_view (not just the bare DocType-level print permission)
    # so a Captain without ownership/elevated/billing access can't bypass the
    # ownership check above by printing another Captain's bill.
    can_print_bill = (
        bool(order)
        and can_view
        and frappe.has_permission("POS Invoice", "print", doc=invoice)
    )

    # Captain has no submit/cancel at the DocType level (see
    # ury/patches/v2_0/default_permissions.py), so these derive from real
    # permission checks rather than a hardcoded role check, and stay correct
    # if DocType permissions change later.
    can_settle = (
        bool(order)
        and is_billing_user
        and frappe.has_permission("POS Invoice", "submit", doc=invoice)
    )
    can_cancel = (
        bool(order)
        and is_billing_user
        and frappe.has_permission("POS Invoice", "cancel", doc=invoice)
    )

    permissions = {
        "view": can_view,
        "modify": can_modify,
        "reduce_items": can_reduce_items,
        "remove_items": can_remove_items,
        "transfer_table": can_transfer_table,
        "transfer_captain": can_transfer_captain,
        "print_bill": can_print_bill,
        "split_bill": can_split_bill,
        "reprint_kot": can_reprint_kot,
        "settle": can_settle,
        "cancel": can_cancel,
    }

    return {
        "table": table_doc.as_dict(),
        "order": order,
        "assignment": assignment,
        "permissions": permissions,
    }


@frappe.whitelist()
def get_captain_context():
    """Return the current session's operational POS context: identity,
    roles, branch, assigned room(s), relevant POS Profile capability fields,
    and current POS opening state.

    Room assignment is sourced from `getRoom()` (`ury.ury_pos.api`), the same
    `URY User` child-table (Branch.user) source used elsewhere for
    branch/room resolution.

    POS opening state mirrors RN `MenuContext.getPos`:
    - single-cashier: branch-wide `posOpening()` (0 = open, 1 = closed)
    - multi-cashier (`custom_enable_multiple_cashier`): room-scoped
      `pos_opening_check().opening_exists` for the user's assigned room
      via `getBranchRoom()` - not any open room on the branch

    Failures while resolving opening state return `opening_state: None`
    (fail closed). Capability field names mirror what
    `derivePOSCapabilities()` in `@ury/core` reads from POS Profile.
    """
    user = frappe.session.user
    user_roles = frappe.get_roles()

    try:
        branch = getBranch()
    except frappe.ValidationError:
        if user == "Administrator" or "System Manager" in user_roles:
            branch = None
        else:
            raise

    rooms = []
    if branch:
        try:
            rooms = getRoom()
        except frappe.ValidationError:
            rooms = []

    pos_profile_name = frappe.db.exists("POS Profile", {"branch": branch}) if branch else None

    pos_profile_context = None
    role_restricted_for_table_order = False
    multi_cashier = False

    if pos_profile_name:
        pos_profile = frappe.get_doc("POS Profile", pos_profile_name)
        multi_cashier = bool(pos_profile.custom_enable_multiple_cashier)

        role_restricted_for_table_order = _has_role(
            user_roles, pos_profile.role_restricted_for_table_order
        )

        pos_profile_context = {
            "name": pos_profile.name,
            "transfer_role_permissions": _has_role(
                user_roles, pos_profile.transfer_role_permissions
            ),
            "role_allowed_for_billing": _has_role(
                user_roles, pos_profile.role_allowed_for_billing
            ),
            "role_restricted_for_table_order": role_restricted_for_table_order,
            "remove_items": bool(pos_profile.remove_items),
            "show_image": bool(pos_profile.show_image),
            "custom_enable_kot_reprint": bool(pos_profile.custom_enable_kot_reprint),
            "custom_enable_multiple_cashier": multi_cashier,
            **get_default_customer_config(pos_profile),
        }

    opening_state = None
    if branch:
        try:
            if multi_cashier:
                # RN parity: open only when the user's assigned room has an
                # open POS Opening Entry (tabMultiple Rooms), not when any
                # room on the branch is open.
                check = pos_opening_check()
                opening_state = {"pos_open": bool(check.get("opening_exists"))}
            else:
                # posOpening() returns 1 when no open POS Opening Entry exists
                # for the branch (and msgprints a notice in that case), 0 when
                # open.
                opening_state = {"pos_open": posOpening() == 0}
        except Exception:
            opening_state = None

    from ury.ury.api.table_reservation import reservations_enabled

    return {
        "user": user,
        "roles": user_roles,
        "branch": branch,
        "rooms": rooms,
        "pos_profile": pos_profile_context,
        "role_restricted_for_table_order": role_restricted_for_table_order,
        "opening_state": opening_state,
        "reservation_enabled": reservations_enabled(branch),
    }


def _order_customer(invoice, profile, order_type, customer):
    if customer:
        return customer
    if invoice.name and invoice.customer:
        return invoice.customer
    return resolve_default_customer(profile, order_type or getattr(invoice, "order_type", None))


def _validate_customer_change(invoice, customer):
    """Allow replacing the customer on an open order, except for Aggregators.

    A walk-in or provisional customer is routinely replaced with the real one
    before payment, and the server accepts that. An Aggregators order's customer
    is the aggregator itself and drives its price list, so it must not change.
    """
    if not getattr(invoice, "name", None):
        return

    current = getattr(invoice, "customer", None)
    if current and current != customer and getattr(invoice, "order_type", None) == "Aggregators":
        frappe.throw(_("The customer of an Aggregators order cannot be changed."))


def _previous_line_snapshot(invoice_items):
    """Snapshot the already-saved invoice lines for the previous-vs-current diff.

    The `reservation_line_key` here is the STABLE, client-supplied line
    identity persisted on `POS Invoice Item-reservation_line_key` (the POS
    cart's `uniqueId`, round-tripped back to the client on reload).

    It deliberately does NOT fall back to `item.name`. The child row's
    autoname is regenerated every time `sync_order` rebuilds `invoice.items`,
    so it cannot carry identity across saves — and worse, using it produced a
    `ref:<item>:<rowname>` key on the PREVIOUS side that the CURRENT side
    could never produce, so EVERY edit to an existing line diffed as
    "remove the old line + add a brand-new one". That is what made a plain
    1 -> 2 quantity bump cancel the original KOT and re-raise a full KOT for
    the new quantity instead of raising a clean +1 delta (B02b).

    A row with no persisted key yields `reservation_line_key: None`, which
    lets `_line_key()` in `ury_order_reservation_service` fall back to its
    context/occurrence keying — the same fallback the current-items side
    uses for an unkeyed line, so the two still agree.
    """
    snapshot = []
    for item in invoice_items or []:
        snapshot.append(
            {
                "reservation_line_key": item.get("reservation_line_key") or None,
                "item_code": item.item_code,
                "item_name": item.item_name,
                "qty": item.qty,
                "comments": item.get("comment") or "",
                "ury_held": cint(item.get("ury_held")),
            }
        )
    return snapshot


def _normalize_current_line_keys(items):
    """Promote whatever stable line identity the client sent into
    `reservation_line_key`, so it is (a) used by the diff and (b) persisted
    by `price_items_for_invoice` for the NEXT sync's previous-side snapshot.

    `_line_ref` is the shared accessor already used by the reservation and
    KOT-generation sides (`reservation_line_key` / `reservation_line_ref` /
    `line_ref` / `pos_line_ref` / `pos_line_id` / `unique_id` / `uniqueId`),
    so no new client contract is invented here — the POS cart's `uniqueId`
    is accepted as-is.
    """
    for item in items or []:
        if item.get("reservation_line_key"):
            continue
        # `name` is last in LINE_REF_FIELDS and is never present on a client
        # payload; guard anyway so a stray echoed row name cannot become an
        # identity (same trap as the previous-side `or item.name` bug).
        probe = {key: value for key, value in item.items() if key != "name"}
        line_ref = _line_ref(probe)
        if line_ref:
            item["reservation_line_key"] = line_ref
    return items


def _backfill_previous_line_keys(previous_items, current_items):
    """Legacy bridge for orders placed BEFORE line keys were persisted.

    Such an invoice's saved rows have no `reservation_line_key`, while the
    client now sends one for every cart line. Without this, the first edit of
    a pre-existing order would still diff as remove+add.

    Adoption is deliberately conservative: a previous unkeyed row inherits a
    current line's key ONLY when that item_code has exactly one unkeyed
    previous row and exactly one current line whose key is not already
    claimed by a keyed previous row. Anything ambiguous (two lines of the
    same item_code with different comments/courses) is left unkeyed and falls
    through to context/occurrence matching, so this can never fuse two
    genuinely distinct lines into one.
    """
    claimed = {p.get("reservation_line_key") for p in previous_items if p.get("reservation_line_key")}

    unkeyed_previous = defaultdict(list)
    for prev in previous_items:
        if not prev.get("reservation_line_key"):
            unkeyed_previous[prev.get("item_code")].append(prev)

    if not unkeyed_previous:
        return previous_items

    available_current = defaultdict(list)
    for cur in current_items or []:
        key = cur.get("reservation_line_key")
        if not key or key in claimed:
            continue
        available_current[cur.get("item") or cur.get("item_code")].append(key)

    for item_code, prev_rows in unkeyed_previous.items():
        candidates = available_current.get(item_code) or []
        if len(prev_rows) == 1 and len(candidates) == 1:
            prev_rows[0]["reservation_line_key"] = candidates[0]

    return previous_items


# Offline capture refusals. Closed and append-only: never renumber or reuse an id.
OFFLINE_REFUSAL_IDS = (
    "URY_CLIENT_ORDER_ID_INVALID",
    "URY_ROUND_ID_EMPTY",
    "URY_CLIENT_ORDER_ID_REBIND",
    "URY_CLIENT_ORDER_ID_MISMATCH",
    "URY_ORDER_NOT_OPEN",
    "URY_OFFLINE_OUTSIDE_SHIFT",
    "URY_OFFLINE_TOTAL_MISMATCH",
    "URY_OFFLINE_APPEND_QTY",
    "URY_OFFLINE_CAPTURE_DISABLED",
    "URY_ROUND_PAYLOAD_MISMATCH",
    "URY_CLIENT_ORDER_ID_IN_USE",
)


def _offline_refusal(refusal_id, message):
    frappe.throw(_("{0}: {1}").format(refusal_id, message), frappe.ValidationError)


def _validate_client_ids(client_order_id, round_id):
    if round_id is not None and not str(round_id).strip():
        _offline_refusal("URY_ROUND_ID_EMPTY", _("Round ID must not be empty."))
    if not client_order_id:
        return None
    try:
        return str(UUID(str(client_order_id)))
    except (ValueError, TypeError, AttributeError):
        _offline_refusal("URY_CLIENT_ORDER_ID_INVALID", _("Client order ID must be a UUID."))


def _client_order_invoice(client_order_id, invoice_name=None, table=None):
    """The order already saved under this client id, after the normal read checks."""
    if not client_order_id:
        return None
    name = frappe.db.get_value(
        "POS Invoice", {"ury_client_order_id": client_order_id}, "name", for_update=True
    )
    if not name:
        return None
    invoice = frappe.get_doc("POS Invoice", name)
    if not frappe.has_permission("POS Invoice", "read", doc=invoice):
        frappe.throw(_("Not permitted to view this order"), frappe.PermissionError)
    user_branch = getBranch()
    if invoice.branch and user_branch and invoice.branch != user_branch:
        frappe.throw(_("Not permitted to view orders outside your active branch"), frappe.PermissionError)
    _enforce_order_access(invoice, deny_message=_("Not permitted to view this order"))
    if invoice_name and invoice_name != invoice.name:
        _offline_refusal("URY_CLIENT_ORDER_ID_MISMATCH", _("Client order ID does not match the requested invoice."))
    if table and table not in _get_table_group(invoice.restaurant_table, invoice.get("custom_merged_tables")):
        _offline_refusal("URY_CLIENT_ORDER_ID_MISMATCH", _("Client order ID does not match the requested table."))
    return invoice


def require_offline_capture(pos_profile):
    """Offline-only inputs are honoured only on a till that opted in to offline capture."""
    if not cint(frappe.db.get_value("POS Profile", pos_profile, "ury_offline_capture")):
        _offline_refusal("URY_OFFLINE_CAPTURE_DISABLED", _("Offline capture is not enabled for this POS Profile."))


def _round_digest(items):
    """Digest of what a round orders (item, quantity); client names, rates and line order are ignored."""
    if isinstance(items, str):
        items = json.loads(items)
    lines = sorted((str(row.get("item") or row.get("item_code") or ""), flt(row.get("qty"))) for row in items or [])
    return hashlib.sha256(json.dumps(lines).encode()).hexdigest()


def _applied_rounds(invoice):
    try:
        rounds = json.loads(invoice.get("ury_applied_rounds") or "{}")
    except ValueError:
        return {}
    return rounds if isinstance(rounds, dict) else {}


def _record_round(invoice, round_id, digest=None):
    invoice.ury_last_round_id = round_id
    rounds = _applied_rounds(invoice)
    rounds.setdefault(str(round_id), digest)
    invoice.ury_applied_rounds = json.dumps(rounds, sort_keys=True)


def _is_applied_round(invoice, round_id, digest=None):
    """Any round already applied to this order, not only the latest; a changed payload is refused."""
    if round_id is None:
        return False
    round_id = str(round_id)
    rounds = _applied_rounds(invoice)
    if round_id in rounds:
        if digest and rounds[round_id] and rounds[round_id] != digest:
            _offline_refusal("URY_ROUND_PAYLOAD_MISMATCH", _("This round was already saved with different items."))
        return True
    # Orders saved before the applied-round ledger only know their latest round.
    return str(invoice.get("ury_last_round_id") or "") == round_id


def _refuse_client_order_rebind(invoice, client_order_id):
    if client_order_id and invoice.get("ury_client_order_id") not in (None, "", client_order_id):
        _offline_refusal("URY_CLIENT_ORDER_ID_REBIND", _("Client order ID cannot be changed for an existing order."))


def _offline_capture_time(captured_at, pos_profile):
    """Capture time in the site timezone, inside this cashier's open shift and not in the future."""
    message = _("Capture must be inside your open POS shift and not in the future.")
    try:
        captured = get_datetime(captured_at)
        if captured and captured.tzinfo:
            captured = captured.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError, OverflowError):
        captured = None
    if not captured:
        _offline_refusal("URY_OFFLINE_OUTSIDE_SHIFT", message)
    opening = frappe.db.get_value(
        "POS Opening Entry",
        {"user": frappe.session.user, "pos_profile": pos_profile, "docstatus": 1, "status": "Open"},
        "period_start_date",
        order_by="period_start_date desc",
    )
    if not opening or not get_datetime(opening) <= captured <= now_datetime():
        _offline_refusal("URY_OFFLINE_OUTSIDE_SHIFT", message)
    return captured


def _kitchen_snapshot(items):
    """Keep stable cart identities; legacy unkeyed rounds compare dish/note totals."""
    rows = []
    legacy = {}
    for item in items:
        if cint(item.get("ury_held")):
            continue
        code = item.get("item") or item.get("item_code")
        note = item.get("comment") or item.get("comments") or ""
        key = item.get("reservation_line_key")
        if not key and (code, note) in legacy:
            legacy[code, note]["qty"] += flt(item.get("qty"))
            continue
        row = dict(item_code=code, item_name=item.get("item_name"), qty=flt(item.get("qty")), comments=note)
        if key:
            row["reservation_line_key"] = key
        else:
            legacy[code, note] = row
        rows.append(row)
    return rows


def _lock_hold_order(invoice, preserve_resolved=False):
    if invoice.name:
        # get_order_invoice resolves these from the current native menu in
        # memory. Reload the persisted held state, not stale menu settings.
        resolved = {field: invoice.get(field) for field in ("selling_price_list", "taxes_and_charges")} if preserve_resolved else {}
        frappe.db.sql("SELECT name FROM `tabPOS Invoice` WHERE name=%s FOR UPDATE", (invoice.name,))
        invoice.flags.for_update = True
        invoice.reload()
        for field, value in resolved.items():
            setattr(invoice, field, value)


@frappe.whitelist()
def fire_items(invoice, line=None, course=None, last_modified_time=None):
    """Fire persisted held rows, never client-supplied quantities/prices/courses."""
    invoice = frappe.get_doc("POS Invoice", invoice)
    _lock_hold_order(invoice)
    if not frappe.has_permission("POS Invoice", "write", doc=invoice):
        frappe.throw(_("Not permitted to modify this order"), frappe.PermissionError)
    branch = getBranch()
    if branch and invoice.branch != branch:
        frappe.throw(_("Not permitted outside your active branch"), frappe.PermissionError)
    _enforce_order_access(invoice, require_modify=True)
    profile = frappe.get_doc("POS Profile", invoice.pos_profile)
    if not cint(getattr(profile, "ury_enable_hold_fire", 0)):
        frappe.throw(_("URY_HOLD_DISABLED: Hold and fire is disabled for this POS Profile."))
    if invoice.docstatus != 0:
        frappe.throw(_("This order is no longer open."))
    if invoice.order_type == "Dine In" and _has_role(
        frappe.get_roles(), profile.role_restricted_for_table_order
    ):
        frappe.throw(_("You are not permitted to take Dine In table orders."), frappe.PermissionError)
    if bool(line) == bool(course):
        frappe.throw(_("Select exactly one line or course to fire."))
    selected = [row for row in invoice.items if row.name == line] if line else [
        row for row in invoice.items if row.get("custom_course") == course
    ]
    if not selected:
        frappe.throw(_("The selected line or course is not on this order."))
    held = [row for row in selected if cint(row.get("ury_held"))]
    if not held:
        return invoice.as_dict()  # retry: nothing is resent, even with an old version
    if last_modified_time and str(invoice.modified) != str(last_modified_time):
        frappe.throw(_("Order has been modified. Reload before firing."))
    previous = _kitchen_snapshot(invoice.items)
    for row in held:
        row.ury_held = 0
    invoice.save()
    # Do not swallow failure: the native request transaction must roll back
    # both the held flags and any partial KOT writes together.
    kot_execute(invoice.name, invoice.customer, invoice.restaurant_table,
                _kitchen_snapshot(invoice.items), previous, invoice.custom_comments)
    return invoice.as_dict()


@frappe.whitelist()
def sync_order(
    items,
    cashier,
    owner,
    mode_of_payment,
    customer,
    no_of_pax,
    last_invoice,
    waiter,
    pos_profile,
    last_modified_time=None,
    table=None,
    invoice=None,
    comments=None,
    order_type=None,
    aggregator_id=None,
    room=None,
    merged_tables=None,
    guest_label=None,
    client_order_id=None,
    round_id=None,
    append=0,
    captured_at=None,
    offline_printed=0,
    expected_total=None,
):
    # None keeps the order's label; any string, blank included, replaces it
    clear_guest_label = guest_label is not None and not str(guest_label).strip()
    guest_label = _guest_label(guest_label)
    # append=1 adds one round to the saved lines (offline replay); it never replaces or removes.
    append = cint(append) == 1
    client_order_id = _validate_client_ids(client_order_id, round_id)
    if append or cint(offline_printed) == 1 or captured_at is not None:
        require_offline_capture(pos_profile)
    round_digest = _round_digest(items) if round_id is not None else None
    user_role = frappe.get_roles()
    posprofile = frappe.get_doc("POS Profile", pos_profile)
    client_invoice = _client_order_invoice(client_order_id, invoice, table)

    billing_user = any(
        role.role in user_role for role in posprofile.role_allowed_for_billing
    )

    # Check if the last invoice was already billed
    if (
        last_invoice
        and frappe.db.get_value("POS Invoice", last_invoice, "invoice_printed") == 1
        and (not billing_user)
    ):
        frappe.msgprint(
            title="Invoice Already Billed",
            indicator="red",
            msg=("This order has already been billed. Please reload the page."),
        )
        return {"status": "Failure"}

    invoice = client_invoice or get_order_invoice(table, invoice, order_type)
    if invoice.name and invoice.pos_profile and invoice.pos_profile != pos_profile:
        frappe.throw(_("URY_PROFILE_MISMATCH: This order belongs to a different POS Profile."), frappe.PermissionError)
    hold_enabled = cint(getattr(posprofile, "ury_enable_hold_fire", 0)) == 1
    if hold_enabled:
        _lock_hold_order(invoice, preserve_resolved=True)
    _refuse_client_order_rebind(invoice, client_order_id)

    if client_invoice:
        # A retry of any round already saved returns the saved order: one invoice, one KOT.
        if _is_applied_round(invoice, round_id, round_digest):
            return invoice.as_dict()
        if invoice.docstatus != 0:
            _offline_refusal("URY_ORDER_NOT_OPEN", _("This order is no longer open."))

    if invoice.name and not frappe.has_permission("POS Invoice", "write", doc=invoice):
        frappe.throw(_("Not permitted to modify this order"), frappe.PermissionError)

    # Reuse Phase 2's ownership formula (_has_role + is_mine/elevated/billing)
    # directly: block a Captain from modifying another Captain's table order,
    # and block modification of an already-billed/printed invoice unless the
    # acting user holds billing access. Only applies once an order actually
    # exists — a brand-new invoice has no owner or billed state to protect.
    elevated_user = _has_role(user_role, posprofile.transfer_role_permissions)
    if invoice.name:
        is_mine = frappe.session.user == invoice.waiter
        can_view_order = is_mine or elevated_user or billing_user
        if not can_view_order:
            frappe.throw(_("Not permitted to modify this order"), frappe.PermissionError)

        if bool(invoice.invoice_printed) and not billing_user:
            frappe.throw(
                _("This order has already been billed and cannot be modified."),
                frappe.PermissionError,
            )

    # role_restricted_for_table_order enforcement + requested-table
    # branch/room validation. Applies to both new and existing orders since
    # `table` reflects the table currently being requested, not just the
    # invoice's already-persisted restaurant_table.
    if table:
        table_branch, table_room = frappe.db.get_value(
            "URY Table", table, ["branch", "restaurant_room"]
        ) or (None, None)

        if not table_branch:
            frappe.throw(_("Table not found."), frappe.PermissionError)

        # Enforce server-side reservation lock check ONLY for NEW table orders or table changes
        is_new_order = not bool(getattr(invoice, "name", None)) or (
            getattr(invoice, "restaurant_table", None) and invoice.restaurant_table != table
        )
        if is_new_order and (order_type == "Dine In" or (invoice and getattr(invoice, "order_type", None) == "Dine In")):
            from ury.ury.api.table_reservation import check_table_reservation, parse_to_datetime
            active_res = check_table_reservation(table)
            if active_res and active_res.get("is_lock_window_active") and active_res.get("status") == "Confirmed":
                res_time_raw = active_res.get("reserved_at")
                formatted_res_time = ""
                if res_time_raw:
                    try:
                        res_dt = parse_to_datetime(res_time_raw)
                        formatted_res_time = res_dt.strftime("%I:%M %p").lstrip("0")
                    except Exception:
                        formatted_res_time = str(res_time_raw)
                frappe.throw(
                    _("Table {0} is reserved for {1}. Please choose another table.").format(table, formatted_res_time)
                )

        try:
            requesting_user_branch = getBranch()
        except frappe.ValidationError:
            if frappe.session.user == "Administrator" or "System Manager" in user_role:
                requesting_user_branch = None
            else:
                raise

        if requesting_user_branch and table_branch != requesting_user_branch:
            frappe.throw(
                _("Not permitted to take orders for a table outside your active branch."),
                frappe.PermissionError,
            )

        if table_room and not (elevated_user or billing_user):
            try:
                assigned_rooms = {row.get("name") for row in getRoom()}
            except frappe.ValidationError:
                if frappe.session.user == "Administrator" or "System Manager" in user_role:
                    assigned_rooms = None
                else:
                    raise

            # getRoom() returns room=None for a branch-level assignment —
            # that means access to every room in the branch, not "assigned
            # to a room named None" (found via live E2E test: this exact
            # throw wrongly blocked a real branch-assigned Captain).
            if assigned_rooms is not None and None not in assigned_rooms and table_room not in assigned_rooms:
                frappe.throw(
                    _("Not permitted to take orders for a table outside your assigned room."),
                    frappe.PermissionError,
                )

    if not append and last_invoice and last_modified_time:
        lastModifiedTime = invoice.modified
        from datetime import datetime

        if isinstance(last_modified_time, str):
            try:
                last_modified_time = datetime.strptime(
                    last_modified_time, "%Y-%m-%d %H:%M:%S.%f"
                )
            except ValueError:
                last_modified_time = datetime.strptime(
                    last_modified_time, "%Y-%m-%d %H:%M:%S"
                )
        if isinstance(lastModifiedTime, str):
            try:
                lastModifiedTime = datetime.strptime(
                    lastModifiedTime, "%Y-%m-%d %H:%M:%S.%f"
                )
            except ValueError:
                lastModifiedTime = datetime.strptime(
                    lastModifiedTime, "%Y-%m-%d %H:%M:%S"
                )
        if lastModifiedTime != last_modified_time:
            frappe.msgprint(
                title="Order has been modified",
                indicator="red",
                msg=(
                    "This order has been modified. Please reload the page to retrieve the latest edits."
                ),
            )
            return {"status": "Failure"}
    elif not (last_invoice and last_modified_time):
        if invoice.name and invoice.invoice_printed == 0 and not billing_user:
            frappe.msgprint(
                title="Table occupied ",
                indicator="red",
                msg=("{0} is already occupied . Please refresh the page.").format(
                    table
                ),
            )
            return {"status": "Failure"}

    past_item = _previous_line_snapshot(invoice.items)
    previous_rows = {row.get("name"): row for row in invoice.items if row.get("name")}

    # Legacy URY clients send JSON; the POS sends an already formatted list.
    if isinstance(items, str):
        items = json.loads(items)
    # Use the same note for authorization, native pricing/storage and KOTs.
    items = [{**item, "comment": item.get("comment") or item.get("comments") or ""} for item in items]

    _normalize_current_line_keys(items)
    has_held = any(cint(item.get("ury_held")) for item in items)
    if has_held and not hold_enabled:
        frappe.throw(_("URY_HOLD_DISABLED: Hold and fire is disabled for this POS Profile."))
    if has_held and (captured_at is not None or cint(offline_printed)):
        frappe.throw(_("URY_HOLD_OFFLINE: Hold and fire requires an online connection."))
    seen_line_names = set()
    seen_keys = {row["reservation_line_key"] for row in past_item if row.get("reservation_line_key")} if append else set()
    for item in items:
        name = item.get("name")
        if name:
            if append:
                frappe.throw(_("Append rounds cannot specify a line name."))
            if name in seen_line_names:
                frappe.throw(_("Repeated line names are not allowed."))
            seen_line_names.add(name)
        old = previous_rows.get(name)
        if name and not old:
            frappe.throw(_("The selected line is not on this order."))
        if old and old.get("reservation_line_key"):
            if item.get("reservation_line_key") not in (None, old.get("reservation_line_key")):
                frappe.throw(_("URY_LINE_KEY_CHANGED: Reload the order before editing this line."))
            item["reservation_line_key"] = old.get("reservation_line_key")
        key = item.get("reservation_line_key")
        if key:
            if key in seen_keys:
                frappe.throw(_("URY_LINE_KEY_REUSED: Order lines must have distinct reservation keys."))
            seen_keys.add(key)
        if old and not cint(old.get("ury_held")) and cint(item.get("ury_held")):
            frappe.throw(_("URY_ALREADY_SENT: Sent lines cannot be held again."))
        if old and cint(old.get("ury_held")) and not cint(item.get("ury_held")):
            frappe.throw(_("URY_HOLD_REQUIRES_FIRE: Use Fire to send held lines."))
    if not append:
        requested_keys = {item["name"]: item.get("reservation_line_key") for item in items if item.get("name")}
        for old_row, snapshot in zip(invoice.items, past_item, strict=True):
            if not snapshot.get("reservation_line_key") and requested_keys.get(old_row.get("name")):
                snapshot["reservation_line_key"] = requested_keys[old_row.get("name")]
        _backfill_previous_line_keys(past_item, items)
    held_names = {name for name, row in previous_rows.items() if cint(row.get("ury_held"))}
    requested_names = {item.get("name") for item in items}
    if not append and held_names - requested_names and any(not item.get("name") for item in items):
        frappe.throw(_("URY_HOLD_REQUIRES_FIRE: Reload the held order before editing."))

    if append and any(flt(item.get("qty")) <= 0 for item in items):
        _offline_refusal("URY_OFFLINE_APPEND_QTY", _("Append rounds must contain only positive quantities."))

    # Authorize before changing headers, payments or lines. Reducing a sent
    # dish+note quantity emits a kitchen cancellation even if the dish total
    # stays unchanged. Match the KOT identity and existing remove_items flag;
    # additions and append-only rounds do not remove the sent baseline.
    if not append and past_item and not _can_remove_sent_items(posprofile, invoice=invoice, pos_profile_name=pos_profile):
        requested_qty_by_item = {}
        for item in items:
            code = item.get("item")
            requested_qty_by_item[code] = requested_qty_by_item.get(code, 0) + flt(item.get("qty"))
        previous_qty_by_item = {}
        for item in past_item:
            code = item["item_code"]
            previous_qty_by_item[code] = previous_qty_by_item.get(code, 0) + flt(item["qty"])
        def sent_quantities(rows):
            totals = defaultdict(float)
            for row in _kitchen_snapshot(rows):
                totals[row["item_code"], row["comments"], row.get("reservation_line_key")] += row["qty"]
            return totals

        sent_before, sent_after = sent_quantities(past_item), sent_quantities(items)
        if (any(requested_qty_by_item.get(code, 0) < qty for code, qty in previous_qty_by_item.items())
                or any(sent_after.get(key, 0) < qty for key, qty in sent_before.items())):
            frappe.throw(
                _("You are not permitted to reduce or remove items from this order."),
                frappe.PermissionError,
            )

    if captured_at is not None:
        captured = _offline_capture_time(captured_at, pos_profile)
        invoice.ury_offline_captured_at = captured
        invoice.set_posting_time = 1
        invoice.posting_date = captured.date()
        invoice.posting_time = captured.time()

    customer = _order_customer(invoice, posprofile, order_type, customer)
    if not customer:
        frappe.throw(_("Select a customer or set a default customer in the POS Profile."))
    else:
        _validate_customer_change(invoice, customer)
        invoice.customer = customer

    if order_type:
        invoice.order_type = order_type

    # role_restricted_for_table_order blocks the Dine-In/table-order path
    # entirely for this role, regardless of ownership/elevated access.
    if invoice.order_type == "Dine In" and _has_role(
        user_role, posprofile.role_restricted_for_table_order
    ):
        frappe.throw(
            _("You are not permitted to take Dine In table orders."),
            frappe.PermissionError,
        )

    customerdoc = frappe.get_doc("Customer", customer)
    invoice.customer_name = customerdoc.customer_name
    invoice.mobile_number = customerdoc.mobile_number
    if comments is not None:
        invoice.custom_comments = comments
    if clear_guest_label:
        invoice.custom_guest_label = ""
    elif guest_label:
        invoice.custom_guest_label = guest_label
    effective_order_type = order_type or invoice.order_type
    invoice.no_of_pax = _validate_dine_in_pax(no_of_pax, effective_order_type)
    invoice.pos_profile = pos_profile
    invoice.company = posprofile.company
    invoice.currency = posprofile.currency
    opening_room = _resolve_sync_opening_room(table=table, room=room, invoice=invoice)
    _require_open_cashier_session(
        posprofile,
        invoice.branch,
        room=opening_room,
        billing_user=billing_user,
        order_type=effective_order_type,
    )
    
    # Secure server-side attribution of cashier and waiter
    multiple_cashier = posprofile.custom_enable_multiple_cashier
    if multiple_cashier:
        pos_opening_list = frappe.db.sql("""
            SELECT DISTINCT `tabPOS Opening Entry`.name 
            FROM `tabPOS Opening Entry`
            INNER JOIN `tabMultiple Rooms` 
            ON `tabMultiple Rooms`.parent = `tabPOS Opening Entry`.name
            WHERE `tabPOS Opening Entry`.branch = %s
            AND `tabPOS Opening Entry`.status = 'Open'
            AND `tabPOS Opening Entry`.docstatus = 1
            AND `tabMultiple Rooms`.room = %s
        """, (invoice.branch, opening_room or room), as_dict=True)

        pos_opened_cashier = frappe.db.get_value("POS Opening Entry", pos_opening_list[0].name, "user") if pos_opening_list else None

        main_cashier = None
        for user_details in posprofile.applicable_for_users:
            if user_details.custom_main_cashier:
                main_cashier = user_details.user
        
        if frappe.session.user == main_cashier:
            invoice.cashier = main_cashier
        else:
            invoice.cashier = pos_opened_cashier
    else:
        invoice.cashier = posprofile.applicable_for_users[0].user if posprofile.applicable_for_users else frappe.session.user

    if not invoice.waiter:
        invoice.waiter = frappe.session.user

    invoice.custom_aggregator_id = aggregator_id
    invoice.custom_restaurant_room = room
    if not invoice.restaurant_table:
        invoice.restaurant_table = table

    if invoice.restaurant_table:
        _reconcile_invoice_merged_tables(invoice)
    
    if order_type == "Aggregators":
        price_list = frappe.db.get_value("Aggregator Settings",{"customer": customer, "parent": invoice.branch, "parenttype": "Branch"},"price_list",)
        
        if not price_list:
            frappe.throw(f"Price list for customer {customer} in branch {invoice.branch} not found in Aggregator Settings.")
    else:
        price_list = invoice.selling_price_list

    # dummy payment
    if invoice.invoice_created == 0:
        invoice.append(
            "payments",
            dict(mode_of_payment=mode_of_payment, amount=0, custom_tender_received=0),
        )
        invoice.invoice_created = 1

    # Reject newly-added lines that are disabled or off the active menu
    # before pricing runs; historic lines already on the invoice are exempt
    # (see _validate_sync_items_against_menu docstring).
    _validate_sync_items_against_menu(
        items,
        past_item,
        invoice.branch,
        table=table,
        room=opening_room or room,
        order_type=effective_order_type,
    )
    from ury.ury.api.ury_availability import apply_count_delta

    apply_count_delta(invoice.branch, past_item, past_item + items if append else items, invoice=invoice)

    menu = _resolve_menu_for_sync(invoice.branch, table=table or invoice.restaurant_table, room=opening_room or room, order_type=effective_order_type) or frappe.db.get_value("URY Menu", {"branch": invoice.branch}, "name")

    priced_items = price_items_for_invoice(
        items, price_list, pos_profile, invoice.branch, menu, posting_date=invoice_pricing_date(invoice),
        customer=invoice.customer,
    )

    _sync_order_company = invoice.company or getattr(posprofile, "company", None) or frappe.db.get_value("Branch", invoice.branch, "company")
    if get_branch_stock_policy(branch=invoice.branch, company=_sync_order_company).reservation_control_enabled:
        reconcile_order_reservations(
            order_ref=_ensure_invoice_reservation_ref(invoice),
            previous_items=past_item,
            accepted_items=past_item + items if append else items,
            branch=invoice.branch,
            company=_sync_order_company,
            actor=frappe.session.user,
        )

    if not append:
        invoice.items = []
    for source, item_dict in zip(items, priced_items, strict=True):
        if source.get("name"):
            item_dict["name"] = source["name"]
        item_dict["ury_held"] = cint(source.get("ury_held"))
        invoice.append("items", item_dict)

    if expected_total is not None:
        # Compare the server-priced, tax-inclusive total without reapplying profile defaults.
        invoice.set_missing_values(for_validate=True)
        invoice.calculate_taxes_and_totals()
        precision = invoice.precision("grand_total")
        if flt(expected_total, precision) != flt(invoice.grand_total, precision):
            _offline_refusal(
                "URY_OFFLINE_TOTAL_MISMATCH", _("Server-priced invoice total differs from the captured total.")
            )

    if client_order_id:
        invoice.ury_client_order_id = client_order_id
        if round_id is not None:
            _record_round(invoice, round_id, round_digest)

    try:
        invoice.save()
    except frappe.UniqueValidationError as e:
        # Lost a race with a concurrent first send of the same client order.
        if client_order_id and frappe.db.get_value(
            "POS Invoice",
            {"ury_client_order_id": client_order_id, "name": ("!=", invoice.name or "")},
            "name",
            for_update=True,
        ):
            _offline_refusal("URY_CLIENT_ORDER_ID_IN_USE", _("This client order is already being saved; retry."))
        frappe.throw(f"Error while updating order: {e}")
    except Exception as e:
        frappe.throw(f"Error while updating order: {e}")   

    try:
        kot_execute(
            invoice.name, customer, table, _kitchen_snapshot(items), [] if append else _kitchen_snapshot(past_item), invoice.custom_comments,
            offline_printed=offline_printed,
        )
    except Exception as e:
        # KOT creation/routing failing is not a side detail to swallow: a
        # customer must never be charged for an item the kitchen never
        # receives. Log for diagnostics, then re-raise so the whole request
        # (including the invoice.save() above) rolls back and the caller
        # sees a real failure instead of a silently accepted order.
        # See sa-post-373-review-fixes Blocker 2.
        error_msg = f"KOT Creation Failed: {str(e)}"
        frappe.log_error(error_msg, "KOT Error")
        frappe.throw(_("Failed to create kitchen order ticket(s) for this order: {0}").format(str(e)))

    # table status
    if invoice.invoice_printed == 0:
        frappe.db.set_value(
            "URY Table", invoice.restaurant_table, {"occupied": 1, "latest_invoice_time": invoice.creation}
        )
        if invoice.custom_merged_tables:
            for merged_table in invoice.custom_merged_tables.split(","):
                frappe.db.set_value(
                    "URY Table", merged_table.strip(), {"occupied": 1, "latest_invoice_time": invoice.creation}
                )

    return invoice.as_dict()


@frappe.whitelist()
def item_query_restaurant(
    doctype="Item",
    txt="",
    searchfield="name",
    start=0,
    page_len=20,
    filters=None,
    as_dict=False,
):
    """Return items that are selected in active menu of the restaurant"""
    restaurant, menu = get_restaurant_and_menu_name(filters["table"])
    items = frappe.db.get_all("URY Menu Item", ["item"], dict(parent=menu, disabled=0))
    del filters["table"]
    filters["name"] = ("in", [d.item for d in items])

    return item_query("Item", txt, searchfield, start, page_len, filters, as_dict)


@frappe.whitelist()
def get_restaurant_and_menu_name(table):
    if not table:
        frappe.throw(_("Please select a table"))

    restaurant, branch, room = frappe.get_value(
        "URY Table",
        table,
        ["restaurant", "branch", "restaurant_room"],
    )
    room_wise_menu = frappe.db.get_value(
        "URY Restaurant",
        restaurant,
        "room_wise_menu",
    )

    if not room_wise_menu:
        menu = frappe.db.get_value("URY Restaurant", restaurant, "active_menu")
    else:
        menu = frappe.db.get_value(
            "Menu for Room",
            {"parent": restaurant, "room": room},
            "menu",
        )

    if not menu:
        frappe.throw(
            _("Please set an active menu for Restaurant {0}").format(restaurant)
        )

    return branch, menu, restaurant

@frappe.whitelist()
def get_menu_name(order_type, branch=None):
    branch = branch or getBranch()
    restaurant = frappe.get_value(
        "URY Restaurant",
        {"branch": branch},
        "name",
    )
    order_type_wise_menu = frappe.db.get_value(
            "URY Restaurant", restaurant, "order_type_wise_menu"
        )
    
    if order_type_wise_menu:
        menu = frappe.db.get_value(
            "Order Type Menu",
            {"parent": restaurant, "order_type": order_type},
            "menu"
        )
        if not menu:
            menu = frappe.db.get_value("URY Restaurant", restaurant, "active_menu")
    else:
        menu = frappe.db.get_value("URY Restaurant", restaurant, "active_menu")   
    return menu  
    

@frappe.whitelist()
def pos_opening_check():
    
    user = frappe.session.user
    # Handle the administrator case differently
    if user == "Administrator":
        return {
            "opening_exists": False,  # Assuming no POS opening entry is needed for Administrator
            "cashier": None,
            "pos_profile": None,
        }
    
    details = getBranchRoom()
    room = details[0].get('name')    # 'Beach'
    branch = details[0].get('branch') # 'Beach'
    
    pos_opening_list = frappe.db.sql("""
        SELECT DISTINCT `tabPOS Opening Entry`.name 
        FROM `tabPOS Opening Entry`
        INNER JOIN `tabMultiple Rooms` 
        ON `tabMultiple Rooms`.parent = `tabPOS Opening Entry`.name
        WHERE `tabPOS Opening Entry`.branch = %s
        AND `tabPOS Opening Entry`.status = 'Open'
        AND `tabPOS Opening Entry`.docstatus = 1
        AND `tabMultiple Rooms`.room = %s
    """, (branch, room), as_dict=True)
    
    
    result = {
        "opening_exists": len(pos_opening_list) > 0,
        "cashier": None,
        "pos_profile": None,
    }

    if result["opening_exists"]:
        # If POS opening entry exists, fetch the cashier from the first entry
        opening_entry = frappe.get_doc("POS Opening Entry", pos_opening_list[0].name)
        result["cashier"] = (
            opening_entry.user
        )  # Fetch values from POS Profile linked to POS Opening Entry
        result["pos_profile"] = opening_entry.pos_profile
        
    return result


@frappe.whitelist()
def table_transfer(table, newTable, invoice):
    current_table = frappe.get_doc("URY Table", table)
    pos_invoice = frappe.get_doc("POS Invoice", invoice)
    new_table = frappe.get_doc("URY Table", newTable)

    if not frappe.has_permission("POS Invoice", "write", doc=pos_invoice):
        frappe.throw(_("Not permitted to transfer this order"), frappe.PermissionError)

    # Explicit server-side transfer-permission check (POS Profile
    # transfer_role_permissions) — the frontend's canCaptainTransfer() is UX
    # only and must not be relied on for authorization.
    if not pos_invoice.pos_profile:
        frappe.throw(
            _("Cannot verify transfer permission: order has no POS Profile."),
            frappe.PermissionError,
        )

    pos_profile_doc = frappe.get_doc("POS Profile", pos_invoice.pos_profile)
    if not _has_role(frappe.get_roles(), pos_profile_doc.transfer_role_permissions):
        frappe.throw(
            _("You are not permitted to transfer tables."),
            frappe.PermissionError,
        )

    merge_members, _table_map = _get_merge_cluster(table)
    if len(merge_members) > 1:
        frappe.throw(_("Table transfer is not allowed for merged tables. Unmerge first."))

    if current_table.branch != new_table.branch:
        frappe.throw(_("Table transfer between different branches is restricted."))

    if new_table.occupied == 1:
        frappe.throw(f"Table {new_table.name} is already occupied")

    from ury.ury.api.table_reservation import (
        check_table_reservation,
        parse_to_datetime,
        transfer_seated_reservation,
        validate_seated_reservation_transfer,
    )
    active_res = check_table_reservation(new_table.name)
    if active_res and active_res.get("is_lock_window_active") and active_res.get("status") == "Confirmed":
        res_time_raw = active_res.get("reserved_at")
        formatted_res_time = ""
        if res_time_raw:
            try:
                res_dt = parse_to_datetime(res_time_raw)
                formatted_res_time = res_dt.strftime("%I:%M %p").lstrip("0")
            except Exception:
                formatted_res_time = str(res_time_raw)
        frappe.throw(
            frappe._("Table {0} is reserved for {1}. Please choose another table.").format(
                new_table.name, formatted_res_time
            )
        )

    old_table_name = current_table.name if isinstance(current_table.name, str) else table
    validate_seated_reservation_transfer(old_table_name, new_table.name, pos_invoice)

    frappe.db.set_value(
        "URY Table",
        new_table.name,
        {"occupied": 1, "latest_invoice_time": pos_invoice.creation},
    )
    other_open_on_old = frappe.db.exists(
        "POS Invoice",
        {"restaurant_table": old_table_name, "docstatus": 0, "name": ["!=", pos_invoice.name]},
    )
    if not other_open_on_old:
        frappe.db.set_value(
            "URY Table",
            current_table.name,
            {"occupied": 0, "latest_invoice_time": None},
        )

    pos_invoice.restaurant_table = new_table.name
    pos_invoice.custom_restaurant_room = new_table.restaurant_room
    pos_invoice.save()

    transfer_seated_reservation(old_table_name, new_table.name, pos_invoice)

    # Do not swallow KOT-transfer failures: a raise here aborts the request
    # (and its transaction) so the table/invoice changes above are rolled
    # back instead of silently leaving order/table/KOT locations
    # inconsistent. An empty matching-KOT set is a valid no-op and does not
    # raise.
    change_table_in_kot(pos_invoice.name, new_table.name, pos_invoice.branch)


@frappe.whitelist()
def get_captain_transfer_candidates(invoice):
    pos_invoice_doc, pos_profile_doc = _authorize_captain_transfer(invoice)
    return _get_captain_transfer_candidates(pos_invoice_doc, pos_profile_doc)


@frappe.whitelist()
def captain_transfer(currentCaptain, newCaptain, invoice):
    pos_invoice_doc, pos_profile_doc = _authorize_captain_transfer(invoice)

    refusal = _captain_transfer_refusal(pos_invoice_doc, pos_profile_doc, newCaptain)
    if refusal:
        frappe.throw(refusal, frappe.PermissionError)

    pos_invoice = pos_invoice_doc
    new_captain_doc = frappe.get_doc("User", newCaptain)

    # Update the waiter field of the POS Invoice
    pos_invoice.waiter = new_captain_doc.name
    pos_invoice.save()


@frappe.whitelist()
def customer_favourite_item(customer_name):
    if not frappe.has_permission("Customer", "read", customer_name):
        frappe.throw(_("Not permitted to access this Customer"), frappe.PermissionError)

    filters = {"customer": customer_name}
    try:
        branch = getBranch()
        if branch:
            filters["branch"] = branch
    except frappe.exceptions.ValidationError:
        pass

    pos = frappe.db.get_list(
        "POS Invoice", filters=filters, fields=["name"]
    )

    item_qty = {}

    for invoice in pos:
        pos_invoice = frappe.get_doc("POS Invoice", invoice)
        for item in pos_invoice.items:
            item_name = item.item_name
            item_qty[item_name] = item_qty.get(item_name, 0) + item.qty

    result = [
        {"item_name": item_name, "qty": qty}
        for item_name, qty in item_qty.items()
        if qty > 1
    ]
    result = sorted(result, key=lambda x: x["qty"], reverse=True)[:3]

    return result


_OTHER_VOID_PREFIX = "Other: "
_VOID_REASON_NOT_CONFIGURED = (
    "URY_VOID_REASON_NOT_CONFIGURED: Choose a configured void reason, "
    "or Other with a note of at least 4 characters."
)


def _configured_void_reasons(pos_profile):
    """One trimmed reason per line. A missing field or profile keeps free text."""
    if not isinstance(pos_profile, str) or not pos_profile.strip():
        return []
    if not frappe.get_meta("POS Profile").has_field("custom_void_reasons"):
        return []
    raw = frappe.db.get_value("POS Profile", pos_profile, "custom_void_reasons")
    if not isinstance(raw, str):
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _cancel_reason_limit():
    """POS Invoice.cancel_reason is Data; a blank length uses Frappe's 140."""
    length = 0
    try:
        field = frappe.get_meta("POS Invoice").get_field("cancel_reason")
        raw = getattr(field, "length", None) if field is not None else None
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            length = 0
        else:
            length = int(raw or 0)
    except Exception:
        length = 0
    return length if length > 0 else 140


def _validated_void_reason(pos_invoice, reason):
    """The trimmed reason to store, or a ValidationError before any side effect."""
    reasons = _configured_void_reasons(getattr(pos_invoice, "pos_profile", None))
    if reasons:
        candidate = reason.strip() if isinstance(reason, str) else None
        note = (
            candidate[len(_OTHER_VOID_PREFIX):]
            if isinstance(candidate, str) and candidate.startswith(_OTHER_VOID_PREFIX)
            else None
        )
        if candidate not in reasons and (note is None or len(note.strip()) < 4):
            frappe.throw(
                _("{0} Configured reasons: {1}").format(
                    _VOID_REASON_NOT_CONFIGURED, "; ".join(reasons),
                ),
                title=_("Invalid void reason"),
            )
        reason = candidate
    elif not isinstance(reason, str) or len(reason.strip()) < 4:
        frappe.throw(
            _("URY_VOID_REASON_TOO_SHORT: Enter a reason for voiding this order (at least 4 characters)."),
            title=_("Invalid void reason"),
        )
    else:
        reason = reason.strip()
    max_length = _cancel_reason_limit()
    if len(reason) > max_length:
        frappe.throw(
            _("URY_VOID_REASON_TOO_LONG: Void reason cannot be longer than {0} characters.").format(max_length),
            title=_("Invalid void reason"),
        )
    return reason


@frappe.whitelist()
def cancel_order(invoice_id, reason):
    pos_invoice = frappe.get_doc("POS Invoice", invoice_id, for_update=True)

    # Authorization gate: the session user must hold cancel rights on this
    # specific invoice before any mutation happens.
    if not frappe.has_permission("POS Invoice", "cancel", doc=pos_invoice):
        frappe.throw(
            _("You do not have permission to cancel this invoice."),
            frappe.PermissionError,
        )

    # Branch scoping: the invoice must belong to the session user's branch
    # (mirrors the getBranch() pattern used across the URY APIs).
    if frappe.session.user != "Administrator":
        user_branch = getBranch()
        if pos_invoice.branch != user_branch:
            frappe.throw(
                _("You are not allowed to cancel an invoice from another branch."),
                frappe.PermissionError,
            )

    if pos_invoice.docstatus == 2:
        frappe.throw(_("Invoice {0} is already cancelled.").format(invoice_id))

    reason = _validated_void_reason(pos_invoice, reason)

    from ury.ury.api.ury_availability import apply_count_delta

    apply_count_delta(pos_invoice.branch, pos_invoice.items, [], invoice=pos_invoice)

    # Release the full merge cluster, not only the primary table and CSV partners.
    if pos_invoice.restaurant_table:
        release_merge_cluster_tables(pos_invoice.restaurant_table, commit=False)

    # KOT cancellation is part of the order-cancellation transaction. If it
    # fails, stop immediately so the invoice is not cancelled while the KOT
    # state remains out of sync.
    cancel_kot(invoice_id)

    # Release any still-active stock reservations held for this order.
    # cancel_order() previously cancelled the KOT/invoice but left
    # `URY Stock Reservation` rows in Reserved status, leaking reserved
    # capacity indefinitely after cancellation (sa-post-373-review-fixes
    # Blocker 3). order_ref is the invoice name, matching how sync_order()
    # reserves via `_ensure_invoice_reservation_ref`.
    release_order_reservations(invoice_id, reason=f"Order cancelled: {reason}" if reason else "Order cancelled")

    # Best-effort delayed-cancellation fraud alert: notify if order was open longer than threshold
    try:
        alert_rule = get_alert_rule("Cancel Delay", branch=pos_invoice.branch)
        if alert_rule:
            creation_time = pos_invoice.creation
            current_time = get_datetime()
            time_diff = current_time - creation_time
            minutes_open = int(time_diff.total_seconds() / 60)

            threshold_minutes = alert_rule.get("threshold_minutes", 0)
            if minutes_open > threshold_minutes:
                # Resolve notify_roles to users and send notifications
                notify_roles = alert_rule.get("notify_roles", [])
                if notify_roles:
                    for role_row in notify_roles:
                        role_name = role_row.get("role") if isinstance(role_row, dict) else role_row.role
                        users = get_users_with_role(role_name, branch=pos_invoice.branch)
                        for user in users:
                            message = f"Invoice {invoice_id} has been cancelled after {minutes_open} minutes"
                            subject = f"Invoice {invoice_id} ({pos_invoice.branch}) is cancelled"
                            create_system_notification(message, user.get("name"), subject)
    except Exception as e:
        # Log error but don't block cancellation
        frappe.log_error(
            title="Delayed cancellation alert notification failed",
            message=f"Failed to send fraud alert for invoice {invoice_id}: {str(e)}"
        )

    if pos_invoice.docstatus == 0:
        pos_invoice.discard()
    else:
        pos_invoice.cancel()
    pos_invoice.db_set("cancel_reason", reason)

def _set_settlement_customer(invoice, customer):
    if invoice.customer != customer:
        invoice.customer = customer
        # Re-resolve party-specific fields through the native controller.
        invoice.customer_name = invoice.debit_to = invoice.party_account_currency = None
        invoice.due_date = invoice.payment_terms_template = None
        invoice.set_missing_values(for_validate=True)


def _lock_settlement_customer(customer, profile):
    from ury.ury_pos.credit import default_accounts

    # Default accounts can never incur AR. Keep ordinary walk-in tills independent.
    if customer and customer not in default_accounts(profile):
        row = frappe.db.sql("SELECT name, disabled FROM `tabCustomer` WHERE name=%s FOR UPDATE", (customer,))
        return row[0][1] if row else None


def _enforce_on_account(invoice, profile, customer_disabled, target=None):
    from ury.ury_pos.credit import balance, credit_limit, default_accounts, require_credit_access

    bills = (invoice, target) if target else (invoice,)
    proposed = 0
    for bill in bills:
        remaining = flt(bill.rounded_total or bill.grand_total) - sum(flt(row.amount) for row in bill.payments)
        if flt(remaining, bill.precision("outstanding_amount")) > 0:
            if flt(bill.conversion_rate) <= 0:
                frappe.throw(_("Missing invoice conversion rate."))
            proposed += remaining * flt(bill.conversion_rate)
    if not proposed:
        return
    customer = invoice.customer
    if not customer or not profile.customer or customer in default_accounts(profile):
        frappe.throw(coded_message("CREDIT-001", _("Select a named customer account.")), frappe.ValidationError)
    require_credit_access(profile)
    if not frappe.has_permission('Customer', 'read', doc=customer):
        frappe.throw(_('Not permitted to use this account.'), frappe.PermissionError)
    if customer_disabled != 0:
        frappe.throw(coded_message("CREDIT-002", _("This customer is inactive.")), frappe.ValidationError)
    limit = credit_limit(customer, invoice.company)
    if limit <= 0:
        frappe.throw(
            coded_message("CREDIT-003", _("This customer has no positive credit limit for the company.")),
            frappe.ValidationError,
        )
    projected = balance(customer, invoice.company, invoice.name, current=True) + proposed
    precision = invoice.precision("base_grand_total")
    if flt(projected, precision) > flt(limit, precision):
        frappe.throw(
            coded_message("CREDIT-004", _("The charge exceeds the available credit.")),
            frappe.ValidationError,
        )
    return True


@frappe.whitelist()
def list_discount_options(pos_profile, invoice=None):
    """Active POS Discount Reasons (in sort_order), valid Coupon Code presets,
    allow_discount_change, and the session user's discount limit %."""
    return _list_discount_options_impl(pos_profile, invoice=invoice)


@frappe.whitelist()
def get_check_discounts(invoice):
    """Read-only discount summary (check + line discounts, reason, note, coupon) for a check."""
    return _get_check_discounts_impl(invoice)


@frappe.whitelist()
def preview_settlement_totals(
    invoice,
    pos_profile,
    additionalDiscount=None,
    discount_percent=None,
    discount_amount=None,
    discount_reason=None,
    discount_note=None,
    coupon_code=None,
    line_discounts=None,
    approval=None,
    clear_discount=0,
):
    """The totals make_invoice / apply_check_discount charges for this check at this discount,
    computed by the native controller (currency precision, smallest currency fraction, system
    rounding, tax-inclusive/exclusive handling). Nothing is saved."""
    spec = parse_discount_request(
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        discount_reason=discount_reason,
        discount_note=discount_note,
        coupon_code=coupon_code,
        line_discounts=line_discounts,
        approval=approval,
        additionalDiscount=additionalDiscount,
        clear_discount=clear_discount,
    )
    bill = frappe.get_doc("POS Invoice", invoice)
    bill.check_permission("read")
    _enforce_order_access(bill)
    if bill.pos_profile and bill.pos_profile != pos_profile:
        frappe.throw(_('Use the order’s own POS Profile.'), frappe.PermissionError)

    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    partner = frappe.get_doc("POS Invoice", bill.custom_merged_pos_invoice) if bill.custom_merged_pos_invoice else None

    bill_snap = _snapshot_doc_discount_state(bill)
    partner_snap = _snapshot_doc_discount_state(partner) if partner is not None else None
    try:
        return apply_discount_to_invoice(
            bill,
            profile,
            spec,
            require_reason=False,
            enforce_auth=False,
            linked_partner=partner,
        )
    finally:
        _restore_doc_discount_state(bill, bill_snap)
        if partner is not None and partner_snap is not None:
            _restore_doc_discount_state(partner, partner_snap)


@frappe.whitelist()
def apply_check_discount(
    invoice,
    pos_profile,
    discount_percent=None,
    discount_amount=None,
    discount_reason=None,
    discount_note=None,
    coupon_code=None,
    line_discounts=None,
    approval=None,
    additionalDiscount=None,
):
    """Apply a check, preset, or line discount onto an open (draft) check before settlement."""
    spec = parse_discount_request(
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        discount_reason=discount_reason,
        discount_note=discount_note,
        coupon_code=coupon_code,
        line_discounts=line_discounts,
        approval=approval,
        additionalDiscount=additionalDiscount,
    )
    bill = frappe.get_doc("POS Invoice", invoice, for_update=True)
    bill.check_permission("write")
    _enforce_order_access(bill)
    if bill.docstatus != 0:
        frappe.throw(_("Discounts can only be applied before settlement."), frappe.ValidationError)
    if bill.pos_profile and bill.pos_profile != pos_profile:
        frappe.throw(_('Use the order’s own POS Profile.'), frappe.PermissionError)

    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    partner = (
        frappe.get_doc("POS Invoice", bill.custom_merged_pos_invoice, for_update=True)
        if bill.custom_merged_pos_invoice
        else None
    )
    summary = apply_discount_to_invoice(
        bill,
        profile,
        spec,
        require_reason=True,
        enforce_auth=True,
        linked_partner=partner,
    )
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    frappe.flags.ury_server_discount_write = True
    try:
        if partner is not None:
            partner.save(ignore_permissions=True)
        bill.save()
    finally:
        frappe.flags.ury_server_discount_write = prev_disc_flag
    return summary


@frappe.whitelist()
def remove_check_discount(invoice, pos_profile, row=None, approval=None):
    """Remove a discount (all discounts or a single line discount) before settlement."""
    bill = frappe.get_doc("POS Invoice", invoice, for_update=True)
    bill.check_permission("write")
    _enforce_order_access(bill)
    if bill.docstatus != 0:
        frappe.throw(_("Discounts can only be removed before settlement."), frappe.ValidationError)
    if bill.pos_profile and bill.pos_profile != pos_profile:
        frappe.throw(_('Use the order’s own POS Profile.'), frappe.PermissionError)

    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    enforce_remove_discount_auth(bill, profile, row=row, approval=approval)
    partner = (
        frappe.get_doc("POS Invoice", bill.custom_merged_pos_invoice, for_update=True)
        if bill.custom_merged_pos_invoice
        else None
    )
    if row:
        target_row = next((r for r in (bill.get("items") or []) if str(r.get("name")) == str(row)), None)
        if not target_row:
            frappe.throw(_("Line item {0} is not on this check.").format(row), frappe.ValidationError)
        target_row["discount_percentage"] = 0
        target_row["discount_amount"] = 0
        if target_row.get("price_list_rate") is not None:
            target_row["rate"] = target_row["price_list_rate"]
        bill.calculate_taxes_and_totals()
        has_remaining = bool(
            flt(bill.get("discount_amount")) > 0
            or flt(bill.get("additional_discount_percentage")) > 0
            or any(
                flt(r.get("discount_percentage")) > 0 or flt(r.get("discount_amount")) > 0
                for r in (bill.get("items") or [])
            )
        )
        if not has_remaining:
            spec = parse_discount_request(clear_discount=1)
            summary = apply_discount_to_invoice(
                bill, profile, spec, require_reason=False, enforce_auth=True, linked_partner=partner
            )
        else:
            from ury.ury_pos.discount import _compute_manual_discount_summary, _read_meta, _write_discount_audit_fields

            prec = currency_precision(bill, bill.get("currency") or profile.get("currency"))
            meta_data = _read_meta(bill)
            line_reasons = dict(meta_data.get("line_reasons") or {})
            line_reasons.pop(str(row), None)
            _write_discount_audit_fields(
                bill,
                reason=bill.get("custom_discount_reason") or meta_data.get("reason"),
                reason_description=meta_data.get("reason_description"),
                note=bill.get("custom_discount_note") or meta_data.get("note"),
                coupon=bill.get("coupon_code") or meta_data.get("coupon_code"),
                approver=bill.get("custom_discount_approver") or meta_data.get("approver"),
                approved_percent=bill.get("custom_discount_approved_percent") or meta_data.get("approved_percent"),
                line_reasons=line_reasons,
                c_hash=meta_data.get("claim_hash"),
            )
            summary = _compute_manual_discount_summary(bill, prec)
            summary["currency"] = bill.get("currency") or profile.get("currency") or ""
            summary["precision"] = prec
    else:
        spec = parse_discount_request(clear_discount=1)
        summary = apply_discount_to_invoice(
            bill, profile, spec, require_reason=False, enforce_auth=True, linked_partner=partner
        )

    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    frappe.flags.ury_server_discount_write = True
    try:
        if partner is not None:
            partner.save(ignore_permissions=True)
        bill.save()
    finally:
        frappe.flags.ury_server_discount_write = prev_disc_flag
    return summary


def _confirmed_phone_payment_requests(invoice_name, company=None):
    if not invoice_name:
        return []
    filters = {
        "reference_doctype": "POS Invoice",
        "reference_name": invoice_name,
        "docstatus": 1,
        "payment_channel": "Phone",
        "payment_request_type": "Inward",
        "status": ["in", ["Paid", "Partially Paid"]],
    }
    if company:
        filters["company"] = company
    rows = frappe.get_all(
        "Payment Request",
        filters=filters,
        fields=["name", "mode_of_payment", "payment_account", "grand_total", "outstanding_amount", "status"],
        order_by="creation asc",
    )
    if not isinstance(rows, (list, tuple)):
        return []
    return [
        pr
        for pr in rows
        if (flt(pr.get("grand_total")) - flt(pr.get("outstanding_amount"))) > 0
    ]


def _received_payment_rows(invoice):
    """Return payment rows that represent money genuinely received on an open check.

    Only rows explicitly marked custom_tender_received=1 by a confirmed server flow, or
    backed by a confirmed Paid/Partially Paid Phone Payment Request, are received.
    Draft/split placeholders, unconfirmed Phone requests, and Step-1 print-preview rows
    have custom_tender_received=0 and are never treated as received.
    """
    rows = []
    matched_pr_names = set()
    matched_phone_modes = {}

    for row in invoice.get("payments") or []:
        amt = flt(row.get("amount"))
        if amt > 0 and cint(row.get("custom_tender_received")) == 1:
            rows.append(row)
            ref = (row.get("reference_no") or "").strip()
            if ref:
                matched_pr_names.add(ref)
            mode = row.get("mode_of_payment")
            if row.get("type") == "Phone" and mode:
                matched_phone_modes[mode] = matched_phone_modes.get(mode, 0.0) + amt

    for pr in _confirmed_phone_payment_requests(invoice.get("name"), invoice.get("company")):
        pr_name = pr.get("name")
        if pr_name in matched_pr_names:
            continue
        paid_amt = flt(pr.get("grand_total")) - flt(pr.get("outstanding_amount"))
        mode = pr.get("mode_of_payment")
        if not mode or paid_amt <= 0:
            continue
        if matched_phone_modes.get(mode, 0.0) >= paid_amt:
            matched_phone_modes[mode] -= paid_amt
            continue
        rows.append(
            {
                "mode_of_payment": mode,
                "amount": paid_amt,
                "account": pr.get("payment_account"),
                "type": "Phone",
                "reference_no": pr_name,
                "custom_tender_received": 1,
            }
        )

    return rows


def _is_phone_mode(row):
    if row.get("type") == "Phone":
        return True
    mode = row.get("mode_of_payment")
    if not mode:
        return False
    return frappe.db.get_value("Mode of Payment", mode, "type") == "Phone"


def _validate_confirmed_phone_payments(invoice, payment_rows):
    """Refuse staging or settling a Phone payment row without a confirmed Paid Payment Request."""
    if invoice.get("is_return"):
        return

    phone_totals = {}
    for row in payment_rows or []:
        amt = flt(row.get("amount"))
        if amt <= 0:
            continue
        if not _is_phone_mode(row):
            continue
        mode = row.get("mode_of_payment")
        phone_totals[mode] = phone_totals.get(mode, 0.0) + amt

    if not phone_totals:
        return

    precision = invoice.precision("paid_amount") if callable(getattr(invoice, "precision", None)) else 2
    if not isinstance(precision, int) or isinstance(precision, bool):
        precision = 2

    for mode, requested_total in phone_totals.items():
        paid_requests = frappe.get_all(
            "Payment Request",
            filters={
                "reference_doctype": "POS Invoice",
                "reference_name": invoice.name,
                "mode_of_payment": mode,
                "docstatus": 1,
                "status": ["in", ["Paid", "Partially Paid"]],
            },
            fields=["name", "grand_total", "outstanding_amount"],
        )
        if not isinstance(paid_requests, (list, tuple)):
            paid_requests = []
        paid_total = sum(
            max(0.0, flt(pr.get("grand_total")) - flt(pr.get("outstanding_amount")))
            for pr in paid_requests
        )
        if flt(paid_total, precision) <= 0:
            frappe.throw(
                _(
                    "Payment for {0} has not been confirmed yet. Confirm the payment or choose another payment method."
                ).format(mode),
                frappe.ValidationError,
            )
        if flt(requested_total, precision) != flt(paid_total, precision):
            frappe.throw(
                _("Confirmed payment for {0} is {1}, not {2}.").format(
                    mode, flt(paid_total, precision), flt(requested_total, precision)
                ),
                frappe.ValidationError,
            )


def _copy_payment_row(row):
    as_dict = getattr(row, "as_dict", None)
    if callable(as_dict):
        data = as_dict(
            no_default_fields=True,
            no_child_table_fields=True,
            no_private_properties=True,
        )
    else:
        data = {
            k: row.get(k)
            for k in (
                "mode_of_payment",
                "amount",
                "reference_no",
                "reference_date",
                "clearance_date",
                "account",
                "type",
                "default",
                "custom_tender_received",
            )
            if row.get(k) is not None
        }
    data["mode_of_payment"] = row.get("mode_of_payment")
    data["amount"] = flt(row.get("amount"))
    if cint(row.get("custom_tender_received")):
        data["custom_tender_received"] = 1
    return data


def _incoming_payment_dict(d):
    # Never copy client-supplied custom_tender_received; Sales Invoice Payment defaults to 0
    # and only server confirmation flows may mark a row received.
    entry = {
        "mode_of_payment": d["mode_of_payment"],
        "amount": flt(d["amount"]),
    }
    for field in ("reference_no", "reference_date", "clearance_date"):
        if d.get(field):
            entry[field] = d[field]
    return entry


def _reconcile_settlement_payments(invoice, incoming_payments):
    """Preserve every server-confirmed received tender and replace unreceived draft rows freely.

    Unreceived rows (custom_tender_received=0: placeholders, unconfirmed Phone requests,
    and provisional Step-1 print rows) are cleanly replaced by incoming_payments. Any row
    explicitly marked custom_tender_received=1 (or backed by a Paid Phone Payment Request)
    keeps its mode, amount, and reference; dropping, reducing, or altering it is refused.
    """
    precision = invoice.precision("paid_amount") if callable(getattr(invoice, "precision", None)) else 2
    if not isinstance(precision, int) or isinstance(precision, bool):
        precision = 2

    existing_rows = _received_payment_rows(invoice)
    if not existing_rows:
        return [_incoming_payment_dict(d) for d in (incoming_payments or [])]

    working = []
    for d in incoming_payments or []:
        amt = flt(d.get("amount"))
        if amt <= 0:
            continue
        working.append(
            {
                "mode_of_payment": d.get("mode_of_payment"),
                "amount": amt,
                "reference_no": (d.get("reference_no") or "").strip(),
                "reference_date": d.get("reference_date"),
                "clearance_date": d.get("clearance_date"),
            }
        )

    indexed = list(enumerate(existing_rows))
    referenced_first = sorted(
        indexed,
        key=lambda pair: (
            0 if (pair[1].get("reference_no") or pair[1].get("clearance_date")) else 1,
            pair[0],
        ),
    )

    preserved_by_idx = {}
    for orig_idx, row in referenced_first:
        mode = row.get("mode_of_payment")
        ex_amt = flt(row.get("amount"))
        ex_ref = (row.get("reference_no") or "").strip()
        ex_clearance = row.get("clearance_date")

        if ex_ref or ex_clearance:
            def _matches_ref(entry):
                if entry["mode_of_payment"] != mode:
                    return False
                if ex_ref and entry["reference_no"] and entry["reference_no"] != ex_ref:
                    return False
                if (
                    ex_clearance
                    and entry["clearance_date"]
                    and str(entry["clearance_date"]) != str(ex_clearance)
                ):
                    return False
                return flt(entry["amount"], precision) >= flt(ex_amt, precision)

            candidates = [entry for entry in working if _matches_ref(entry)]
            candidates.sort(
                key=lambda e: (
                    0
                    if (
                        (ex_ref and e["reference_no"] == ex_ref)
                        or (
                            ex_clearance
                            and e["clearance_date"]
                            and str(e["clearance_date"]) == str(ex_clearance)
                        )
                    )
                    else 1,
                    0 if flt(e["amount"], precision) == flt(ex_amt, precision) else 1,
                )
            )
            if not candidates:
                ref_hint = f" (ref {ex_ref})" if ex_ref else ""
                frappe.throw(
                    _(
                        "Cannot drop, reduce, or alter tender already recorded on check {0} ({1}: {2}{3})."
                    ).format(invoice.name, mode, ex_amt, ref_hint)
                )
            chosen = candidates[0]
            chosen["amount"] = flt(chosen["amount"] - ex_amt, precision)
            if chosen["amount"] <= 0:
                chosen["amount"] = 0
            else:
                if ex_ref and chosen["reference_no"] == ex_ref:
                    chosen["reference_no"] = ""
                    chosen["reference_date"] = None
                if (
                    ex_clearance
                    and chosen["clearance_date"]
                    and str(chosen["clearance_date"]) == str(ex_clearance)
                ):
                    chosen["clearance_date"] = None
            copied = _copy_payment_row(row)
            copied["custom_tender_received"] = 1
            preserved_by_idx[orig_idx] = copied
        else:
            same_mode = [
                entry
                for entry in working
                if entry["mode_of_payment"] == mode
                and not entry["reference_no"]
                and not entry["clearance_date"]
                and flt(entry["amount"], precision) > 0
            ]
            available = flt(sum(entry["amount"] for entry in same_mode), precision)
            if available < flt(ex_amt, precision):
                frappe.throw(
                    _(
                        "Cannot drop or reduce tender already recorded on check {0} ({1}: {2})."
                    ).format(invoice.name, mode, ex_amt)
                )
            exact = next(
                (e for e in same_mode if flt(e["amount"], precision) == flt(ex_amt, precision)),
                None,
            )
            if exact is not None:
                exact["amount"] = 0
            else:
                need = ex_amt
                for entry in same_mode:
                    take = min(entry["amount"], need)
                    entry["amount"] = flt(entry["amount"] - take, precision)
                    need = flt(need - take, precision)
                    if need <= 0:
                        break
            copied = _copy_payment_row(row)
            copied["custom_tender_received"] = 1
            preserved_by_idx[orig_idx] = copied

    result = [preserved_by_idx[i] for i in range(len(existing_rows))]
    for entry in working:
        if flt(entry["amount"], precision) > 0:
            row_dict = {
                "mode_of_payment": entry["mode_of_payment"],
                "amount": entry["amount"],
            }
            for field in ("reference_no", "reference_date", "clearance_date"):
                if entry.get(field):
                    row_dict[field] = entry[field]
            result.append(row_dict)
    return result


# Method for URY POS
@frappe.whitelist()
def make_invoice(
    customer,
    payments,
    cashier,
    pos_profile,
    owner,
    additionalDiscount=None,
    table=None,
    invoice=None,
    submit_invoice=1,
    room_number=None,
    client_order_id=None,
    round_id=None,
    discount_percent=None,
    discount_amount=None,
    discount_reason=None,
    discount_note=None,
    coupon_code=None,
    line_discounts=None,
    approval=None,
    clear_discount=0,
):
    submit_invoice = frappe.utils.cint(submit_invoice) if submit_invoice is not None else 1
    client_order_id = _validate_client_ids(client_order_id, round_id)
    discount_spec = parse_discount_request(
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        discount_reason=discount_reason,
        discount_note=discount_note,
        coupon_code=coupon_code,
        line_discounts=line_discounts,
        approval=approval,
        additionalDiscount=additionalDiscount,
        clear_discount=clear_discount,
    )
    pos_prof = frappe.get_cached_doc("POS Profile", pos_profile)
    customer_disabled = _lock_settlement_customer(customer, pos_prof)
    existing_invoice = _client_order_invoice(client_order_id, invoice, table)
    if existing_invoice:
        # A settled client order answers every settle retry, with or without its round id.
        if existing_invoice.docstatus == 1:
            return {"name": existing_invoice.name, "status": "Submitted"}
        if existing_invoice.docstatus != 0:
            _offline_refusal("URY_ORDER_NOT_OPEN", _("This order is no longer open."))
        invoice = existing_invoice.name
    if invoice and frappe.db.get_value('POS Invoice', invoice, 'docstatus', for_update=True) == 1:
        settled = frappe.get_doc('POS Invoice', invoice)
        settled.check_permission('read')
        _enforce_order_access(settled)
        if settled.branch != getBranch():
            frappe.throw(_('Not permitted outside your branch.'), frappe.PermissionError)
        return {'name': settled.name, 'status': 'Submitted'}

    order_type =  invoice_name = frappe.get_value("POS Invoice",invoice , "order_type")
    invoice = get_order_invoice(table, invoice, order_type, "Payments")
    _check_held_before_settlement(invoice, preserve_resolved=True)
    if invoice.docstatus == 1:
        return {'name': invoice.name, 'status': 'Submitted'}
    if invoice.pos_profile and invoice.pos_profile != pos_profile:
        frappe.throw(_('Use the order’s own POS Profile.'), frappe.PermissionError)
    _refuse_client_order_rebind(invoice, client_order_id)

    if table:
        restaurant = get_restaurant_and_menu_name(table)
        invoice.restaurant = restaurant

    _set_settlement_customer(invoice, customer)
    invoice.pos_profile = pos_profile
    # The settling operator, not the client-sent or originally assigned cashier.
    invoice.cashier = frappe.session.user

    if isinstance(payments, str):
        payments = frappe.parse_json(payments) or []

    if not invoice.get("is_return"):
        for d in payments or []:
            if flt(d.get("amount")) < 0:
                frappe.throw(
                    _("Negative payment amount for {0} is not allowed.").format(
                        d.get("mode_of_payment") or _("tender")
                    )
                )

    existing_received_tenders = _received_payment_rows(invoice)

    # Room Charge: the cashier's room/folio text, for a hotel bridge that adds
    # POS Invoice.room_number; set before payments so its hooks can read it.
    incoming_room = str(room_number).strip() if room_number is not None else ""
    if incoming_room and frappe.get_meta("POS Invoice").has_field("room_number"):
        existing_room = (invoice.get("room_number") or "").strip()
        if existing_received_tenders and existing_room and existing_room != incoming_room:
            frappe.throw(
                _(
                    "Cannot change room-charge reference on check {0} from {1} to {2} after a tender is already recorded."
                ).format(invoice.name, existing_room, incoming_room)
            )
        invoice.room_number = incoming_room
    
    invoice.company = pos_prof.company
    invoice.currency = pos_prof.currency

    target = None
    if invoice.custom_merged_pos_invoice:
        target = frappe.get_doc("POS Invoice", invoice.custom_merged_pos_invoice, for_update=True)
        target.cashier = frappe.session.user

    if discount_spec["has_any_arg"]:
        apply_discount_to_invoice(
            invoice,
            pos_prof,
            discount_spec,
            require_reason=True,
            enforce_auth=True,
            linked_partner=target,
        )
    else:
        invoice.calculate_taxes_and_totals()
        if target is not None:
            target.calculate_taxes_and_totals()

    if target is not None:
        doc_existing = _received_payment_rows(invoice)
        target_existing = _received_payment_rows(target)
        if doc_existing or target_existing:
            combined_existing_check = frappe._dict(
                name=invoice.name,
                company=invoice.get("company"),
                payments=doc_existing + target_existing,
                precision=getattr(invoice, "precision", None),
            )
            reconciled_all = _reconcile_settlement_payments(combined_existing_check, payments)
            new_rows = reconciled_all[len(doc_existing) + len(target_existing):]
            invoice.set("payments", [])
            target.set("payments", [])
            for row in doc_existing:
                copied = _copy_payment_row(row)
                copied["custom_tender_received"] = 1
                invoice.append("payments", copied)
            for row in target_existing:
                copied = _copy_payment_row(row)
                copied["custom_tender_received"] = 1
                target.append("payments", copied)
            doc_req = max(0, flt(invoice.rounded_total or invoice.grand_total) - sum(flt(r.amount) for r in invoice.payments))
            target_req = max(0, flt(target.rounded_total or target.grand_total) - sum(flt(r.amount) for r in target.payments))
            alloc_rows = new_rows
        else:
            invoice.set("payments", [])
            target.set("payments", [])
            doc_req = invoice.rounded_total or invoice.grand_total
            target_req = target.rounded_total or target.grand_total
            alloc_rows = payments

        for d in alloc_rows:
            amt = float(d["amount"])
            mode = d["mode_of_payment"]
            
            if doc_req > 0:
                give = min(amt, doc_req)
                invoice.append("payments", dict(mode_of_payment=mode, amount=give))
                amt -= give
                doc_req -= give
                
            if target_req > 0 and amt > 0:
                give = min(amt, target_req)
                target.append("payments", dict(mode_of_payment=mode, amount=give))
                amt -= give
                target_req -= give
                
        if not target.payments and payments:
            target.append("payments", dict(mode_of_payment=payments[0]["mode_of_payment"], amount=0))
        _validate_confirmed_phone_payments(invoice, invoice.payments)
        _validate_confirmed_phone_payments(target, target.payments)
        if _enforce_on_account(invoice, pos_prof, customer_disabled, target):
            if target.company != invoice.company or target.pos_profile != invoice.pos_profile or target.docstatus != 0:
                frappe.throw(_('Merged credit bills must be open on the same till.'))
            _set_settlement_customer(target, customer)
            target.set_paid_amount()
            target.set_outstanding_amount()
        target.flags.ignore_payment_sync = True
        
        frappe.flags.in_bill_merge_sync = True
        prev_tender_flag = getattr(frappe.flags, "ury_server_tender_received", False)
        prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
        if target_existing:
            frappe.flags.ury_server_tender_received = True
        if discount_spec["has_any_arg"]:
            frappe.flags.ury_server_discount_write = True
        try:
            target.save(ignore_permissions=True)
        finally:
            frappe.flags.ury_server_tender_received = prev_tender_flag
            frappe.flags.ury_server_discount_write = prev_disc_flag
            frappe.flags.in_bill_merge_sync = False
            
        invoice.flags.ignore_payment_sync = True
    else:
        reconciled_payments = _reconcile_settlement_payments(invoice, payments)
        _validate_confirmed_phone_payments(invoice, reconciled_payments)
        invoice.set("payments", [])
        for row_dict in reconciled_payments:
            invoice.append("payments", row_dict)

    invoice.set_paid_amount()
    if not target:
        _enforce_on_account(invoice, pos_prof, customer_disabled)
    invoice.set_outstanding_amount()
    if client_order_id:
        invoice.ury_client_order_id = client_order_id
        if round_id is not None:
            _record_round(invoice, round_id)
    # invoice.owner = owner
    prev_tender_flag = getattr(frappe.flags, "ury_server_tender_received", False)
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    if existing_received_tenders:
        frappe.flags.ury_server_tender_received = True
    if discount_spec["has_any_arg"]:
        frappe.flags.ury_server_discount_write = True
    try:
        invoice.save()
    finally:
        frappe.flags.ury_server_tender_received = prev_tender_flag
        frappe.flags.ury_server_discount_write = prev_disc_flag

    if not submit_invoice:
        return {"name": invoice.name, "status": "Draft"}

    if target:
        # Saving the primary runs on_update, which saves a separate copy of the partner.
        target.reload()
    _submit_and_release_tables(invoice, pos_prof, customer_disabled, target)
    return {"name": invoice.name, "status": "Submitted"}


def _check_held_before_settlement(invoice, preserve_resolved=False):
    _lock_hold_order(invoice, preserve_resolved=preserve_resolved)
    bills = [invoice]
    if invoice.custom_merged_pos_invoice:
        linked = frappe.get_doc("POS Invoice", invoice.custom_merged_pos_invoice)
        _lock_hold_order(linked)
        bills.append(linked)
    if any(cint(row.get("ury_held")) for bill in bills for row in bill.items):
        frappe.throw(_("URY_HELD_NOT_FIRED: Fire or remove held items before settlement."))


def _submit_and_release_tables(invoice, profile, customer_disabled, target=None):
    credit_partner = None
    if _enforce_on_account(invoice, profile, customer_disabled, target) and target:
        if (target.customer, target.company, target.pos_profile, target.docstatus) != (
            invoice.customer, invoice.company, invoice.pos_profile, 0
        ):
            frappe.throw(_('Merged credit bills must belong to the same account and till.'))
        credit_partner = target
    try:
        if credit_partner:
            # Both halves must post in this transaction with the allocated tenders.
            # The ordinary merge hook performs best-effort partner synchronization.
            previous = frappe.flags.in_bill_merge_sync
            frappe.flags.in_bill_merge_sync = True
            try:
                credit_partner.submit()
                invoice.submit()
            finally:
                frappe.flags.in_bill_merge_sync = previous
        else:
            invoice.submit()
    except Exception as e:
        frappe.throw(f"Error while settling order: {e}")

    # Free the table when no other open drafts remain on this table group
    release_invoice = invoice

    # If this invoice is a secondary merged bill,
    # resolve the primary invoice that owns the tables.
    if (
        not release_invoice.restaurant_table
        and release_invoice.custom_merged_pos_invoice
    ):
        release_invoice = frappe.get_doc(
            "POS Invoice",
            release_invoice.custom_merged_pos_invoice,
        )

    if release_invoice.restaurant_table:
        _free_tables_if_no_open_invoices(
            release_invoice.restaurant_table,
            release_invoice.custom_merged_tables,
        )


@frappe.whitelist()
def submit_order_invoice(invoice):
    doc = frappe.get_doc("POS Invoice", invoice)
    if doc.docstatus == 1:
        return {"name": doc.name, "status": "Submitted"}

    customer, profile_name = doc.customer, doc.pos_profile
    profile = frappe.get_cached_doc('POS Profile', profile_name)
    customer_disabled = _lock_settlement_customer(customer, profile)
    _check_held_before_settlement(doc)
    if doc.docstatus == 1:
        return {'name': doc.name, 'status': 'Submitted'}
    if (doc.customer, doc.pos_profile) != (customer, profile_name):
        frappe.throw(_('The order changed; reopen settlement.'))
    target = frappe.get_doc('POS Invoice', doc.custom_merged_pos_invoice, for_update=True) if doc.custom_merged_pos_invoice else None
    _submit_and_release_tables(doc, profile, customer_disabled, target)
    return {"name": doc.name, "status": "Submitted"}
        
        

# Cancel KOT Doc Creation
def cancel_kot(invoice_id):

    pos_invoice = frappe.get_doc("POS Invoice", invoice_id)
    pos_profile_id = pos_invoice.pos_profile
    pos_profile = frappe.get_doc("POS Profile", pos_profile_id)
    kot_naming_series = pos_profile.custom_kot_naming_series
    cancel_kot_naming_series = "CNCL-" + kot_naming_series

    from ury.ury.api.ury_kot_generate import _line_keyed_items

    items = list(_line_keyed_items(_kitchen_snapshot(pos_invoice.items)).values())

    if pos_invoice.restaurant_table:
        restaurant_table = pos_invoice.restaurant_table
    else:
        restaurant_table = None

    # Process items for a canceled KOT
    process_items_for_cancel_kot(
        invoice_id,
        pos_invoice.customer,
        restaurant_table,
        items,
        "",
        pos_profile_id,
        cancel_kot_naming_series,
        "Cancelled",
        items,
    )

    # Set the KOTs associated with the invoice as canceled
    kot_list = frappe.db.get_list(
        "URY KOT",
        filters={
            "invoice": invoice_id,
            "type": ("in", ("New Order", "Order Modified")),
            "docstatus": 1,
        },
        fields=("*"),
    )

    for item in kot_list:
        kot_doc = frappe.get_doc("URY KOT", item.name)
        kot_doc.docstatus = 2
        kot_doc.save()


def change_table_in_kot(invoice, new_table, branch):
    # Get a list of KOTs associated with the POS Invoice
    kot_list = frappe.get_all(
        "URY KOT",
        filters={
            "invoice": invoice,
            "docstatus": 1,
            "order_status": "Ready For Prepare",
            "verified": 0,
        },
    )

    # Update each KOT's restaurant_table and send a real-time update
    for kot in kot_list:
        frappe.db.set_value("URY KOT", kot.name, "restaurant_table", new_table)
        production = frappe.db.get_value("URY KOT", kot.name, "production")
        kot_channel = "{}_{}_{}".format("kot_update", branch, production)
        frappe.publish_realtime(kot_channel)
