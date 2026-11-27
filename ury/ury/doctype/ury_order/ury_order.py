# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import hashlib
import json
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from uuid import UUID
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_datetime, get_system_timezone, now_datetime
from erpnext.controllers.queries import item_query
from ury.brand import coded_message
from ury.ury_pos.api import getBranch, getBranchRoom, getRoom, posOpening
from ury.ury_pos.complimentary import check_is_comped_for_room_charge, set_split_comp_headers
from ury.ury_pos.default_customer import get_default_customer_config, resolve_default_customer
from ury.ury_pos import void_return_reopen as _void_ops
from ury.ury_pos.discount import (
    apply_discount_to_invoice,
    canonical_split_discount_claim,
    currency_precision,
    enforce_remove_discount_auth,
    get_check_discounts as _get_check_discounts_impl,
    list_discount_options as _list_discount_options_impl,
    parse_discount_request,
    user_discount_limit,
    with_currency_precision,
    _above_limit,
    _approver_discount_ceiling,
    _compute_manual_discount_summary,
    _exceeds_approved_percent,
    _get_override_module,
    _is_auto_pricing_rule_row,
    _read_meta as _read_discount_meta,
    _resolve_reason,
    _restore_doc_discount_state,
    _set_row_field,
    _snapshot_doc_discount_state,
    _write_discount_audit_fields,
)
from ury.ury.api.ury_kot_generate import _execute_kot as kot_execute
from ury.ury.api.ury_kot_generate import process_items_for_cancel_kot
from ury.ury.api.ury_production_validation import DIRECT_RETAIL
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
from ury.ury.hooks.ury_pos_tender import sync_charged_tip
from ury.ury_pos.approval_currency import company_approval_amount
from ury.ury_pos.auth_challenge import attach_auth_challenge
from ury.ury_pos.fx import (
    build_offline_collision_plan,
    ensure_local_change_payment_row,
    prepare_settlement_payments,
)

try:
    from ury.ury_pos.pricing_actions import sync_service_charge as _sync_service_charge
except ImportError:  # pragma: no cover - site-less test stubs without full frappe
    def _sync_service_charge(doc, profile=None):
        return None

try:
    from ury.ury_pos.privileged_auth import (
        DEFAULT_MANAGER_ROLES as _PA_DEFAULT_MANAGER_ROLES,
        _is_valid_str as _pa_is_valid_str,
        _table_exists as _pa_table_exists,
        assert_not_efris_fiscalised as _pa_assert_not_efris_fiscalised,
        assert_not_offline_replay as _pa_assert_not_offline_replay,
        authorize_or_perform as _pa_authorize_or_perform,
        is_company_policy_active as _pa_is_company_policy_active,
    )
except ImportError:  # pragma: no cover - site-less test stubs without full frappe
    _PA_DEFAULT_MANAGER_ROLES = frozenset({"URY Manager", "System Manager"})
    _pa_is_valid_str = lambda v: isinstance(v, str) and bool(v.strip())
    _pa_table_exists = lambda dt: False
    _pa_assert_not_efris_fiscalised = lambda doc: None
    _pa_assert_not_offline_replay = lambda **kw: None
    _pa_authorize_or_perform = lambda **kw: {"approver": None, "via_pin": False, "reason": "", "reason_note": ""}
    _pa_is_company_policy_active = lambda company=None: False

from frappe import cache


def _load_ury_pos_submodule(name: str):
    import importlib.util
    import pathlib
    import sys

    fqn = f"ury.ury_pos.{name}"
    existing = sys.modules.get(fqn)
    if existing is not None:
        return existing
    try:
        pkg = __import__("ury.ury_pos", fromlist=[name])
        mod = getattr(pkg, name, None)
        if mod is not None:
            return mod
    except ImportError:
        pass
    sub_path = pathlib.Path(__file__).resolve().parents[3] / "ury_pos" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(fqn, sub_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {fqn} from {sub_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fqn] = mod
    spec.loader.exec_module(mod)
    return mod


def _dayparts_mod():
    import sys

    dayparts = _load_ury_pos_submodule("dayparts")
    if "frappe" not in sys.modules or getattr(dayparts, "frappe", None) is None:
        dayparts.frappe = frappe
    return dayparts


def _tax_mod():
    import sys

    tax_determination = _load_ury_pos_submodule("tax_determination")
    if "frappe" not in sys.modules or getattr(tax_determination, "frappe", None) is None:
        tax_determination.frappe = frappe
    return tax_determination


class URYOrder(Document):
    pass

@frappe.whitelist()
def merge_free_tables(table1, table2, approval_token=None, reason=None, reason_note=None):
    """Merges two tables in the same room; allows one occupied and one free."""
    if approval_token is None and reason is None and reason_note is None:
        return merge_tables_batch(table1, [table2])
    return merge_tables_batch(
        table1,
        [table2],
        approval_token=approval_token,
        reason=reason,
        reason_note=reason_note,
    )


@frappe.whitelist()
def merge_tables_batch(anchor_table, tables, approval_token=None, reason=None, reason_note=None):
    DEFAULT_MANAGER_ROLES = _PA_DEFAULT_MANAGER_ROLES
    _is_valid_str = _pa_is_valid_str
    _table_exists = _pa_table_exists
    assert_not_offline_replay = _pa_assert_not_offline_replay
    authorize_or_perform = _pa_authorize_or_perform
    is_company_policy_active = _pa_is_company_policy_active

    assert_not_offline_replay()

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

    # Resolve POS Profile / Company for `merge_checks` privilege rule enforcement when present.
    if _table_exists("POS Privilege Rule") or approval_token:
        anchor_branch = frappe.db.get_value("URY Table", anchor_table, "branch")
        pos_profile_name = (
            frappe.db.get_value("POS Profile", {"branch": anchor_branch, "disabled": 0}, "name")
            if _is_valid_str(anchor_branch)
            else None
        )
        company = (
            frappe.db.get_value("Branch", anchor_branch, "company")
            if _is_valid_str(anchor_branch)
            else None
        )
        if not _is_valid_str(company) and _is_valid_str(pos_profile_name):
            company = frappe.db.get_value("POS Profile", pos_profile_name, "company")
        policy_active = is_company_policy_active(company)
        if policy_active or approval_token:
            authorize_or_perform(
                privilege="merge_checks",
                pos_profile=pos_profile_name if _is_valid_str(pos_profile_name) else None,
                company=company if _is_valid_str(company) else None,
                skip_profile_company_lookup=True,
                reference_doctype="URY Table",
                reference_name=anchor_table,
                claim={
                    "action": "merge_tables",
                    "anchor_table": anchor_table,
                    "tables": sorted(targets),
                },
                token=approval_token,
                reason=reason,
                reason_note=reason_note,
                require_reason=None if policy_active else False,
                fallback_roles=DEFAULT_MANAGER_ROLES | {"URY Cashier", "URY Captain", "System Manager"},
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

    from ury.ury.api.seating_membership import update_members

    for table_name in sorted(members):
        update_members(frappe, table_name, add=[primary.name])
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
        from ury.ury.api.seating_membership import update_members

        for table in sorted(set(expected) ^ set(current)):
            if table in expected:
                update_members(frappe, table, add=[invoice.name])
            else:
                update_members(frappe, table, remove=[invoice.name])

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


def release_merge_cluster_tables(table_or_tables, commit=True):
    # Pass commit=False from document hooks: Frappe v16 forbids commits inside doc events.

    if isinstance(table_or_tables, (list, tuple, set)):
        cluster = list(table_or_tables)
    else:
        cluster = _get_table_group(table_or_tables)

    from ury.ury.api.table_lifecycle import set_table_state

    for member in sorted(cluster):
        set_table_state(member, "Available", clear_cluster=True)

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
    from ury.ury.api.table_lifecycle import set_table_state, update_table_after_check

    if update_table_after_check(tables, invoice_doc.get("branch"), printed=True):
        return True
    if not _has_open_pos_invoices_for_cluster(tables):
        for table in sorted(tables):
            set_table_state(table, "Available")

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
    *,
    printed=False,
    branch=None,
    commit=True,
):

    if not restaurant_table:
        return

    tables = _get_table_group(
        restaurant_table,
        custom_merged_tables,
    )

    if printed:
        from ury.ury.api.table_lifecycle import update_table_after_check

        if update_table_after_check(tables, branch, printed=True):
            return
    if _has_open_pos_invoices_for_cluster(tables):
        return

    release_merge_cluster_tables(tables, commit=commit)


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
        custom_seat_no=cint(item_row.get("custom_seat_no")),
        custom_origin_invoice_item=item_row.get("custom_origin_invoice_item") or item_row.name,
        custom_kitchen_anchor=item_row.get("custom_kitchen_anchor"),
        ury_held=cint(item_row.get("ury_held")),
        fire_at=item_row.get("fire_at"),
        fire_confirmation_pending=cint(item_row.get("fire_confirmation_pending")),
        cost_center=item_row.cost_center,
        uom=item_row.uom,
        conversion_factor=item_row.conversion_factor,
        stock_uom=item_row.get("stock_uom"),
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
        custom_parent_line_key=item_row.get("custom_parent_line_key") or None,
        custom_line_role=item_row.get("custom_line_role") or "Standard",
        custom_is_modifier=cint(item_row.get("custom_is_modifier")),
        custom_modifier_prefix=item_row.get("custom_modifier_prefix") or "",
        custom_modifier_group=item_row.get("custom_modifier_group") or None,
        custom_modifier_option=item_row.get("custom_modifier_option") or None,
        custom_modifiers_json=item_row.get("custom_modifiers_json") or "",
        custom_bundle_overrides_json=item_row.get("custom_bundle_overrides_json") or "",
        custom_inherit_parent_routing=cint(item_row.get("custom_inherit_parent_routing", 1)),
        custom_hide_unpriced_on_receipt=cint(item_row.get("custom_hide_unpriced_on_receipt")),
    )
    if item_row.get("margin_type"):
        data["margin_type"] = item_row.get("margin_type")
        data["margin_rate_or_amount"] = flt(item_row.get("margin_rate_or_amount"))
    for priv_field in (
        "custom_original_price_list_rate",
        "custom_price_overridden",
        "custom_price_override_reason",
        "custom_price_override_approver",
        "custom_is_open_item",
        "custom_open_item_reference",
        "custom_is_complimentary",
        "custom_comp_type",
        "custom_comp_reason",
        "custom_comp_approver",
        "custom_efris_tax_category",
        "custom_tax_exempt",
        "item_tax_template",
        "item_tax_rate",
    ):
        val = item_row.get(priv_field)
        if val not in (None, "", 0, 0.0):
            data[priv_field] = val
    if cint(item_row.get("ignore_pricing_rule")) == 1 or cint(item_row.get("custom_price_overridden")) == 1:
        data["ignore_pricing_rule"] = 1
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


def _split_kots_to_move(source, move_map, reservations_enabled, *, anchored=None):
    """Move connected tickets only when all their issued lines move together."""
    by_key = defaultdict(dict)
    by_item = defaultdict(list)
    moving_dishes = set()
    for item in source.items:
        by_item[item.item_code].append(item)
        for key in (item.get("reservation_line_key") or item.name, _line_key(item.item_code, item, 1)):
            by_key[key][item.name] = item
        if move_map.get(item.name, 0) and not cint(item.get("ury_held")):
            moving_dishes.add(item.item_code)

    candidates = {}
    retained = set()
    from ury.ury.api.kitchen_ticket_history import issued_line_candidates, round_ledger, split_ticket_families

    for family, rows, ambiguous in split_ticket_families(frappe, source, round_ledger(source)[1]):
        line_qty = defaultdict(Decimal)
        possible_lines, issued_lines = set(), set()
        whole = bool(rows) and not ambiguous
        for row, outstanding in rows:
            if not row.get("reservation_line_key"):
                matches = []
            elif row.get("item"):
                matches = issued_line_candidates(row, [item for item in by_item[row.get("item")]
                                                        if not cint(item.get("ury_held"))])
            else:
                matches = list(by_key.get(row.get("reservation_line_key"), {}).values())
            if len(matches) != 1:
                possible = matches or by_item.get(row.get("item"), ())
                if reservations_enabled and (row.get("item") in moving_dishes
                        or any(move_map.get(item.name, 0) for item in possible)):
                    frappe.throw(coded_message(
                        "SPLIT-RESERVED-KOT",
                        _("The released kitchen line cannot be matched to its stock reservation."),
                    ))
                possible_lines.update(item.name for item in possible)
                whole = False
                continue
            line = matches[0]
            line_qty[line.name] += abs(outstanding)
            if row.get("item") or row.get("quantity"):
                issued_lines.add(line.name)
            quantity = Decimal(str(line.qty))
            if (line.get("custom_kitchen_anchor") or Decimal(str(move_map.get(line.name, 0))) != quantity
                    or line_qty[line.name] > quantity):
                whole = False
        lines = set(line_qty)
        if whole:
            candidates[family] = lines
        else:
            # A blank historical placeholder cannot establish issued stock.
            # An unkeyed row with a dish can still identify possible owners.
            retained.update(issued_lines | possible_lines)

    # A line may have been sent in several rounds and can appear on more
    # than one ticket. Propagate the anchor across the entire component.
    while blocked := [ticket for ticket, lines in candidates.items() if lines & retained]:
        for ticket in blocked:
            retained.update(candidates.pop(ticket))
    if anchored is not None:
        anchored.update(retained)
    return [ticket for family in candidates for ticket in family]


def _has_manual_line_discount(rows):
    """True when any active non-automatic item row carries a manual line discount."""
    for r in rows or []:
        if cint(r.get("is_disposable")) or flt(r.get("qty")) <= 0 or _is_auto_pricing_rule_row(r):
            continue
        if flt(r.get("discount_percentage")) > 0 or flt(r.get("discount_amount")) > 0:
            return True
        plr = flt(r.get("price_list_rate"))
        rate = flt(r.get("rate"))
        if plr > 0 and rate >= 0 and plr > rate + 1e-9:
            return True
    return False


def _load_check_for_split(source_invoice):
    _pa_assert_not_offline_replay()
    try:
        source = frappe.get_doc("POS Invoice", source_invoice, for_update=True)
    except TypeError:
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

    if source.docstatus != 0:
        frappe.throw(_("Only draft invoices can be split."))
    _pa_assert_not_efris_fiscalised(source)

    from ury.ury_pos.outlet_context import assert_outlet_access

    split_profile_name = source.get("pos_profile")
    assert_outlet_access(split_profile_name)
    raw_clock_in_req = frappe.db.get_value(
        "POS Profile", split_profile_name, "custom_require_clock_in_to_order"
    )
    if isinstance(raw_clock_in_req, (int, float, str)) and cint(raw_clock_in_req) == 1:
        from ury.ury_pos import timeclock

        timeclock.enforce_clock_in_for_profile_name(split_profile_name, user=frappe.session.user)

    return source


def _authorize_check_split(source, plans, approval_token, reason, reason_note, *, action):
    # Check Simphony Opt 102 (`custom_max_splits_before_pin`) and `split_check` privilege / PIN.
    pos_profile_name = source.get("pos_profile")
    raw_company = source.get("company")
    company = raw_company if _pa_is_valid_str(raw_company) else (
        frappe.db.get_value("Branch", source.branch, "company") if _pa_is_valid_str(source.branch) else None
    )
    max_splits = 0
    if _pa_is_valid_str(pos_profile_name) and _pa_table_exists("POS Profile"):
        raw_max_splits = frappe.db.get_value("POS Profile", pos_profile_name, "custom_max_splits_before_pin")
        max_splits = max(0, cint(raw_max_splits)) if raw_max_splits is not None else 0

    existing_splits = 0
    if max_splits > 0:
        if source.get("custom_split_group"):
            group_count = frappe.db.count(
                "POS Invoice",
                {"custom_split_group": source.custom_split_group, "docstatus": ["!=", 2]},
            )
            existing_splits = max(0, cint(group_count) - 1)
        if source.get("custom_split_from"):
            existing_splits = max(existing_splits, 1)

    over_split_cap = max_splits > 0 and existing_splits + len(plans) > max_splits
    policy_active = _pa_is_company_policy_active(company)

    # Compute ownership access; a verified Manager PIN can authorise splitting a teammate's or billed check.
    access_ok = True
    try:
        _enforce_order_access(
            source,
            pos_profile_name=pos_profile_name,
            require_modify=True,
            deny_message=coded_message("SPLIT-NOT-PERMITTED", _("Not permitted to split this invoice.")),
        )
    except frappe.PermissionError:
        access_ok = False
        if not approval_token and not policy_active:
            raise

    moves = [[[k, round(flt(v), 4)] for k, v in sorted(plan.items())] for plan in plans]
    claim = {"action": action, "source_invoice": source.name}
    if action == "split_bill":
        claim["move_items"] = moves[0]
    else:
        claim["splits"] = moves
    if policy_active or over_split_cap or approval_token or not access_ok or _pa_table_exists("POS Authorisation Log"):
        _pa_authorize_or_perform(
            privilege="split_check",
            pos_profile=pos_profile_name if _pa_is_valid_str(pos_profile_name) else None,
            company=company if _pa_is_valid_str(company) else None,
            skip_profile_company_lookup=True,
            reference_doctype="POS Invoice",
            reference_name=source.name,
            claim=claim,
            token=approval_token,
            reason=reason,
            reason_note=reason_note,
            require_reason=None if policy_active else False,
            force_pin=over_split_cap,
            allow_fallback_predicate=access_ok,
            deny_message=coded_message("SPLIT-NOT-PERMITTED", _("Not permitted to split this invoice.")),
        )

    return company


@frappe.whitelist()
def split_bill(
    source_invoice,
    items_to_move,
    customer=None,
    guest_label=None,
    approval_token=None,
    reason=None,
    reason_note=None,
    approval=None,
):
    """Move selected line items from a draft bill to a new sibling POS Invoice."""
    if isinstance(items_to_move, str):
        items_to_move = json.loads(items_to_move)
    guest_label = _guest_label(guest_label)
    source = _load_check_for_split(source_invoice)

    move_map = {
        row["name"]: float(row["qty"])
        for row in items_to_move
        if row.get("name") and float(row.get("qty", 0)) > 0
    }
    if not move_map:
        frappe.throw(_("Select at least one item to move."))

    from ury.ury.api.modifier_tree import modifier_edges

    roots = [row for row in source.items if not row.get("custom_parent_line_key")]
    moved_children = set()
    for parent, child in modifier_edges(source.items, roots):
        parent_move = move_map.get(parent.name, 0)
        if parent_move > 0 and flt(parent.qty) > 0:
            ratio = min(1.0, parent_move / flt(parent.qty))
            move_map[child.name] = round(flt(child.qty) * ratio, 6)
            moved_children.add(child.name)
    if any(move_map.get(row.name) and cint(row.get("custom_is_modifier")) and row.name not in moved_children
           for row in source.items):
        frappe.throw(coded_message("MODIFIER-SPLIT-WITH-PARENT",
                                   _("Modifiers must be split together with their parent item.")), frappe.ValidationError)

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

    _validate_split_share_quantities(
        source.items, [{key: Decimal(str(qty)) for key, qty in move_map.items()}],
    )
    company = _authorize_check_split(
        source, [move_map], approval_token, reason, reason_note, action="split_bill",
    )
    return _apply_check_split(source, move_map, company, customer, guest_label, approval, items_to_move)


def _check_split_kitchen_plan(source, move_map, company):
    reservations_enabled = get_branch_stock_policy(branch=source.branch, company=company).reservation_control_enabled
    anchored = set()
    movable = _split_kots_to_move(source, move_map, reservations_enabled, anchored=anchored)
    return reservations_enabled, movable, anchored


def _split_balance_precisions(check, company):
    currency_places = 2 if check.get("currency") == "USD" else currency_precision(check)
    base_currency = check.get("company_currency") or (
        frappe.db.get_value("Company", company, "default_currency") if company else None
    )
    base_places = currency_precision(currency=base_currency) if base_currency else currency_places
    return currency_places, base_places


def _apply_check_split(
    source, move_map, company, customer=None, guest_label=None, approval=None, items_to_move=None,
    *, kitchen_plan=None,
):
    reservations_enabled, movable_kots, anchored_items = (
        kitchen_plan if kitchen_plan is not None else _check_split_kitchen_plan(source, move_map, company)
    )
    anchored_origins = {
        item.get("custom_origin_invoice_item") or item.name for item in source.items
        if item.get("custom_kitchen_anchor") or (item.name in anchored_items and item.name in move_map)
    }
    for item in source.items:
        origin = item.get("custom_origin_invoice_item") or item.name
        moved = move_map.get(item.name, 0)
        if origin in anchored_origins:
            item.custom_kitchen_anchor = item.get("custom_kitchen_anchor") or source.name
        if origin in anchored_origins or 0 < moved < item.qty:
            item.custom_origin_invoice_item = origin
    split_root = source.get("custom_split_from") or source.name

    from ury.ury_pos.split_balances import allocate_split_balances, set_split_balance

    # Freeze balances only on priced documents. Older unpriced drafts still
    # use native pricing when no previous calculation is available.
    balances = None
    if source.get("grand_total") is not None:
        balances = allocate_split_balances(source, move_map, *_split_balance_precisions(source, company))

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
        "custom_tax_exempt",
        "custom_serving_period",
        "company",
        "currency",
        "conversion_rate",
        "price_list_currency",
    ]
    for field in header_fields:
        if source.get(field) is not None:
            new_invoice.set(field, source.get(field))

    from ury.ury.api.kitchen_ticket_history import record_ticket_transfer, round_ledger, set_issued_bindings

    record_ticket_transfer(new_invoice, movable_kots, True)

    bindings = round_ledger(source)[1]
    moved_bindings = {ticket: bindings[ticket] for ticket in movable_kots if ticket in bindings}
    if moved_bindings:
        set_issued_bindings(new_invoice, moved_bindings)
        set_issued_bindings(source, {k: v for k, v in bindings.items() if k not in moved_bindings})

    split_group = source.get("custom_split_group") or frappe.generate_hash(length=10)
    source.custom_split_group = split_group

    new_invoice.custom_split_from = split_root
    new_invoice.custom_split_group = split_group
    new_invoice.invoice_printed = 0
    new_invoice.invoice_created = 0
    # Adversarial split-then-reprint control: child starts at 0 prints while source preserves its print count.
    new_invoice.custom_bill_print_count = 0
    new_invoice.custom_kot_reprint_count = 0
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
        if not item.get("custom_kitchen_anchor")
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

    split_rate_prec = currency_precision(source)
    verified_priv_child_rates = {}
    copied_priv_snapshots = []
    source_priv_snapshots = []

    def _priv_rate_snapshot(row):
        return {
            "rate": flt(row.get("rate"), split_rate_prec),
            "price_list_rate": flt(row.get("price_list_rate"), split_rate_prec),
            "discount_percentage": flt(row.get("discount_percentage")),
            "discount_amount": flt(row.get("discount_amount")),
            "ignore_pricing_rule": 1
            if (cint(row.get("ignore_pricing_rule")) == 1 or cint(row.get("custom_price_overridden")) == 1)
            else 0,
        }

    items_to_remove = []
    source_names, child_names = [], []
    linked_parent_keys = {row.get("custom_parent_line_key") for row in source.items if row.get("custom_parent_line_key")}
    for item in source.items:
        move_qty = move_map.get(item.name, 0)
        is_priv_row = (
            cint(item.get("custom_price_overridden")) == 1
            or cint(item.get("custom_is_complimentary")) == 1
        )
        if is_priv_row and move_qty < item.qty:
            source_priv_snapshots.append((item, _priv_rate_snapshot(item)))
        if move_qty < item.qty:
            source_names.append(item.name)
        if move_qty <= 0:
            continue
        child_names.append(item.name)
        copied_qty = item.qty if move_qty >= item.qty else move_qty
        fields = _copy_invoice_item_fields(item, copied_qty)
        if not fields["reservation_line_key"] and item.name in linked_parent_keys:
            fields["reservation_line_key"] = item.name
        copied_row = new_invoice.append("items", fields)
        if copied_row is None and new_invoice.items:
            copied_row = new_invoice.items[-1]
        if is_priv_row and copied_row is not None:
            snap = _priv_rate_snapshot(item)
            copied_priv_snapshots.append((copied_row, snap))
            if cint(item.get("custom_is_modifier")) == 1:
                verified_priv_child_rates[("row_id", id(copied_row))] = snap["rate"]
                if item.get("reservation_line_key"):
                    verified_priv_child_rates[("rkey", str(item.get("reservation_line_key")))] = snap["rate"]
        if move_qty >= item.qty:
            items_to_remove.append(item)
        else:
            item.qty = float(Decimal(str(item.qty)) - Decimal(str(move_qty)))

    for item in items_to_remove:
        source.remove(item)
    set_split_comp_headers(source, new_invoice)
    if balances:
        set_split_balance(source, balances[0], source_names)
        set_split_balance(new_invoice, balances[1], child_names)

    # Carry bill-level discount & audit metadata cleanly across the split halves.
    has_header_discount = orig_pct > 0 or orig_flat > 0
    has_moved_line_discount = _has_manual_line_discount(new_invoice.items)
    source_has_remaining_line_discount = _has_manual_line_discount(source.items)
    source_has_remaining_discount = has_header_discount or source_has_remaining_line_discount
    if source.get("owner"):
        new_invoice.owner = source.get("owner")

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

    src_meta = _read_discount_meta(source)
    carry_approver = source.get("custom_discount_approver") or src_meta.get("approver")
    carry_approved_pct = flt(source.get("custom_discount_approved_percent")) or flt(src_meta.get("approved_percent"))
    source_had_db_approver = bool(source.get("custom_discount_approver"))

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
        if not new_invoice.get("custom_discount_reason") and src_meta.get("reason"):
            if frappe.get_meta("POS Invoice").has_field("custom_discount_reason"):
                new_invoice.set("custom_discount_reason", src_meta.get("reason"))
        if not new_invoice.get("custom_discount_note") and src_meta.get("note"):
            if frappe.get_meta("POS Invoice").has_field("custom_discount_note"):
                new_invoice.set("custom_discount_note", src_meta.get("note"))
        if carry_approver:
            approval_payload = frappe._dict(
                approver=carry_approver,
                percent=carry_approved_pct or orig_pct,
            )
            new_invoice.flags.discount_approval = approval_payload
            # Only set source.flags.discount_approval when source lacked DB custom_discount_approver
            # (pre-B1 meta-only carry); when source ALREADY has custom_discount_approver in DB,
            # leaving flags.discount_approval unset lets siteops#8620 take its has_prior_approval
            # path so an approving owner splitting the check does not hit SUPERVISOR_SELF_APPROVAL.
            if source_has_remaining_discount and not source_had_db_approver:
                source.flags.discount_approval = approval_payload

    if not source_has_remaining_discount and (source_had_db_approver or carry_approver):
        removal_actor = carry_approver or frappe.session.user
        source.flags.discount_removal = frappe._dict(approver=removal_actor, action="remove")
        source.flags.discount_approval = frappe._dict(approver=removal_actor, percent=0.0, action="remove")
        _write_discount_audit_fields(source)

    payment_mode = source.payments[0].mode_of_payment if source.payments else None
    source_received = _received_payment_rows(source)
    received_total = sum(flt(row.get("amount")) for row in source_received)

    previous_split_flag = getattr(frappe.flags, "ury_bill_split", False)
    frappe.flags.ury_bill_split = True
    prev_tender_flag = getattr(frappe.flags, "ury_server_tender_received", False)
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    frappe.flags.ury_server_discount_write = True
    precision_context = ExitStack()
    try:
        for check in (source, new_invoice):
            ensure_share_quantity_precision(check)
            precision_context.enter_context(with_currency_precision(check, split_rate_prec))
        split_profile = None
        if source.get("pos_profile") and frappe.db.exists("POS Profile", source.pos_profile):
            try:
                split_profile = frappe.get_doc("POS Profile", source.pos_profile)
            except Exception:
                split_profile = None

        source.set_missing_values()
        source.run_method("set_missing_values")
        for row, snap in source_priv_snapshots:
            for field, val in snap.items():
                setattr(row, field, val)
        if split_profile is not None:
            _sync_service_charge(source, split_profile)
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
        for row, snap in copied_priv_snapshots:
            for field, val in snap.items():
                setattr(row, field, val)
        if split_profile is not None:
            _sync_service_charge(new_invoice, split_profile)
        new_invoice.calculate_taxes_and_totals()

        # P1: Guard against discount-% inflation on BOTH split halves (standalone and under #8620).
        if has_header_discount or has_moved_line_discount or source_has_remaining_line_discount or carry_approver:
            split_prec = currency_precision(source)
            source_post_summary = _compute_manual_discount_summary(source, split_prec)
            child_post_summary = _compute_manual_discount_summary(new_invoice, split_prec)
            caller_limit_pct = user_discount_limit(frappe.session.user, company, source.get("pos_profile"))
            prior_approved_pct = carry_approved_pct if carry_approver else 0.0
            reason_code_for_split = source.get("custom_discount_reason") or src_meta.get("reason")
            reason_requires_sup = False
            if reason_code_for_split and (
                source_post_summary["discount_total"] > 0 or child_post_summary["discount_total"] > 0
            ):
                try:
                    r_doc = _resolve_reason(reason_code_for_split, required=False)
                    reason_requires_sup = bool(r_doc and cint(r_doc.get("requires_supervisor")) == 1)
                except Exception:
                    reason_requires_sup = False

            def _check_split_half(half_doc, half_sum):
                if half_sum["discount_total"] <= 0:
                    return False, False, False, 0.0
                half_eff = half_sum["effective_percent"]
                half_req = max(half_eff, flt(half_doc.get("additional_discount_percentage")))
                is_comp_half = bool(
                    half_eff >= 100.0
                    or (half_sum["discount_basis"] > 0 and flt(half_sum["after_total"], split_prec) <= 0)
                )
                exceeds_self = bool(
                    is_comp_half
                    or reason_requires_sup
                    or _above_limit(
                        half_sum["discount_total"],
                        half_sum["discount_basis"],
                        half_doc.get("additional_discount_percentage"),
                        caller_limit_pct,
                        split_prec,
                    )
                )
                exceeds_prior = bool(
                    not carry_approver
                    or _exceeds_approved_percent(half_sum, half_doc, prior_approved_pct, split_prec)
                )
                return exceeds_prior, (exceeds_self and exceeds_prior), is_comp_half, half_req

            src_over_prior, src_needs_reauth, src_comp, src_req_pct = _check_split_half(source, source_post_summary)
            chd_over_prior, chd_needs_reauth, chd_comp, chd_req_pct = _check_split_half(new_invoice, child_post_summary)

            if src_needs_reauth or chd_needs_reauth:
                elevated_pct = max(
                    src_req_pct if src_needs_reauth else 0.0,
                    chd_req_pct if chd_needs_reauth else 0.0,
                )
                split_privilege = "complimentary" if (src_comp or chd_comp) else "discount"
                token = str(approval).strip() if approval is not None and str(approval).strip() else None
                if not token:
                    frappe.throw(
                        coded_message(
                            "POS-AUTH-REQUIRED",
                            _(
                                "Manager approval is required because splitting this check raises the discount percentage to {0}%."
                            ).format(round(elevated_pct, 2)),
                        ),
                        frappe.PermissionError,
                    )
                split_claim = canonical_split_discount_claim(
                    source.name, items_to_move, percent=elevated_pct, reason=reason_code_for_split
                )
                override_mod = _get_override_module()
                consumed = override_mod.consume(
                    token,
                    split_privilege,
                    source.get("pos_profile") or "",
                    source.name,
                    split_claim,
                )
                new_approver = consumed.get("approver")
                if not new_approver or new_approver == frappe.session.user:
                    frappe.throw(
                        coded_message("POS-AUTH-SELF", _("You cannot approve your own discount.")),
                        frappe.PermissionError,
                    )
                approver_ceiling = _approver_discount_ceiling(
                    new_approver, company, pos_profile=source.get("pos_profile"), privilege=split_privilege
                )
                new_approved_pct = elevated_pct
                if approver_ceiling is not None and new_approved_pct > approver_ceiling:
                    new_approved_pct = approver_ceiling
                reauth_payload = frappe._dict(approver=new_approver, percent=new_approved_pct)
                for half_doc, half_needs in (
                    (source, src_needs_reauth or src_over_prior),
                    (new_invoice, chd_needs_reauth or chd_over_prior),
                ):
                    if not half_needs:
                        continue
                    h_meta = _read_discount_meta(half_doc)
                    half_doc.flags.discount_approval = reauth_payload
                    _write_discount_audit_fields(
                        half_doc,
                        reason=half_doc.get("custom_discount_reason") or h_meta.get("reason") or reason_code_for_split,
                        reason_description=h_meta.get("reason_description") or reason_code_for_split,
                        note=half_doc.get("custom_discount_note") or h_meta.get("note") or src_meta.get("note"),
                        coupon=half_doc.get("coupon_code") if hasattr(half_doc, "coupon_code") else None,
                        approver=new_approver,
                        approved_percent=new_approved_pct,
                        line_reasons=h_meta.get("line_reasons") or src_meta.get("line_reasons") or {},
                    )
                carry_approver = new_approver
                carry_approved_pct = new_approved_pct
            elif carry_approver:
                for half_doc, half_over in ((source, src_over_prior), (new_invoice, chd_over_prior)):
                    if not half_over:
                        continue
                    h_meta = _read_discount_meta(half_doc)
                    half_doc.flags.discount_approval = None
                    _write_discount_audit_fields(
                        half_doc,
                        reason=half_doc.get("custom_discount_reason") or h_meta.get("reason") or reason_code_for_split,
                        reason_description=h_meta.get("reason_description") or reason_code_for_split,
                        note=half_doc.get("custom_discount_note") or h_meta.get("note") or src_meta.get("note"),
                        coupon=half_doc.get("coupon_code") if hasattr(half_doc, "coupon_code") else None,
                        approver=None,
                        approved_percent=0.0,
                        line_reasons=h_meta.get("line_reasons") or src_meta.get("line_reasons") or {},
                    )

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

        # When the approving supervisor splits a check they previously approved for another
        # owner, siteops#8620 checks `approver != frappe.session.user` on new_invoice.insert().
        # Caller write/access checks already passed above, so preserve original check ownership
        # across the insert/save of the inherited approval.
        orig_session_user = frappe.session.user
        swap_for_inherited_approval = bool(
            carry_approver
            and carry_approver == orig_session_user
            and source.get("owner")
            and source.get("owner") != carry_approver
        )
        if swap_for_inherited_approval:
            frappe.session.user = source.get("owner")
        if verified_priv_child_rates and getattr(new_invoice, "flags", None) is not None:
            new_invoice.flags.ury_verified_privileged_child_rates = verified_priv_child_rates
        try:
            if swap_for_inherited_approval:
                new_invoice.insert(ignore_permissions=True)
            else:
                new_invoice.insert()
        finally:
            if getattr(new_invoice, "flags", None) is not None:
                new_invoice.flags.ury_verified_privileged_child_rates = None
            if swap_for_inherited_approval:
                frappe.session.user = orig_session_user
        new_invoice.reload()
        frappe.db.set_value(
            "POS Invoice",
            new_invoice.name,
            {
                "custom_split_from": split_root,
                "custom_split_group": split_group,
            },
            update_modified=False,
        )
        if source_received:
            frappe.flags.ury_server_tender_received = True
        record_ticket_transfer(source, movable_kots, new_invoice.name)
        swap_source_save = bool(
            swap_for_inherited_approval and getattr(source.flags, "discount_approval", None) is not None
        )
        if swap_source_save:
            frappe.session.user = source.get("owner")
        try:
            if swap_source_save:
                source.save(ignore_permissions=True)
            else:
                source.save()
        finally:
            if swap_source_save:
                frappe.session.user = orig_session_user

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
            if not item.get("custom_kitchen_anchor")
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
            if not item.get("custom_kitchen_anchor")
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

    finally:
        precision_context.close()
        frappe.flags.ury_server_tender_received = prev_tender_flag
        frappe.flags.ury_server_discount_write = prev_disc_flag
        frappe.flags.ury_bill_split = previous_split_flag

    return {
        "source_invoice": source.name,
        "new_invoice": new_invoice.name,
    }



_ADDED_CHECK_DISCARD = object()


def _shared_kitchen_dependants(check_names):
    item = frappe.qb.DocType("POS Invoice Item")
    invoice = frappe.qb.DocType("POS Invoice")
    return (
        frappe.qb.from_(item)
        .inner_join(invoice).on(invoice.name == item.parent)
        .select(item.name, item.custom_origin_invoice_item)
        .where(item.custom_kitchen_anchor.isin(check_names))
        .where(invoice.name.notin(check_names) & (invoice.docstatus != 2))
        .for_update()
        .run(as_dict=True)
    )


def assert_no_shared_kitchen_void(invoice, method=None):
    """A payment share cannot void a physical ticket or release its stock."""
    if not invoice.get("custom_split_group"):
        return
    if (method == "before_discard"
            and getattr(getattr(invoice, "flags", None), "ury_added_check_discard", None) is _ADDED_CHECK_DISCARD):
        return
    attached = any(row.get("custom_kitchen_anchor") for row in invoice.get("items") or [])
    if attached or _shared_kitchen_dependants([invoice.name]):
        frappe.throw(coded_message(
            "SPLIT-KITCHEN-SHARED",
            _("Shared kitchen items must be consolidated before the check can be voided."),
        ))


def _assert_shared_kitchen_lines_unchanged(previous, accepted):
    current_by_key = {_line_ref(row): row for row in accepted}
    for row in previous:
        if not row.get("custom_kitchen_anchor"):
            continue
        current = current_by_key.get(_line_ref(row))
        if not current or any(
            (current.get(field) or default) != (row.get(field) or default)
            for field, default in (("qty", 0), ("custom_course", ""), ("custom_seat_no", 0))
        ) or (current.get("comment") or current.get("comments") or "") != (row.get("comments") or ""):
            frappe.throw(coded_message(
                "SPLIT-KITCHEN-SHARED", _("Shared kitchen items must be consolidated before changing the line."),
            ))


def ensure_share_quantity_precision(doc, method=None):
    """Keep four-decimal shares through native POS rounding and stock settlement."""
    rows = doc.get("items") or []
    if not any(
        (row.get("custom_origin_invoice_item") or row.get("pos_invoice_item"))
        and flt(row.get("qty")) != round(flt(row.get("qty")), 3) for row in rows
    ):
        return
    for record, parentfield in [(doc, "items"), *((row, None) for row in rows)]:
        if not callable(getattr(record, "precision", None)):
            continue
        for field in ("qty", "stock_qty"):
            precision = record.precision(field, parentfield)
            if isinstance(precision, int) and precision < 4:
                record._precision[parentfield or "main"][field] = 4


_MAX_SPLIT_CHECKS = 20
_SHARE_UNIT = Decimal("0.0001")


def _split_decimal(value):
    try:
        qty = Decimal(str(value))
        if qty.is_finite() and qty > 0 and qty == qty.quantize(_SHARE_UNIT):
            return qty
    except (InvalidOperation, ValueError):
        pass
    frappe.throw(_("Enter a positive split quantity with up to four decimal places."))


def _plan_split_checks(source, splits, mode, split_count):
    rows = source.get("items") or []
    roots = [item for item in rows if not item.get("custom_parent_line_key")]
    from ury.ury.api.modifier_tree import modifier_edges

    edges = list(modifier_edges(rows, roots))
    if len(roots) + len(edges) != len(rows):
        frappe.throw(_("A linked item has no parent on this check."))
    quantities = {item.name: Decimal(str(item.qty)) for item in roots}
    if mode == "even":
        count = _split_decimal(split_count)
        if not 2 <= count <= _MAX_SPLIT_CHECKS or count % 1:
            frappe.throw(_("Choose between 2 and 20 checks."))
        # Round down so the source owns every remaining unit at four decimals.
        plans = [
            {name: (qty / count).quantize(_SHARE_UNIT, rounding=ROUND_DOWN) for name, qty in quantities.items()}
            for _index in range(int(count) - 1)
        ]
    elif mode == "by_seat":
        seats = defaultdict(dict)
        for item in roots:
            seats[cint(item.get("custom_seat_no"))][item.name] = quantities[item.name]
        seat_numbers = sorted(seats)
        plans = [seats[seat] for seat in seat_numbers[1:]]
    elif mode == "manual":
        if not isinstance(splits, list) or not 1 <= len(splits) < _MAX_SPLIT_CHECKS:
            frappe.throw(_("Choose between 2 and 20 checks."))
        plans = []
        for group in splits:
            if not isinstance(group, list):
                frappe.throw(_("Select items for every new check."))
            plan = {}
            for selection in group:
                if not isinstance(selection, dict):
                    frappe.throw(_("Select a valid invoice item."))
                name = selection.get("name")
                if not isinstance(name, str) or name not in quantities or name in plan:
                    frappe.throw(_("Select each parent item only once per check."))
                plan[name] = _split_decimal(selection.get("qty"))
            plans.append(plan)
    else:
        frappe.throw(_("Choose a valid split method."))

    if not plans or len(plans) >= _MAX_SPLIT_CHECKS or any(not plan for plan in plans):
        frappe.throw(_("Select items for every new check."))
    allocated = defaultdict(Decimal)
    for plan in plans:
        for name, qty in list(plan.items()):
            if qty <= 0 or allocated[name] + qty > quantities[name]:
                frappe.throw(_("Split quantities exceed the available quantity or precision."))
            allocated[name] += qty
        for parent, child in edges:
            if parent.name not in plan:
                continue
            child_qty = Decimal(str(child.qty))
            parent_qty = Decimal(str(parent.qty))
            piece = (child_qty * plan[parent.name] / parent_qty).quantize(_SHARE_UNIT, rounding=ROUND_DOWN)
            if allocated[parent.name] == parent_qty:
                piece = child_qty - allocated[child.name]
            if piece <= 0:
                frappe.throw(_("A linked item cannot be split at this quantity."))
            plan[child.name] = piece
            allocated[child.name] += piece
    if not any(qty > allocated[name] for name, qty in quantities.items()):
        frappe.throw(_("At least one item must remain on the original bill."))

    _validate_split_share_quantities(rows, plans)
    return [{name: float(qty) for name, qty in plan.items()} for plan in plans]


def _validate_split_share_quantities(rows, plans):
    for item in rows:
        parts = [plan[item.name] for plan in plans if item.name in plan]
        if not parts:
            continue
        remaining = Decimal(str(item.qty)) - sum(parts)
        if len(parts) == 1 and remaining == 0:
            continue
        pieces = (*parts, remaining)
        conversion = Decimal(str(item.get("conversion_factor") or 1))
        for uom, factor in ((item.get("uom"), Decimal(1)), (item.get("stock_uom"), conversion)):
            if uom and any((qty * factor) % 1 for qty in pieces):
                if cint(frappe.db.get_value("UOM", uom, "must_be_whole_number")):
                    frappe.throw(coded_message("SPLIT-WHOLE-UOM", _("This unit of measure requires whole quantities.")))
        if any(qty % 1 for qty in pieces) and not item.get("custom_parent_line_key") and not cint(
            frappe.db.get_value("Item", item.item_code, "custom_is_shareable")
        ):
            frappe.throw(coded_message("SPLIT-ITEM-NOT-SHAREABLE", _("This item does not allow fractional shares.")))


@frappe.whitelist(methods=["POST"])
def split_check_multi(source_invoice, splits=None, mode="manual", split_count=None,
                      approval_token=None, reason=None, reason_note=None):
    """Create all sibling checks in one transaction with one split approval."""
    savepoint = "ury_split_check_multi"
    frappe.db.savepoint(savepoint)
    try:
        source = _load_check_for_split(source_invoice)
        if isinstance(splits, str):
            splits = frappe.parse_json(splits)
        plans = _plan_split_checks(source, splits, mode, split_count)
        company = _authorize_check_split(
            source, plans, approval_token, reason, reason_note, action="split_check_multi",
        )
        # Resolve every ticket against the intact check. A shared ticket can
        # still contain a line already moved by an earlier sibling.
        kitchen_plans = [_check_split_kitchen_plan(source, plan, company) for plan in plans]
        names = []
        for plan, kitchen_plan in zip(plans, kitchen_plans, strict=True):
            result = _apply_check_split(source, plan, company, kitchen_plan=kitchen_plan)
            names.append(result["new_invoice"])
        return {"source_invoice": source.name, "new_invoices": names}
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise


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
    """Resolve ONE bill-level discount and preserve line/header discount metadata for the
    consolidated check (Simphony Add Check).

    Matching percentages keep that percentage; differing discounts (such as merging a 0%
    check with a 10%-discounted check) sum their exact discount amounts on the shared
    apply_discount_on basis so neither an overcharge nor an unintended discount expansion
    occurs. Conflicting apply_discount_on bases (Net Total vs Grand Total) are refused.
    Line-only discounted checks still carry their discount reason, note, approver, and
    ury_discount_meta onto the consolidated check.
    """
    all_bills = [destination, *sources]
    discounted = []
    line_discounted = []
    for bill in all_bills:
        pct = flt(bill.get("additional_discount_percentage"))
        amt = flt(bill.get("discount_amount"))
        if pct > 0 and amt <= 0:
            bill.calculate_taxes_and_totals()
            amt = flt(bill.get("discount_amount"))
        if pct > 0 or amt > 0:
            discounted.append((bill, pct, amt))
        if _has_manual_line_discount(bill.get("items")):
            line_discounted.append(bill)

    if not discounted and not line_discounted:
        return None

    metadata_bills = []
    seen_bill_ids = set()
    for bill in [b for b, _pct, _amt in discounted] + line_discounted:
        if id(bill) not in seen_bill_ids:
            seen_bill_ids.add(id(bill))
            metadata_bills.append(bill)

    reasons = []
    reason_descriptions = []
    notes = []
    approvers = []
    merged_line_reasons = {}
    for bill in metadata_bills:
        bmeta = _read_discount_meta(bill)
        r = bill.get("custom_discount_reason") or bmeta.get("reason")
        if r:
            reasons.append(r)
            reason_descriptions.append(bmeta.get("reason_description") or r)
        n = bill.get("custom_discount_note") or bmeta.get("note")
        if n:
            notes.append(n)
        ap = bill.get("custom_discount_approver") or bmeta.get("approver")
        if ap:
            ap_pct = flt(bill.get("custom_discount_approved_percent")) or flt(bmeta.get("approved_percent"))
            if ap_pct <= 0:
                ap_pct = _compute_manual_discount_summary(bill, currency_precision(bill))["effective_percent"]
            approvers.append((ap, ap_pct))
        if isinstance(bmeta.get("line_reasons"), dict):
            merged_line_reasons.update(bmeta["line_reasons"])
    carry_reason = reasons[0] if reasons else None
    carry_reason_desc = reason_descriptions[0] if reason_descriptions else carry_reason
    carry_note = "; ".join(dict.fromkeys(notes)) if notes else None
    carry_approver, carry_approved_pct = max(approvers, key=lambda pair: pair[1]) if approvers else (None, 0.0)

    if not discounted:
        return {
            "has_header_discount": False,
            "apply_discount_on": None,
            "additional_discount_percentage": None,
            "discount_amount": None,
            "clear_coupon": False,
            "reason": carry_reason,
            "reason_description": carry_reason_desc,
            "note": carry_note,
            "approver": carry_approver,
            "approved_percent": carry_approved_pct,
            "line_reasons": merged_line_reasons,
        }

    bases = {bill.get("apply_discount_on") or "Grand Total" for bill, _pct, _amt in discounted}
    if len(bases) > 1:
        frappe.throw(
            _("Cannot merge checks with bill discounts applied on different bases ({0}).").format(
                ", ".join(sorted(bases))
            )
        )
    basis = next(iter(bases))

    all_pcts = [flt(bill.get("additional_discount_percentage")) for bill in all_bills]
    coupons = {bill.get("coupon_code") for bill in all_bills if bill.get("coupon_code")}

    if all(p > 0 for p in all_pcts) and len({round(p, 6) for p in all_pcts}) == 1:
        return {
            "has_header_discount": True,
            "apply_discount_on": basis,
            "additional_discount_percentage": all_pcts[0],
            "discount_amount": None,
            "clear_coupon": len(coupons) > 1,
            "reason": carry_reason,
            "reason_description": carry_reason_desc,
            "note": carry_note,
            "approver": carry_approver,
            "approved_percent": carry_approved_pct,
            "line_reasons": merged_line_reasons,
        }

    return {
        "has_header_discount": True,
        "apply_discount_on": basis,
        "additional_discount_percentage": 0,
        "discount_amount": sum(amt for _, _, amt in discounted),
        "clear_coupon": True,
        "reason": carry_reason,
        "reason_description": carry_reason_desc,
        "note": carry_note,
        "approver": carry_approver,
        "approved_percent": carry_approved_pct,
        "line_reasons": merged_line_reasons,
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


def _prepare_split_consolidation(destination, sources, company):
    from ury.ury_pos.split_balances import combine_split_balances

    checks = [destination, *sources]
    if not any(doc.get("custom_split_group") or doc.get("custom_split_balance") for doc in checks):
        return None, None, {}
    if not destination.get("custom_split_group"):
        destination.custom_split_group = next((doc.get("custom_split_group") for doc in sources
                                               if doc.get("custom_split_group")), None)
    dependants = _shared_kitchen_dependants([doc.name for doc in checks])
    balance, line_keys = combine_split_balances(checks, *_split_balance_precisions(destination, company))
    return balance, dependants, line_keys


def _reassign_merged_kitchen_anchors(destination, sources, dependants):
    owners = {doc.name for doc in (destination, *sources)}
    external_origins = {row.custom_origin_invoice_item for row in dependants}
    for row in destination.items:
        if row.get("custom_kitchen_anchor") in owners:
            origin = row.get("custom_origin_invoice_item") or row.name
            row.custom_kitchen_anchor = destination.name if origin in external_origins or None in external_origins else None


def _discard_added_check(source, destination, company, split_consolidation, reservations_enabled):

    if split_consolidation and reservations_enabled:
        # Ownership follows the issued ticket, including quantities reserved
        # for any sibling left outside this merge.
        frappe.db.set_value("URY Stock Reservation", {"order_ref": source.name}, "order_ref",
                            destination.name, update_modified=False)
    elif reservations_enabled:
        reconcile_order_reservations(
            order_ref=source.name,
            previous_items=_reservation_lines(source.items),
            accepted_items=[],
            branch=destination.branch,
            company=company,
            actor=frappe.session.user,
        )

    if split_consolidation:
        # The normal draft discard guard stays in force for all other calls.
        source.flags.ury_added_check_discard = _ADDED_CHECK_DISCARD
        try:
            source.discard()
        finally:
            source.flags.ury_added_check_discard = None
    else:
        source.discard()
    source.db_set("cancel_reason", _("Added to check {0}").format(destination.name))


def _ambiguous_issued_lines():
    frappe.throw(coded_message("KOT-OWNERSHIP", _("Cannot identify the original kitchen ticket for this line.")))


def _issued_merge_claims(check, tickets, interested):
    from ury.ury.api.kitchen_ticket_history import issued_line_candidates

    claims = defaultdict(list)
    sent = [row for row in check.items if not cint(row.get("ury_held"))]
    for ticket in tickets:
        for line in ticket.kot_items:
            matches = issued_line_candidates(line, [row for row in sent if row.item_code == line.item])
            if len(matches) > 1 and any(row.name in interested for row in matches):
                _ambiguous_issued_lines()
            if len(matches) == 1:
                claims[matches[0].name].append((ticket, line))
    return claims


def _bind_issued_merge_keys(checks, reservations_enabled):
    """Resolve invoice-local identities while each ticket still belongs to its issuing check."""
    from ury.ury.api.kitchen_ticket_history import CANCELLATIONS, round_ledger, set_issued_bindings, submitted_tickets

    groups = defaultdict(list)
    for check in checks:
        for row in check.items:
            if not cint(row.get("ury_held")):
                groups[row.get("reservation_line_key") or None].append((check, row))

    interested = {row.name for raw, entries in groups.items() if not raw or len(entries) > 1
                  for _, row in entries}
    histories, claims, bindings = {}, {}, {}
    for check in checks:
        bindings.update(round_ledger(check)[1])
        if any(row.name in interested for row in check.items):
            histories[check.name] = [ticket for ticket in submitted_tickets(frappe, check)
                                     if ticket.type not in CANCELLATIONS]
            for ticket in histories[check.name]:
                for line in ticket.kot_items:
                    line.update(bindings.get(ticket.name, {}).get(line.name, {}))
            claims.update(_issued_merge_claims(check, histories[check.name], interested))

    updates = defaultdict(dict)
    check_names = {check.name for check in checks}
    for raw, entries in groups.items():
        if not any(row.name in interested for _, row in entries):
            continue
        if not raw and not any(row.name in claims for _, row in entries):
            continue
        # Shares of a single release keep one identity, even for an originally
        # unkeyed line. Two origin checks with their own releases never share one.
        issuers = defaultdict(list)
        for check, row in entries:
            if claims.get(row.name):
                issuers[row.get("custom_origin_invoice_item") or row.name].append(check.name)
        keys = {}
        for check, row in entries:
            origin = row.get("custom_origin_invoice_item") or row.name
            owners = issuers[origin]
            if not raw and not owners:
                continue
            if not owners:
                owner = row.get("custom_kitchen_anchor")
            elif check.name in owners:
                owner = check.name
            elif len(owners) == 1:
                owner = owners[0]
            else:
                owner = row.get("custom_kitchen_anchor") or check.name
                if owners and owner not in owners:
                    _ambiguous_issued_lines()
            component = (row.item_code, origin, owner)
            if component not in keys:
                keys[component] = raw if raw and not keys else frappe.generate_hash(length=20)
            new = keys[component]
            issued_key = f"ref:{row.item_code}:{new}"
            for ticket, line in claims.get(row.name, ()):
                if line.get("reservation_line_key") != issued_key:
                    bindings.setdefault(ticket.name, {}).setdefault(line.name, {})["reservation_line_key"] = issued_key
            if new == raw:
                continue
            if row.get("custom_kitchen_anchor") and row.custom_kitchen_anchor not in check_names:
                _ambiguous_issued_lines()
            old = raw or row.name
            updates[check.name].setdefault(row.name, {})["reservation_line_key"] = new
            for child in check.items:
                if child.get("custom_parent_line_key") == old:
                    if sum((r.get("reservation_line_key") or r.name) == old for r in check.items) != 1:
                        _ambiguous_issued_lines()
                    updates[check.name].setdefault(child.name, {})["custom_parent_line_key"] = new
            for ticket in histories[check.name]:
                for child in ticket.kot_items:
                    if child.get("parent_line_key") == old:
                        record = bindings.setdefault(ticket.name, {}).setdefault(child.name, {})
                        record["parent_line_key"] = new
                        key = child.get("reservation_line_key") or ""
                        if key.startswith(old + "::combo::"):
                            record["reservation_line_key"] = new + key[len(old):]

    if updates:
        external = {line.custom_origin_invoice_item for line in _shared_kitchen_dependants(sorted(check_names))}
        if None in external or any((row.get("custom_origin_invoice_item") or row.name) in external
                                   for check in checks for row in check.items if row.name in updates.get(check.name, {})):
            _ambiguous_issued_lines()
    for check in checks:
        changes = updates.get(check.name)
        if changes:
            if reservations_enabled:
                from ury.ury.api.ury_order_reservation_service import rebind_merged_reservations

                rebind_merged_reservations(check, changes)
            for row in check.items:
                if row.name in changes:
                    row.update(changes[row.name])
    if bindings:
        set_issued_bindings(checks[0], bindings)


def _merged_kitchen_keys(checks, line_keys):
    """Keep issued identities and separate held groups that arrive from another check."""
    claimed = {row.get("reservation_line_key") or row.name for check in checks for row in check.items
               if not cint(row.get("ury_held"))}
    representatives = {}
    updates = {}
    for check in checks:
        parents = defaultdict(list)
        for row in check.items:
            old = row.get("reservation_line_key") or row.name
            key = old
            if cint(row.get("ury_held")):
                representative = line_keys.get(row.name, row.name)
                if representative not in representatives:
                    key = frappe.generate_hash(length=20) if old in claimed else old
                    representatives[representative] = key
                    claimed.add(key)
                key = representatives[representative]
                if key != old:
                    updates[row.name] = {"reservation_line_key": key}
            parents[old].append((row.get("fire_at"), key))
        for row in check.items:
            parent = row.get("custom_parent_line_key")
            if parent and parent in parents:
                candidates = parents[parent]
                matches = {key for deadline, key in candidates
                           if len(candidates) == 1 or deadline == row.get("fire_at")}
                if len(matches) != 1:
                    _ambiguous_held_lines()
                if parent not in matches:
                    updates.setdefault(row.name, {})["custom_parent_line_key"] = matches.pop()
    return updates


def _scope_whole_check_comp_for_merge(check):
    """Keep the original guest's authorized comp when another check joins."""
    if not cint(check.get("custom_is_complimentary")) or flt(check.get("additional_discount_percentage")) != 100:
        return
    from ury.ury_pos.pricing_actions import _set_line_comp

    from ury.ury_pos.split_balances import allocate_split_balances, set_split_balance

    if not check.get("custom_split_balance"):
        snapshot, _ = allocate_split_balances(check, {}, *_split_balance_precisions(check, check.company))
        set_split_balance(check, snapshot, [row.name for row in check.items])
    for row in check.items:
        if not cint(row.get("custom_is_complimentary")):
            _set_line_comp(row, check.get("custom_comp_type"), check.get("custom_comp_reason"),
                           check.get("custom_comp_approver"))
    check.flags.ury_converting_grand_total_comp = check.get("apply_discount_on") == "Grand Total"
    try:
        with _split_discount_pricing(check, None):
            check.additional_discount_percentage = check.discount_amount = 0
    finally:
        check.flags.ury_converting_grand_total_comp = None


def _consolidated_check_is_complimentary(checks):
    """A full-check comp can be recorded on the header or on individual lines."""
    has_comp = False
    for check in checks:
        header_comp = cint(check.get("custom_is_complimentary"))
        if header_comp:
            has_comp = True
            if flt(check.get("additional_discount_percentage")) == 100:
                continue
        for row in check.items:
            if cint(row.get("custom_is_complimentary")):
                has_comp = True
            elif not cint(row.get("is_disposable")) and flt(row.get("qty")) > 0 and flt(row.get("rate")) > 0:
                return False
    return has_comp


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
    source_tenders, carried_room = _validate_and_collect_source_tenders(destination, sources)
    checks = [destination, *sources]
    if any(check.get("custom_split_group") or check.get("custom_split_balance") for check in checks):
        for check in checks:
            _scope_whole_check_comp_for_merge(check)
    discount_plan = _prepare_consolidated_discount(destination, sources)

    company = destination.company or frappe.db.get_value("Branch", destination.branch, "company")
    reservations_enabled = get_branch_stock_policy(
        branch=destination.branch, company=company
    ).reservation_control_enabled
    _bind_issued_merge_keys(checks, reservations_enabled)
    split_balance, dependants, line_keys = _prepare_split_consolidation(destination, sources, company)
    split_consolidation = dependants is not None
    merged_comp = _consolidated_check_is_complimentary(checks)
    kitchen_updates = _merged_kitchen_keys(checks, line_keys)
    if reservations_enabled and kitchen_updates:
        from ury.ury.api.ury_order_reservation_service import rebind_merged_reservations

        for check in checks:
            rebind_merged_reservations(check, kitchen_updates)
    for check in checks:
        for row in check.items:
            if row.name in kitchen_updates:
                row.update(kitchen_updates[row.name])
    destination_before = _reservation_lines(destination.items)
    merge_rate_prec = currency_precision(destination)
    verified_priv_child_rates = {}
    merged_items = {line_keys[row.name]: row for row in destination.items} if split_consolidation else {}
    billing_keys = [line_keys[row.name] for row in destination.items] if split_balance else []
    for source in sources:
        for row in source.items:
            origin = row.get("custom_origin_invoice_item") or row.name
            key = line_keys.get(row.name)
            appended = merged_items.get(key)
            if appended is not None:
                appended.qty = float(Decimal(str(appended.qty)) + Decimal(str(row.qty)))
                appended.custom_kitchen_anchor = (
                    appended.get("custom_kitchen_anchor") or row.get("custom_kitchen_anchor")
                )
            else:
                values = row.as_dict(no_default_fields=True, no_child_table_fields=True, no_private_properties=True)
                if split_consolidation:
                    values["custom_origin_invoice_item"] = origin
                    values["reservation_line_key"] = row.get("reservation_line_key") or row.name
                appended = destination.append("items", values)
                if appended is None and destination.items:
                    appended = destination.items[-1]
                if split_consolidation and appended is not None:
                    merged_items[key] = appended
                    billing_keys.append(key)
            if (
                appended is not None
                and cint(row.get("custom_is_modifier")) == 1
                and (
                    cint(row.get("custom_price_overridden")) == 1
                    or cint(row.get("custom_is_complimentary")) == 1
                )
            ):
                verified_rate = flt(row.get("rate"), merge_rate_prec)
                verified_priv_child_rates[("row_id", id(appended))] = verified_rate
                if appended.get("reservation_line_key"):
                    verified_priv_child_rates[("rkey", str(appended.get("reservation_line_key")))] = verified_rate

    if split_consolidation:
        _reassign_merged_kitchen_anchors(destination, sources, dependants)
        if split_balance:
            from ury.ury_pos.split_balances import set_split_balance

            set_split_balance(destination, split_balance, billing_keys)
        ensure_share_quantity_precision(destination)

    if discount_plan is not None:
        if discount_plan.get("has_header_discount"):
            destination.apply_discount_on = discount_plan["apply_discount_on"]
            destination.additional_discount_percentage = discount_plan["additional_discount_percentage"]
            if discount_plan["discount_amount"] is not None:
                destination.discount_amount = discount_plan["discount_amount"]
            if discount_plan.get("clear_coupon") and hasattr(destination, "coupon_code"):
                destination.coupon_code = None
        if (
            discount_plan.get("reason")
            or discount_plan.get("note")
            or discount_plan.get("approver")
            or discount_plan.get("line_reasons")
        ):
            _write_discount_audit_fields(
                destination,
                reason=discount_plan.get("reason"),
                reason_description=discount_plan.get("reason_description"),
                note=discount_plan.get("note"),
                coupon=destination.get("coupon_code") if hasattr(destination, "coupon_code") else None,
                approver=discount_plan.get("approver"),
                approved_percent=discount_plan.get("approved_percent") or 0.0,
                line_reasons=discount_plan.get("line_reasons") or {},
            )
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

    from ury.ury.api.kitchen_ticket_history import owned_ticket_filters, record_ticket_transfer

    for source in sources:
        transferred = [name for scope in owned_ticket_filters(source)
                       for name in frappe.get_all("URY KOT", filters=scope, pluck="name")]
        if transferred:
            record_ticket_transfer(destination, transferred, True)
            record_ticket_transfer(source, transferred, destination.name)
            frappe.db.set_value("POS Invoice", source.name, "ury_applied_rounds", source.ury_applied_rounds,
                                update_modified=False)

    destination.invoice_printed = 0
    destination.custom_is_complimentary = cint(merged_comp)
    with with_currency_precision(destination):
        if destination.get("pos_profile"):
            _sync_service_charge(destination)
        destination.calculate_taxes_and_totals()
        destination.set_paid_amount()
        destination.set_outstanding_amount()
    prev_tender_flag = getattr(frappe.flags, "ury_server_tender_received", False)
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    prev_merge_flag = getattr(frappe.flags, "in_bill_merge_sync", False)
    frappe.flags.ury_server_tender_received = True
    frappe.flags.ury_server_discount_write = True
    frappe.flags.in_bill_merge_sync = True
    orig_session_user = frappe.session.user
    swap_for_inherited_approval = bool(
        discount_plan
        and discount_plan.get("approver")
        and discount_plan.get("approver") == orig_session_user
        and destination.get("owner")
        and destination.get("owner") != discount_plan.get("approver")
    )
    if swap_for_inherited_approval:
        frappe.session.user = destination.get("owner")
    prev_verified_child_rates = getattr(
        getattr(destination, "flags", None), "ury_verified_privileged_child_rates", None
    )
    if verified_priv_child_rates and getattr(destination, "flags", None) is not None:
        destination.flags.ury_verified_privileged_child_rates = verified_priv_child_rates
    try:
        with with_currency_precision(destination):
            if swap_for_inherited_approval:
                destination.save(ignore_permissions=True)
            else:
                destination.save()
    finally:
        if getattr(destination, "flags", None) is not None:
            destination.flags.ury_verified_privileged_child_rates = prev_verified_child_rates
        if swap_for_inherited_approval:
            frappe.session.user = orig_session_user
        frappe.flags.ury_server_tender_received = prev_tender_flag
        frappe.flags.ury_server_discount_write = prev_disc_flag
        frappe.flags.in_bill_merge_sync = prev_merge_flag

    for dependant in dependants or ():
        frappe.db.set_value("POS Invoice Item", dependant.name, "custom_kitchen_anchor",
                            destination.name, update_modified=False)
    for source in sources:
        _discard_added_check(source, destination, company, split_consolidation, reservations_enabled)

    if reservations_enabled and not split_consolidation:
        reconcile_order_reservations(
            order_ref=destination.name,
            previous_items=destination_before,
            accepted_items=_reservation_lines(destination.items),
            branch=destination.branch,
            company=company,
            actor=frappe.session.user,
        )

    from ury.ury.api.table_lifecycle import on_check_closed

    for source in sources:
        if source.restaurant_table and source.restaurant_table != destination.restaurant_table:
            if not on_check_closed(source):
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
        from ury.ury.api.table_lifecycle import holds_until_settlement

        branch, menu_name, restaurant = get_restaurant_and_menu_name(table)
        filters = {"docstatus": 0}
        if invoiceNo:
            filters["name"] = invoiceNo
        elif is_payment == "Printed":
            filters["invoice_printed"] = 1
        elif is_payment != "Payments" and not holds_until_settlement(branch):
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

        if (not invoice_name or not invoice.get("taxes_and_charges")) and cint(invoice.get("custom_tax_exempt")) != 1:
            invoice.taxes_and_charges = frappe.db.get_value(
                "URY Restaurant", restaurant, "default_tax_template"
            )

        if not (invoice_name and invoice.get("custom_serving_period") and invoice.get("selling_price_list")):
            dp = _dayparts_mod()

            if dp.get_serving_periods(restaurant):
                resolved = dp.resolve_menu_and_price_list(
                    branch,
                    restaurant=restaurant,
                    table=table,
                    order_type=invoice.order_type,
                )
                invoice.selling_price_list = resolved.get("price_list") or frappe.db.get_value(
                    "Price List", dict(restaurant_menu=menu_name, enabled=1)
                )
            else:
                invoice.selling_price_list = frappe.db.get_value(
                    "Price List", dict(restaurant_menu=menu_name, enabled=1)
                )

        if not getattr(invoice, "customer", None) or invoice.customer == "Guest":
            from ury.ury.api.table_reservation import (
                get_completed_reservation_for_table,
                reservations_enabled,
            )

            completed_res = get_completed_reservation_for_table(table, branch) if reservations_enabled(branch) else None
            if completed_res:
                if completed_res.get("customer"):
                    invoice.customer = completed_res.get("customer")
                elif completed_res.get("customer_name"):
                    invoice.custom_guest_label = completed_res.get("customer_name")[:GUEST_LABEL_MAX]
                if completed_res.get("customer") and completed_res.get("customer_name"):
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

        if (not invoice_name or not invoice.get("taxes_and_charges")) and cint(invoice.get("custom_tax_exempt")) != 1:
            if (order_type == "Aggregators" and frappe.db.get_value("Branch", branch, "custom_no_taxes") == 0) or order_type != "Aggregators":
                invoice.taxes_and_charges = frappe.db.get_value("URY Restaurant", restaurant, "default_tax_template")

        if not (invoice_name and invoice.get("custom_serving_period") and invoice.get("selling_price_list")):
            dp = _dayparts_mod()

            if restaurant and order_type != "Aggregators" and dp.get_serving_periods(restaurant):
                resolved = dp.resolve_menu_and_price_list(
                    branch,
                    restaurant=restaurant,
                    order_type=order_type,
                )
                invoice.selling_price_list = resolved.get("price_list") or frappe.db.get_value(
                    "Price List", dict(restaurant_menu=menu, enabled=1)
                )
            else:
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
    """The pricing date of an invoice: its posting date or verified offline capture date.

    ERPNext resets posting_date to today on validate unless set_posting_time
    is on, so an open till invoice prices on today; offline replays price on
    their verified capture date without needing set_posting_time=1.
    """
    if cint(getattr(invoice, "set_posting_time", 0)) and getattr(invoice, "posting_date", None):
        return frappe.utils.getdate(invoice.posting_date)
    if getattr(invoice, "ury_offline_captured_at", None):
        return frappe.utils.getdate(invoice.ury_offline_captured_at)
    return frappe.utils.getdate()


def price_items_for_invoice(
    items,
    price_list,
    pos_profile,
    branch,
    menu,
    posting_date=None,
    customer=None,
    price_overrides=None,
):
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
    price_overrides = price_overrides or {}

    for d in items:
        item_code = d.get("item")
        item_name = d.get("item_name")
        line_key = d.get("reservation_line_key")

        if d.get("custom_course"):
            from ury.ury.api.ury_kot_generate import resolve_line_course

            course = resolve_line_course(d, item_code, menu)
        else:
            course = frappe.db.get_value("URY Menu Item", {"item": item_code, "parent": menu}, "course")
        seat = d.get("custom_seat_no", 0)
        if cint(seat) < 0 or flt(seat) != cint(seat):
            frappe.throw(_("Seat number must be a non-negative whole number."))

        override = price_overrides.get(line_key) if line_key else None
        if override and override.get("is_validated_modifier"):
            resolved_rate = override["rate"]
        else:
            item_price = price_valid_on(
                item_code, price_list, posting_date, release_field, released, customer=customer,
            )
            if not item_price:
                try:
                    raw_zero = frappe.db.get_value("Item", item_code, "custom_allow_zero_price")
                except KeyError:
                    raw_zero = 0
                zero_ok = isinstance(raw_zero, (int, float, str, bool)) and cint(raw_zero) == 1
                if zero_ok:
                    item_price = frappe._dict(price_list_rate=0.0)
                else:
                    frappe.throw(
                        coded_message(
                            "PRICE-NOT-VALID",
                            _("No released item price valid on {2} found for Item: {0} in Price List: {1}. Please check the price list settings.").format(item_code, price_list, posting_date),
                        ),
                        frappe.ValidationError,
                    )
            resolved_rate = item_price.price_list_rate

        if not item_name or item_name == item_code:
            item_name = frappe.db.get_value("Item", item_code, "item_name") or item_name or item_code
        department_warehouse = _department_warehouse_for_item(item_code, branch, company)
        modifier_fields = {
            "custom_parent_line_key": d.get("custom_parent_line_key") or None,
            "custom_line_role": d.get("custom_line_role") or "Standard",
            "custom_is_modifier": cint(d.get("custom_is_modifier")),
            "custom_modifier_prefix": d.get("custom_modifier_prefix") or "",
            "custom_modifier_group": d.get("custom_modifier_group") or None,
            "custom_modifier_option": d.get("custom_modifier_option") or None,
            "custom_modifiers_json": d.get("custom_modifiers_json") or "",
            "custom_bundle_overrides_json": d.get("custom_bundle_overrides_json") or "",
            "custom_inherit_parent_routing": cint(d.get("custom_inherit_parent_routing", 1)),
            "custom_hide_unpriced_on_receipt": cint(d.get("custom_hide_unpriced_on_receipt")),
        }
        priced_items.append(
            dict(
                item_code=item_code,
                item_name=item_name,
                qty=d.get("qty"),
                reservation_line_key=line_key,
                **({"custom_seat_no": cint(seat)} if seat else {}),
                **({"custom_course": course} if course else {}),
                **({"warehouse": department_warehouse} if department_warehouse else {}),
                comment=d.get("comment"),
                rate=resolved_rate,
                price_list_rate=resolved_rate,
                base_price_list_rate=resolved_rate,
                cost_center=frappe.db.get_value(
                    "POS Profile", pos_profile, "cost_center"
                ),
                **modifier_fields,
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


def _resolve_menu_for_sync(branch, table=None, room=None, order_type=None, local_dt=None):
    """Authoritative menu for sync validation (room-wise / serving-period / order-type-wise)."""
    restaurant = frappe.db.get_value("URY Restaurant", {"branch": branch}, "name")
    if not restaurant:
        return None

    dp = _dayparts_mod()

    if dp.get_serving_periods(restaurant):
        resolved = dp.resolve_menu_and_price_list(
            branch,
            restaurant=restaurant,
            table=table,
            room=room,
            order_type=order_type,
            local_dt=local_dt,
        )
        if resolved.get("menu"):
            return resolved["menu"]

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
    local_dt=None,
    active_period=None,
    validated_modifier_keys=None,
):
    """Reject newly added disabled / off-menu / out-of-daypart lines; keep historic + aggregators.

    Aggregator flows price from Aggregator Settings (not restaurant menu).
    Historic item codes already on the invoice may stay even if later disabled,
    removed from the menu, or outside their daypart window so captains can still
    update notes/qty of sent lines.
    Server-validated modifier / combo upcharge lines are checked for Item.disabled
    but are not required to appear as standalone dishes on URY Menu.
    """
    historic_codes = {prev["item_code"] for prev in (past_item or []) if prev.get("item_code")}
    validated_modifier_keys = set(validated_modifier_keys or ())
    new_codes = []
    modifier_codes = []
    for row in items or []:
        code = row.get("item")
        if not code or code in historic_codes:
            continue
        line_key = row.get("reservation_line_key")
        if line_key and line_key in validated_modifier_keys:
            if code not in modifier_codes:
                modifier_codes.append(code)
            continue
        if code not in new_codes:
            new_codes.append(code)

    for code in modifier_codes:
        if frappe.db.get_value("Item", code, "disabled"):
            frappe.throw(
                _("Item {0} is disabled and cannot be added to the order.").format(code)
            )

    if not new_codes:
        return

    if order_type == "Aggregators":
        for code in new_codes:
            if frappe.db.get_value("Item", code, "disabled"):
                frappe.throw(
                    _("Item {0} is disabled and cannot be added to the order.").format(code)
                )
        return

    menu = _resolve_menu_for_sync(
        branch, table=table, room=room, order_type=order_type, local_dt=local_dt
    )
    if not menu:
        frappe.throw(_("Please set an active menu for this restaurant."))

    menu_rows = frappe.get_all(
        "URY Menu Item",
        filters={"parent": menu, "item": ["in", new_codes], "disabled": 0},
        fields=["item", "custom_available_from", "custom_available_to", "custom_serving_period"],
    )
    rows_by_code = {row.item: row for row in menu_rows}

    dp_mod = _dayparts_mod()

    for code in new_codes:
        if code not in rows_by_code:
            frappe.throw(
                _("Item {0} is not available on the menu.").format(code)
            )
        if frappe.db.get_value("Item", code, "disabled"):
            frappe.throw(
                _("Item {0} is disabled and cannot be added to the order.").format(code)
            )
        dp = dp_mod.evaluate_item_daypart(
            rows_by_code[code], active_period=active_period, local_dt=local_dt, branch=branch
        )
        if dp.get("unavailable_daypart"):
            frappe.throw(
                _("Item {0} is outside its serving window ({1}).").format(
                    code, dp.get("daypart_label") or _("unavailable now")
                )
            )


def _validate_dine_in_pax(no_of_pax, order_type, profile=None):
    from ury.ury_pos.covers import normalize_covers

    return normalize_covers(no_of_pax, order_type, profile, _frappe=frappe)


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

    from ury.ury.api.table_lifecycle import holds_until_settlement

    filters = {"docstatus": 0}
    if not holds_until_settlement(table_doc.branch):
        filters["invoice_printed"] = 0
    invoices = frappe.get_all(
        "POS Invoice",
        filters=filters,
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

    # An active privilege policy can authorize this move through a manager PIN.
    can_transfer_table = can_modify and (has_elevated_access or bool(
        invoice and _pa_is_company_policy_active(invoice.get("company"))
        and frappe.has_permission("POS Invoice", "write", doc=invoice)
    ))

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
    """Allow replacing the customer on an open order, except for Aggregators or tax-exempt checks.

    A walk-in or provisional customer is routinely replaced with the real one
    before payment, and the server accepts that. An Aggregators order's customer
    is the aggregator itself and drives its price list, so it must not change.
    A tax-exempt check's customer is bound to the exemption certificate and must
    not change until the exemption is cleared and re-authorised.
    """
    if not getattr(invoice, "name", None):
        return

    current = getattr(invoice, "customer", None)
    if current and current != customer and getattr(invoice, "order_type", None) == "Aggregators":
        frappe.throw(_("The customer of an Aggregators order cannot be changed."))
    raw_exempt = invoice.get("custom_tax_exempt") if callable(getattr(invoice, "get", None)) else getattr(invoice, "custom_tax_exempt", 0)
    raw_exempt_cust = invoice.get("custom_tax_exempt_customer") if callable(getattr(invoice, "get", None)) else getattr(invoice, "custom_tax_exempt_customer", None)
    exempt_cust = raw_exempt_cust if isinstance(raw_exempt_cust, str) and raw_exempt_cust.strip() else current
    if (
        isinstance(raw_exempt, (int, float, str, bool))
        and cint(raw_exempt) == 1
        and exempt_cust
        and customer != exempt_cust
    ):
        frappe.throw(
            coded_message(
                "TAX-EXEMPT-CUSTOMER-CHANGED",
                _("Cannot change customer on a tax-exempt check. Clear the tax exemption and re-authorise for the new customer."),
            ),
            frappe.ValidationError,
        )


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
                "custom_course": item.get("custom_course"),
                "custom_seat_no": cint(item.get("custom_seat_no")),
                **({"custom_kitchen_anchor": item.get("custom_kitchen_anchor")} if item.get("custom_kitchen_anchor") else {}),
                "custom_parent_line_key": item.get("custom_parent_line_key") or None,
                "custom_line_role": item.get("custom_line_role") or "Standard",
                "custom_is_modifier": cint(item.get("custom_is_modifier")),
                "custom_modifier_prefix": item.get("custom_modifier_prefix") or "",
                "custom_modifiers_json": item.get("custom_modifiers_json") or "",
                "custom_bundle_overrides_json": item.get("custom_bundle_overrides_json") or "",
                "custom_inherit_parent_routing": cint(item.get("custom_inherit_parent_routing", 1)),
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


def _record_round(invoice, round_id, digest=None):
    from ury.ury.api.kitchen_ticket_history import round_ledger, write_round_ledger

    invoice.ury_last_round_id = round_id
    rounds, bindings = round_ledger(invoice)
    rounds.setdefault(str(round_id), digest)
    write_round_ledger(invoice, rounds, bindings)


def _is_applied_round(invoice, round_id, digest=None):
    """Any round already applied to this order, not only the latest; a changed payload is refused."""
    if round_id is None:
        return False
    round_id = str(round_id)
    from ury.ury.api.kitchen_ticket_history import round_ledger

    rounds, _ = round_ledger(invoice)
    if round_id in rounds:
        if digest and rounds[round_id] and rounds[round_id] != digest:
            _offline_refusal("URY_ROUND_PAYLOAD_MISMATCH", _("This round was already saved with different items."))
        return True
    # Orders saved before the applied-round ledger only know their latest round.
    return str(invoice.get("ury_last_round_id") or "") == round_id


def _refuse_client_order_rebind(invoice, client_order_id):
    if client_order_id and invoice.get("ury_client_order_id") not in (None, "", client_order_id):
        _offline_refusal("URY_CLIENT_ORDER_ID_REBIND", _("Client order ID cannot be changed for an existing order."))


def _offline_capture_time(captured_at, pos_profile, billing_user=True):
    """Capture time in the site timezone, inside the open POS shift and not in the future.

    Billing/cashier users remain strictly bound to their own open POS Opening Entry.
    Floor/waiter roles (who cannot open a till themselves) authorize against an active
    open POS Opening Entry on the same POS Profile.
    """
    message = _("Capture must be inside your open POS shift and not in the future.")
    try:
        captured = get_datetime(captured_at)
        if captured and captured.tzinfo:
            captured = captured.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
    except (ValueError, TypeError, AttributeError, OverflowError):
        captured = None
    if not captured:
        _offline_refusal("URY_OFFLINE_OUTSIDE_SHIFT", message)
    opening_filters = {"pos_profile": pos_profile, "docstatus": 1, "status": "Open"}
    if billing_user:
        opening_filters["user"] = frappe.session.user
    opening = frappe.db.get_value(
        "POS Opening Entry",
        opening_filters,
        "period_start_date",
        order_by="period_start_date desc",
    )
    if not opening or not get_datetime(opening) <= captured <= now_datetime():
        _offline_refusal("URY_OFFLINE_OUTSIDE_SHIFT", message)
    return captured


def _kitchen_snapshot(items):
    """Keep stable cart identities; legacy unkeyed rounds compare dish/note totals."""
    items = list(items)
    parents = {
        item.get("reservation_line_key") or item.get("name"): item
        for item in items if not item.get("custom_parent_line_key")
    }
    rows = []
    legacy = {}
    for item in items:
        if cint(item.get("ury_held")):
            continue
        code = item.get("item") or item.get("item_code")
        note = item.get("comment") or item.get("comments") or ""
        parent_key = item.get("custom_parent_line_key")
        context = parents.get(parent_key, item) if parent_key else item
        course = context.get("custom_course")
        seat = cint(context.get("custom_seat_no") or context.get("seat_no"))
        key = item.get("reservation_line_key")
        if not key and (code, note, course, seat) in legacy:
            legacy[code, note, course, seat]["qty"] += flt(item.get("qty"))
            continue
        row = dict(
            item_code=code,
            item_name=item.get("item_name"),
            qty=flt(item.get("qty")),
            comments=note,
            custom_course=course,
            custom_seat_no=seat,
            custom_parent_line_key=item.get("custom_parent_line_key") or None,
            custom_line_role=item.get("custom_line_role") or "Standard",
            custom_is_modifier=cint(item.get("custom_is_modifier")),
            custom_modifier_prefix=item.get("custom_modifier_prefix") or "",
            custom_modifiers_json=item.get("custom_modifiers_json") or "",
            custom_bundle_overrides_json=item.get("custom_bundle_overrides_json") or "",
            custom_inherit_parent_routing=cint(item.get("custom_inherit_parent_routing", 1)),
        )
        if key:
            row["reservation_line_key"] = key
        else:
            # Keep the saved row for course validation without changing its legacy kitchen identity.
            row["raw_line_key"] = item.get("name")
            legacy[code, note, course, seat] = row
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


def _staff_held_order(invoice, *, scheduling=False):
    invoice = frappe.get_doc("POS Invoice", invoice)
    if scheduling:
        # A locking read through Frappe keeps the schedule transaction on
        # the same invoice row as every fire path.
        frappe.db.get_value("POS Invoice", invoice.name, "name", for_update=True)
        invoice.flags.for_update = True
        invoice.reload()
    else:
        _lock_hold_order(invoice)
    if not frappe.has_permission("POS Invoice", "write", doc=invoice):
        frappe.throw(_("Not permitted to modify this order"), frappe.PermissionError)
    branch = getBranch()
    if not branch or invoice.branch != branch:
        frappe.throw(_("Not permitted outside your active branch"), frappe.PermissionError)
    _enforce_order_access(invoice, require_modify=True)
    profile = frappe.get_doc("POS Profile", invoice.pos_profile)
    if not cint(getattr(profile, "ury_enable_hold_fire", 0)):
        frappe.throw(coded_message("HOLD-DISABLED", _("Hold and fire is disabled for this outlet.")))
    if profile.get("branch") != invoice.branch:
        frappe.throw(_("Not permitted outside your active branch"), frappe.PermissionError)
    if invoice.docstatus != 0:
        frappe.throw(_("This order is no longer open."))
    if invoice.order_type == "Dine In" and _has_role(
        frappe.get_roles(), profile.role_restricted_for_table_order
    ):
        frappe.throw(_("You are not permitted to take Dine In table orders."), frappe.PermissionError)
    return invoice, profile


def _selected_held_rows(invoice, line, course):
    if bool(line) == bool(course):
        frappe.throw(_("Select exactly one line or course to fire."))
    selected = [row for row in invoice.items if row.name == line] if line else [
        row for row in invoice.items if row.get("custom_course") == course
    ]
    if not selected:
        frappe.throw(_("The selected line or course is not on this order."))
    return _held_release_group(invoice, selected)


def _ambiguous_held_lines():
    frappe.throw(coded_message("HOLD-AMBIGUOUS-LINE", _("Held kitchen lines have an ambiguous reservation key.")))


def _held_release_group(invoice, selected):
    """A parent and its persisted kitchen modifiers release as one group."""
    selected_keys = {row.get("reservation_line_key") or row.name for row in selected}
    parents = {row.get("custom_parent_line_key") for row in selected if row.get("custom_parent_line_key")}
    group_keys = selected_keys | parents
    keys = Counter(row.get("reservation_line_key") or row.name for row in invoice.items)
    group = [row for row in invoice.items
             if (row.get("reservation_line_key") or row.name) in group_keys
             or row.get("custom_parent_line_key") in group_keys]
    if any(keys[row.get("reservation_line_key") or row.name] > 1 for row in group):
        _ambiguous_held_lines()
    return [row for row in group if cint(row.get("ury_held")) and flt(row.get("qty")) > 0]


def _check_held_version(invoice, last_modified_time):
    if last_modified_time and str(invoice.modified) != str(last_modified_time):
        frappe.throw(_("Order has been modified. Reload the order and try again."))


def _publish_held_updates(invoice, modified_before, rows):
    frappe.publish_realtime(
        "held_kitchen_items_updated",
        {
            "invoice": invoice.name,
            "previous_modified": str(modified_before),
            "modified": str(invoice.modified),
            "items": [
                {"name": row.name, "reservation_line_key": row.get("reservation_line_key"),
                 "ury_held": row.get("ury_held"), "fire_at": row.get("fire_at"),
                 "fire_confirmation_pending": cint(row.get("fire_confirmation_pending"))}
                for row in rows
            ],
        },
        room=f"doc:POS Invoice/{invoice.name}",
        after_commit=True,
    )


def _release_held_rows(invoice, held, *, kitchen_authority=False):
    if not held:
        return
    modified_before = invoice.modified
    for row in held:
        row.ury_held = 0
        row.fire_at = None
        row.fire_confirmation_pending = 0
    invoice.save(ignore_permissions=kitchen_authority)
    # The locked selection is already the positive kitchen delta. Re-diffing
    # the entire cart could move a legacy same-dish quantity between seats.
    # A failure rolls back the release flags and ticket writes together.
    kot_execute(
        invoice.name, invoice.customer, invoice.restaurant_table, _kitchen_snapshot(held),
        [], invoice.custom_comments, ignore_permissions=kitchen_authority, require_routing=True,
    )
    _publish_held_updates(invoice, modified_before, held)


@frappe.whitelist(methods=["POST"])
def fire_items(invoice, line=None, course=None, last_modified_time=None):
    """Fire persisted held rows, with idempotent retries after release."""
    invoice, _profile = _staff_held_order(invoice)
    held = _selected_held_rows(invoice, line, course)
    previous_modified = str(invoice.modified)
    if held:
        _check_held_version(invoice, last_modified_time)
        _release_held_rows(invoice, held)
    response = invoice.as_dict()
    response["held_update_previous_modified"] = previous_modified
    return response


@frappe.whitelist(methods=["POST"])
def schedule_held_items(invoice, line=None, course=None, delay_minutes=None, fire_at=None,
                        last_modified_time=None):
    invoice, profile = _staff_held_order(invoice, scheduling=True)
    held = _selected_held_rows(invoice, line, course)
    # Legacy callers can set an initial timer. Once one exists, only a
    # current check version authorizes replacing it. Retries without that
    # version return the existing deadline, even for a relative delay.
    if not held or (not last_modified_time and any(row.get("fire_at") for row in held)):
        response = invoice.as_dict()
        response["held_update_previous_modified"] = str(invoice.modified)
        return response
    _check_held_version(invoice, last_modified_time)
    if delay_minutes is not None and fire_at is not None:
        frappe.throw(_("Specify either a delay or a fire time."))
    now = now_datetime()
    try:
        if fire_at is not None:
            if not isinstance(fire_at, str) or not fire_at.strip():
                raise ValueError
            deadline = datetime.fromisoformat(fire_at)
            if deadline.tzinfo:
                deadline = deadline.astimezone(ZoneInfo(get_system_timezone())).replace(tzinfo=None)
            if deadline < now.replace(microsecond=0):
                raise ValueError
        else:
            delay = int(str(delay_minutes)) if delay_minutes is not None else (
                cint(profile.get("default_fire_delay_minutes")) or 10
            )
            if delay < 1:
                raise ValueError
            deadline = now + timedelta(minutes=delay)
    except (TypeError, ValueError, OverflowError):
        frappe.throw(_("Choose a valid fire time or a positive delay in whole minutes."))
    modified_before = invoice.modified
    for row in held:
        row.fire_at = deadline
        row.fire_confirmation_pending = 0
    invoice.save()
    _publish_held_updates(invoice, modified_before, held)
    response = invoice.as_dict()
    response["held_update_previous_modified"] = str(modified_before)
    return response


def fire_due_held_orders():
    """Scheduler entry point. Process each check in an independent transaction."""
    cutoff = now_datetime()
    due = frappe.db.sql(
        """
        SELECT item.parent
        FROM `tabPOS Invoice Item` item
        JOIN `tabPOS Invoice` invoice ON invoice.name = item.parent
        WHERE item.ury_held = 1 AND item.qty > 0 AND item.fire_at <= %s
          AND invoice.docstatus = 0
        GROUP BY item.parent
        ORDER BY MIN(item.fire_at), item.parent
        """,
        (cutoff,),
    )
    # Close the read transaction before acquiring locks for individual checks.
    frappe.db.rollback()
    for (name,) in due:
        try:
            invoice = frappe.get_doc("POS Invoice", name)
            _lock_hold_order(invoice)
            profile = frappe.get_doc("POS Profile", invoice.pos_profile)
            if invoice.docstatus == 0 and profile.get("branch") == invoice.branch and cint(
                profile.get("ury_enable_hold_fire")
            ):
                held = [
                    row for row in invoice.items if cint(row.get("ury_held")) and flt(row.get("qty")) > 0
                    and row.get("fire_at") and get_datetime(row.get("fire_at")) <= cutoff
                ]
                # Scheduling writes the deadline to each member of a group.
                # A shared cart key on older checks cannot authorize firing
                # an unscheduled modifier or a sibling with a later deadline.
                _release_held_rows(invoice, held, kitchen_authority=True)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="Timed kitchen release failed", message=frappe.get_traceback())


def _fire_next_held_course(invoice, bumped_tickets):
    """Advance at most one course after a complete earlier course is bumped.

    KDS callers lock the invoice before changing any ticket status.
    """
    profile = frappe.get_doc("POS Profile", invoice.pos_profile)
    if (invoice.docstatus != 0 or profile.get("branch") != invoice.branch
            or not cint(profile.get("ury_enable_hold_fire"))
            or not cint(profile.get("auto_fire_next_course_on_bump"))):
        return
    held = [
        row for row in invoice.items
        if cint(row.get("ury_held")) and flt(row.get("qty")) > 0
        and not row.get("custom_parent_line_key") and row.get("custom_course")
    ]
    if not held or not bumped_tickets:
        return
    from ury.ury.api.ury_kot_display import _get_kot_cancellation_state

    from ury.ury.api.kitchen_ticket_history import owned_ticket_filters, ticket_ownership

    ticket_condition, scope_value = "kot.invoice = %s", invoice.name
    if ticket_ownership(invoice):
        owned = [row[0] for scope in owned_ticket_filters(invoice)
                 for row in frappe.db.get_values("URY KOT", scope, "name", for_update=True)]
        if not owned:
            return
        ticket_condition, scope_value = "kot.name IN %s", tuple(owned)
    tickets = frappe.db.sql(
        f"""
        SELECT kot.name, kot.order_status, item.course,
               (kot.docstatus = 1 AND item.quantity > IFNULL(item.cancelled_qty, 0)) AS has_remaining_qty
        FROM `tabURY KOT` kot
        JOIN `tabURY KOT Items` item ON item.parent = kot.name
        WHERE {ticket_condition} AND kot.docstatus IN (1, 2)
          AND IFNULL(kot.type, '') NOT IN ('Cancelled', 'Partially cancelled')
        FOR UPDATE
        """,
        (scope_value,),
        as_dict=True,
    )
    course_items = [
        row for row in invoice.items
        if flt(row.get("qty")) > 0 and not row.get("custom_parent_line_key") and row.get("custom_course")
    ]
    ticket_courses = {row["course"] for row in tickets if row["course"]}
    courses = ticket_courses | {row.get("custom_course") for row in course_items}
    priorities = {
        row["name"]: cint(row["custom_serving_priority"]) for row in frappe.get_all(
            "URY Menu Course", filters={"name": ["in", sorted(courses)]},
            fields=["name", "custom_serving_priority"],
        )
    }
    ranked = {course: priority for course, priority in priorities.items() if priority > 0}
    held_courses = {row.get("custom_course") for row in held} & ranked.keys()
    if not held_courses:
        return
    course = min(held_courses, key=lambda course: (ranked[course], course))
    earlier = [row for row in tickets if 0 < ranked.get(row["course"], 0) < ranked[course]]
    if not any(row["name"] in bumped_tickets for row in earlier):
        return
    cancelled, pending = _get_kot_cancellation_state(invoice.branch, invoice=invoice)
    if any(
        row["name"] in pending
        or (row["order_status"] != "Served" and row["name"] not in cancelled and row.get("has_remaining_qty", True))
        for row in earlier
    ):
        return

    # A sent earlier course with no ticket history cannot be considered
    # served. Direct retail can legitimately have no kitchen ticket.
    for row in course_items:
        name = row.get("custom_course")
        if name not in ticket_courses and 0 < ranked.get(name, 0) < ranked[course]:
            context = resolve_production_context(row.get("item_code"), invoice.branch, company=invoice.company)
            if not context or context.production_policy != DIRECT_RETAIL:
                return
    group = _selected_held_rows(invoice, None, course)
    # An explicit schedule takes precedence over the course bump.
    if any(row.get("fire_at") for row in group):
        return
    if cint(profile.get("require_course_fire_confirmation")):
        if any(not cint(row.get("fire_confirmation_pending")) for row in group):
            modified_before = invoice.modified
            for row in group:
                row.fire_confirmation_pending = 1
            invoice.save(ignore_permissions=True)
            _publish_held_updates(invoice, modified_before, group)
    else:
        _release_held_rows(invoice, group, kitchen_authority=True)


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
    approval_token=None,
    void_reason=None,
    void_note=None,
    is_waste=0,
    pickup_approval_token=None,
    guest_count_approval_token=None,
    order_type_approval_token=None,
    privilege_reason=None,
    privilege_reason_note=None,
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
    # G36: optional clock-in gate when configured on the POS Profile and HR attendance is active.
    if cint(posprofile.get("custom_require_clock_in_to_order")) == 1:
        from ury.ury_pos import timeclock

        timeclock.enforce_clock_in_for_profile(posprofile, user=frappe.session.user)
    client_invoice = _client_order_invoice(client_order_id, invoice, table)

    billing_user = any(
        role.role in user_role for role in posprofile.role_allowed_for_billing
    )

    # Authorize the requested table before reading any seating or reservation policy.
    elevated_user = _has_role(user_role, posprofile.transfer_role_permissions)
    if table:
        table_branch, table_room = frappe.db.get_value(
            "URY Table", table, ["branch", "restaurant_room"]
        ) or (None, None)

        if not table_branch:
            frappe.throw(_("Table not found."), frappe.PermissionError)

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

    is_offline_replay = (captured_at is not None) or (cint(offline_printed) == 1)
    collision_plan = None
    collision_split_create = False
    collision_merge = False
    pre_merge_grand_total = 0.0
    effective_round_id = round_id

    # G46: Check for offline table collision BEFORE get_order_invoice() enforces single-waiter
    # ownership on an existing check opened by another waiter on the same table.
    invoice_doc = None
    if is_offline_replay and table and client_order_id and not client_invoice and not invoice:
        existing_meta = frappe.db.get_value(
            "POS Invoice",
            {"restaurant_table": table, "docstatus": 0, "invoice_printed": 0},
            [
                "name",
                "pos_profile",
                "restaurant",
                "branch",
                "selling_price_list",
                "taxes_and_charges",
                "custom_split_group",
                "ury_client_order_id",
            ],
            as_dict=True,
        )
        if existing_meta and (existing_meta.get("ury_client_order_id") or "") != client_order_id:
            if existing_meta.get("pos_profile") and existing_meta.get("pos_profile") != pos_profile:
                frappe.throw(
                    coded_message("PROFILE-MISMATCH", _("This order belongs to a different POS Profile.")),
                    frappe.PermissionError,
                )
            collision_plan = build_offline_collision_plan(
                existing_invoice=existing_meta,
                table=table,
                client_order_id=client_order_id,
                round_id=round_id,
                policy=posprofile.get("custom_offline_collision_policy"),
            )
            if collision_plan["action"] == "merge_rounds":
                invoice_doc = frappe.get_doc("POS Invoice", existing_meta["name"])
                pre_merge_grand_total = flt(invoice_doc.grand_total)
                effective_round_id = collision_plan["collision_round_id"]
                if _is_applied_round(invoice_doc, effective_round_id, round_digest):
                    return invoice_doc.as_dict()
                collision_merge = True
                append = 1
                invoice_doc.custom_offline_conflict = 1
            else:
                invoice_doc = get_order_invoice(None, None, order_type)
                invoice_doc.restaurant_table = table
                invoice_doc.restaurant = existing_meta.get("restaurant") or posprofile.restaurant
                invoice_doc.branch = existing_meta.get("branch") or posprofile.branch
                if existing_meta.get("selling_price_list"):
                    invoice_doc.selling_price_list = existing_meta.get("selling_price_list")
                if existing_meta.get("taxes_and_charges"):
                    invoice_doc.taxes_and_charges = existing_meta.get("taxes_and_charges")
                invoice_doc.custom_split_from = collision_plan["custom_split_from"]
                invoice_doc.custom_split_group = collision_plan["custom_split_group"]
                invoice_doc.custom_offline_conflict = 1
                if not guest_label:
                    guest_label = collision_plan["custom_guest_label"]
                collision_split_create = True

    if table and not invoice and not client_invoice and not invoice_doc:
        from ury.ury.api.table_lifecycle import assert_table_seatable

        assert_table_seatable(table, table_branch)
    invoice = invoice_doc or client_invoice or get_order_invoice(table, invoice, order_type)
    if invoice.name and invoice.pos_profile and invoice.pos_profile != pos_profile:
        frappe.throw(
            coded_message("PROFILE-MISMATCH", _("This order belongs to a different POS Profile.")),
            frappe.PermissionError,
        )
    hold_enabled = cint(getattr(posprofile, "ury_enable_hold_fire", 0)) == 1
    if hold_enabled:
        _lock_hold_order(invoice, preserve_resolved=True)

    if not collision_merge:
        _refuse_client_order_rebind(invoice, client_order_id)

    if client_invoice:
        # A retry of any round already saved returns the saved order: one invoice, one KOT.
        if _is_applied_round(invoice, round_id, round_digest):
            return invoice.as_dict()
        if invoice.docstatus != 0:
            _offline_refusal("URY_ORDER_NOT_OPEN", _("This order is no longer open."))

    if invoice.name and not collision_merge and not frappe.has_permission("POS Invoice", "write", doc=invoice):
        frappe.throw(_("Not permitted to modify this order"), frappe.PermissionError)

    # Reuse Phase 2's ownership formula (_has_role + is_mine/elevated/billing)
    # directly: block a Captain from modifying another Captain's table order,
    # and block modification of an already-billed/printed invoice unless the
    # acting user holds billing access. Only applies once an order actually
    # exists — a brand-new invoice has no owner or billed state to protect.
    is_super_user = frappe.session.user == "Administrator" or "System Manager" in user_role
    picked_up_with_pin = False
    if invoice.name and not collision_merge:
        is_mine = frappe.session.user == invoice.waiter
        can_view_order = is_super_user or is_mine or elevated_user or billing_user
        if not is_mine and not is_super_user:
            _is_valid_str = _pa_is_valid_str
            assert_not_offline_replay = _pa_assert_not_offline_replay
            authorize_or_perform = _pa_authorize_or_perform
            is_company_policy_active = _pa_is_company_policy_active

            _co = getattr(posprofile, "company", None) or getattr(invoice, "company", None)
            _co = _co if _is_valid_str(_co) else None
            _pickup_policy = is_company_policy_active(_co)
            _pickup_tok = pickup_approval_token
            if _pickup_policy or _pickup_tok or not can_view_order:
                assert_not_offline_replay(
                    append=append, captured_at=captured_at, offline_printed=offline_printed
                )
                auth_pickup = authorize_or_perform(
                    privilege="pickup_other_check",
                    pos_profile=pos_profile,
                    company=_co,
                    skip_profile_company_lookup=True,
                    reference_doctype="POS Invoice",
                    reference_name=invoice.name,
                    claim={
                        "action": "pickup_other_check",
                        "invoice": invoice.name,
                        "owner_waiter": invoice.waiter or "",
                    },
                    token=_pickup_tok,
                    reason=privilege_reason,
                    reason_note=privilege_reason_note,
                    require_reason=None if _pickup_policy else False,
                    allow_fallback_predicate=can_view_order,
                    deny_message=_("Not permitted to modify this order"),
                )
                picked_up_with_pin = bool(auth_pickup.get("via_pin"))
                can_view_order = True

        if not can_view_order:
            frappe.throw(_("Not permitted to modify this order"), frappe.PermissionError)

        if bool(invoice.invoice_printed) and not billing_user and not picked_up_with_pin:
            frappe.throw(
                _("This order has already been billed and cannot be modified."),
                frappe.PermissionError,
            )

    if table:
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
        if invoice.name and invoice.invoice_printed == 0 and not billing_user and not collision_merge:
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
    parent_holds = {
        row.get("reservation_line_key"): cint(row.get("ury_held"))
        for row in past_item if row.get("reservation_line_key") and not row.get("custom_parent_line_key")
    }
    for row in items:
        old_row = previous_rows.get(row.get("name"))
        key = row.get("reservation_line_key") or (old_row and old_row.get("reservation_line_key"))
        if key and not row.get("custom_parent_line_key"):
            parent_holds[key] = cint(row.get("ury_held"))
    for row in items:
        if row.get("custom_parent_line_key") in parent_holds:
            row["ury_held"] = parent_holds[row["custom_parent_line_key"]]
    has_held = any(cint(item.get("ury_held")) for item in items)
    if has_held and not hold_enabled:
        frappe.throw(coded_message("HOLD-DISABLED", _("Hold and fire is disabled for this outlet.")))
    if has_held and (captured_at is not None or cint(offline_printed)):
        frappe.throw(coded_message("HOLD-OFFLINE", _("Hold and fire requires an online connection.")))
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
    # stays unchanged. Unsent held items (`ury_held=1`) are always freely
    # editable/removable even when `remove_items=0` (Amendment 1 + R2 P1 #9),
    # and previously zeroed rows (qty == 0) are never treated as a second
    # reduction. Sent-item reductions ALWAYS require authorization (privilege
    # or manager PIN) AND a mandatory reason (never bypassed by remove_items
    # or offline replay).
    sent_void_reason_info = None
    sent_void_approver = None
    sent_waste_items = []
    if not append and past_item:
        def sent_quantities(rows):
            totals = defaultdict(float)
            for row in _kitchen_snapshot(rows):
                totals[(row["item_code"], row["comments"], row.get("reservation_line_key") or "")] += flt(row["qty"])
            return totals

        sent_before, sent_after = sent_quantities(past_item), sent_quantities(items)
        rate_by_line = {}
        rate_by_code = {}
        for inv_row in invoice.items or []:
            if cint(inv_row.get("ury_held")):
                continue
            r_val = flt(inv_row.get("rate") or 0)
            if inv_row.get("reservation_line_key"):
                rate_by_line[inv_row.get("reservation_line_key")] = r_val
            if inv_row.get("item_code") and inv_row.get("item_code") not in rate_by_code:
                rate_by_code[inv_row.get("item_code")] = r_val

        sent_reductions = []
        b2a_void_lines = []
        voided_amount = 0.0
        for key in sorted(sent_before.keys()):
            prev_qty = flt(sent_before[key])
            if prev_qty <= 0:
                continue
            after_qty = flt(sent_after.get(key, 0.0))
            if after_qty < prev_qty - 1e-9:
                item_code, _comment, line_key = key
                delta_pos = prev_qty - after_qty
                unit_rate = rate_by_line.get(line_key, rate_by_code.get(item_code, 0.0))
                voided_amount += unit_rate * delta_pos
                sent_reductions.append({
                    "item_code": item_code,
                    "line_key": line_key,
                    "from_qty": float(prev_qty),
                    "to_qty": float(after_qty),
                    "delta": float(after_qty - prev_qty),
                })
                b2a_void_lines.append({
                    "item_code": item_code,
                    "reservation_line_key": line_key,
                    "from_qty": prev_qty,
                    "to_qty": after_qty,
                    "delta": delta_pos,
                })
        voided_amount = round(voided_amount, 4)

        if sent_reductions:
            if captured_at is not None or cint(offline_printed) == 1:
                frappe.throw(
                    _("Sent items cannot be reduced or voided during offline replay. [{0}]").format(
                        _void_ops.CODE_VOID_OFFLINE_REFUSED
                    ),
                )

            # Reject reducing a partially paid check below already-received tender.
            received_paid = _void_ops.received_tender_total(invoice)
            if received_paid > 0:
                current_total = flt(invoice.get("rounded_total") or invoice.get("grand_total"))
                if current_total <= 0:
                    current_total = sum(flt(r.get("qty")) * flt(r.get("rate")) for r in invoice.items)
                if current_total - voided_amount < received_paid - 1e-6:
                    frappe.throw(
                        _("Cannot reduce items below payments already received on this check. [{0}]").format(
                            _void_ops.CODE_VOID_PARTIAL_PAYMENT
                        ),
                    )

            has_full_void = any(r["to_qty"] <= 0 for r in sent_reductions)
            privilege = "void_sent_item" if has_full_void else "reduce_sent_qty"
            fallback_privs = ("void_sent_item",) if privilege == "reduce_sent_qty" else ()
            sent_reductions.sort(key=lambda r: (r["item_code"], r["line_key"]))
            approval_amount = company_approval_amount(invoice, voided_amount, frappe)

            # Authorization check precedes reason validation when caller lacks
            # solo privilege and supplied no approval token (keeps unauthorized
            # cashiers from probing reason validation & matches B2a R2 fail-closed contract).
            if not approval_token and not _void_ops.can_perform_pos_privilege(
                frappe.session.user,
                pos_profile,
                privilege,
                amount=approval_amount,
                fallback_privileges=fallback_privs,
            ):
                attach_auth_challenge(
                    privilege=privilege, pos_profile=pos_profile,
                    reference_doctype="POS Invoice", reference_name=invoice.name,
                    claim={}, amount=approval_amount, framework=frappe,
                )
                frappe.throw(
                    _("You are not permitted to reduce or remove items from this order without manager approval.")
                    + f" [{_void_ops.CODE_AUTH_REQUIRED}]",
                    frappe.PermissionError,
                )

            sent_void_reason_info = _void_ops.resolve_pos_reason(
                privilege,
                pos_profile,
                void_reason,
                note=void_note,
                is_waste=is_waste,
                fallback_privileges=fallback_privs,
            )
            claim = {
                "invoice": invoice.name,
                "reductions": sent_reductions,
                "reason": sent_void_reason_info["code"],
                "note": sent_void_reason_info["note"],
                "is_waste": 1 if sent_void_reason_info["is_waste"] else 0,
            }
            b2a_compat_claim = {
                "action": "sync_order_void_sent_item",
                "lines": b2a_void_lines,
                "amount": voided_amount,
                "reason": (void_reason or "").strip(),
            }
            sent_void_approver = _void_ops.require_pos_override(
                privilege,
                pos_profile,
                "POS Invoice",
                invoice.name,
                claim,
                approval_token=approval_token,
                amount=approval_amount,
                doc=invoice,
                fallback_privileges=fallback_privs,
                alt_claim=b2a_compat_claim,
            )
            if sent_void_reason_info["is_waste"]:
                row_by_line = {
                    row.get("reservation_line_key"): row
                    for row in (invoice.items or [])
                    if row.get("reservation_line_key")
                }
                row_by_code = {}
                for row in (invoice.items or []):
                    code = row.get("item_code")
                    if code and code not in row_by_code:
                        row_by_code[code] = row
                sent_waste_items = []
                for r in sent_reductions:
                    src_row = row_by_line.get(r["line_key"]) or row_by_code.get(r["item_code"]) or {}
                    if (src_row.get("custom_line_role") or "") == "Combo Upcharge":
                        continue
                    sent_waste_items.append(
                        {
                            "item_code": r["item_code"],
                            "qty": abs(r["delta"]),
                            "warehouse": src_row.get("warehouse"),
                            "uom": src_row.get("uom"),
                            "custom_line_role": src_row.get("custom_line_role"),
                            "custom_bundle_overrides_json": src_row.get("custom_bundle_overrides_json"),
                        }
                    )

    if captured_at is not None:
        captured = _offline_capture_time(captured_at, pos_profile, billing_user=billing_user)
        invoice.ury_offline_captured_at = captured
        invoice.set_posting_time = 0
        invoice.posting_date = captured.date()
        invoice.posting_time = captured.time()
        if getattr(invoice, "flags", None) is not None:
            invoice.flags.ury_trusted_offline_capture = True
            invoice.flags._ury_offline_posting_dt = captured

    customer = _order_customer(invoice, posprofile, order_type, customer)
    if not customer:
        frappe.throw(_("Select a customer or set a default customer in the POS Profile."))
    else:
        _validate_customer_change(invoice, customer)
        invoice.customer = customer

    effective_order_type = invoice.order_type if collision_merge else (order_type or invoice.order_type)
    if not collision_merge:
        pax_value = no_of_pax
        if pax_value in (None, "") or (effective_order_type != "Dine In" and pax_value in (0, "0")):
            pax_value = invoice.get("no_of_pax") or (1 if effective_order_type != "Dine In" else None)
        from ury.ury_pos.covers import parse_covers

        previous_covers = parse_covers(invoice.get("no_of_pax"))
        if (invoice.name and effective_order_type == invoice.get("order_type")
                and pos_profile == invoice.get("pos_profile")
                and previous_covers and previous_covers > 0
                and parse_covers(pax_value) == previous_covers):
            validated_pax = previous_covers
        else:
            validated_pax = _validate_dine_in_pax(pax_value, effective_order_type, posprofile)

    # Privilege gates for changing order type (`order_type_change`) and guest count
    # (`guest_count_change`) on an existing open check (Simphony Opt 7 & Opt 33).
    # Offline Merge Rounds collision reconciliation appends items onto an occupied check
    # without altering its existing order_type or no_of_pax, so skip these mutation gates.
    if invoice.name and not collision_merge:
        DEFAULT_MANAGER_ROLES = _PA_DEFAULT_MANAGER_ROLES
        _is_valid_str = _pa_is_valid_str
        assert_not_offline_replay = _pa_assert_not_offline_replay
        authorize_or_perform = _pa_authorize_or_perform
        is_company_policy_active = _pa_is_company_policy_active

        _co = getattr(posprofile, "company", None) or getattr(invoice, "company", None)
        _co = _co if _is_valid_str(_co) else None
        prev_order_type = invoice.get("order_type")
        if order_type and prev_order_type and order_type != prev_order_type:
            assert_not_offline_replay(
                append=append, captured_at=captured_at, offline_printed=offline_printed
            )
            branch_no_taxes = (
                cint(frappe.db.get_value("Branch", invoice.branch, "custom_no_taxes")) == 1
                if invoice.get("branch")
                else False
            )
            tax_dropping_switch = order_type == "Aggregators" and branch_no_taxes
            _ot_policy = is_company_policy_active(_co)
            legacy_ot_allowed = bool(
                is_super_user
                or elevated_user
                or (not tax_dropping_switch and (bool(getattr(posprofile, "custom_edit_order_type", 0)) or billing_user))
            )
            if _ot_policy or order_type_approval_token or tax_dropping_switch or not legacy_ot_allowed:
                authorize_or_perform(
                    privilege="order_type_change",
                    pos_profile=pos_profile,
                    company=_co,
                    skip_profile_company_lookup=True,
                    reference_doctype="POS Invoice",
                    reference_name=invoice.name,
                    claim={
                        "action": "order_type_change",
                        "invoice": invoice.name,
                        "from_order_type": prev_order_type,
                        "to_order_type": order_type,
                    },
                    token=order_type_approval_token,
                    reason=privilege_reason,
                    reason_note=privilege_reason_note,
                    require_reason=None if _ot_policy else False,
                    force_pin=tax_dropping_switch and not bool(set(user_role) & (DEFAULT_MANAGER_ROLES | {"System Manager"})),
                    allow_fallback_predicate=legacy_ot_allowed,
                )

        prev_pax = cint(invoice.get("no_of_pax") or 0)
        incoming_pax = validated_pax
        if no_of_pax not in (None, "") and prev_pax > 0 and incoming_pax != prev_pax:
            _pax_policy = is_company_policy_active(_co)
            if _pax_policy or guest_count_approval_token:
                assert_not_offline_replay(
                    append=append, captured_at=captured_at, offline_printed=offline_printed
                )
                authorize_or_perform(
                    privilege="guest_count_change",
                    pos_profile=pos_profile,
                    company=_co,
                    skip_profile_company_lookup=True,
                    reference_doctype="POS Invoice",
                    reference_name=invoice.name,
                    claim={
                        "action": "guest_count_change",
                        "invoice": invoice.name,
                        "from_pax": prev_pax,
                        "to_pax": incoming_pax,
                    },
                    token=guest_count_approval_token,
                    reason=privilege_reason,
                    reason_note=privilege_reason_note,
                    require_reason=None if _pax_policy else False,
                    allow_fallback_predicate=True,
                )

    if order_type and not collision_merge:
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
    if not collision_merge:
        invoice.no_of_pax = validated_pax
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

    dp_mod = _dayparts_mod()
    tx_mod = _tax_mod()

    stamp_meta = dp_mod.stamp_invoice_outlet_time(
        invoice,
        at=invoice.get("ury_offline_captured_at") if captured_at is not None else None,
    )
    resolved_menu_meta = dp_mod.resolve_menu_and_price_list(
        invoice.branch,
        restaurant=invoice.get("restaurant"),
        table=table or invoice.restaurant_table,
        room=opening_room or room,
        order_type=effective_order_type,
        customer=customer,
        local_dt=stamp_meta["local_dt"],
    )
    current_period_price_list = resolved_menu_meta.get("price_list")

    if effective_order_type == "Aggregators":
        price_list = frappe.db.get_value("Aggregator Settings",{"customer": customer, "parent": invoice.branch, "parenttype": "Branch"},"price_list",)
        
        if not price_list:
            frappe.throw(f"Price list for customer {customer} in branch {invoice.branch} not found in Aggregator Settings.")
    else:
        if not invoice.name or not past_item:
            if resolved_menu_meta.get("source") == "serving_period" and current_period_price_list:
                price_list = current_period_price_list
                invoice.selling_price_list = price_list
            else:
                price_list = invoice.selling_price_list or current_period_price_list
                if price_list and not invoice.selling_price_list:
                    invoice.selling_price_list = price_list
        else:
            price_list = invoice.selling_price_list or current_period_price_list
    if getattr(invoice, "flags", None) is not None:
        invoice.flags.ury_authoritative_price_list = price_list
        invoice.flags.ury_trusted_server_pricing = True

    # dummy payment
    if invoice.invoice_created == 0:
        invoice.append(
            "payments",
            dict(mode_of_payment=mode_of_payment, amount=0, custom_tender_received=0),
        )
        invoice.invoice_created = 1

    menu = (
        resolved_menu_meta.get("menu")
        or _resolve_menu_for_sync(
            invoice.branch,
            table=table or invoice.restaurant_table,
            room=opening_room or room,
            order_type=effective_order_type,
            local_dt=stamp_meta["local_dt"],
        )
        or frappe.db.get_value("URY Menu", {"branch": invoice.branch}, "name")
    )
    _sync_order_company = invoice.company or getattr(posprofile, "company", None) or frappe.db.get_value("Branch", invoice.branch, "company")

    # Validate G3 modifiers & G4 combo choices and resolve authoritative 0/2/3dp server prices.
    from ury.ury.api.ury_modifiers import validate_and_normalize_order_items

    rate_precision = 2
    if callable(getattr(invoice, "precision", None)):
        try:
            prec = invoice.precision("rate")
            if isinstance(prec, int) and not isinstance(prec, bool):
                rate_precision = prec
        except Exception:
            pass
    if invoice.get("currency"):
        try:
            frac = cint(frappe.db.get_value("Currency", invoice.currency, "fraction_units"))
            if frac >= 1:
                rate_precision = max(0, len(str(frac)) - 1)
        except Exception:
            pass

    items, modifier_price_overrides = validate_and_normalize_order_items(
        items,
        past_item,
        menu,
        price_list,
        invoice.branch,
        _sync_order_company,
        posting_date=invoice_pricing_date(invoice),
        customer=invoice.customer,
        currency_precision=rate_precision,
    )

    # Reject newly-added lines that are disabled, off-menu, or outside daypart window
    # before pricing runs; historic lines already on the invoice are exempt
    # (see _validate_sync_items_against_menu docstring).
    _validate_sync_items_against_menu(
        items,
        past_item,
        invoice.branch,
        table=table,
        room=opening_room or room,
        order_type=effective_order_type,
        local_dt=stamp_meta["local_dt"],
        active_period=resolved_menu_meta.get("serving_period"),
        validated_modifier_keys=set(modifier_price_overrides.keys()),
    )
    from ury.ury.api.ury_availability import apply_count_delta

    apply_count_delta(invoice.branch, past_item, past_item + items if append else items, invoice=invoice)

    previous_by_key = {
        snapshot["reservation_line_key"]: row
        for row, snapshot in zip(invoice.items, past_item, strict=True)
        if snapshot.get("reservation_line_key")
    }
    period_crossed = (
        bool(invoice.name and past_item)
        and effective_order_type != "Aggregators"
        and bool(current_period_price_list and invoice.selling_price_list)
        and current_period_price_list != invoice.selling_price_list
    )

    if period_crossed:
        # Open-check rate stability (G40): keep already-added lines at their original
        # period rates; price newly added lines at the active period's price list.
        priced_items = []
        pricing_dt = invoice_pricing_date(invoice)
        for src in items:
            prev_row = previous_rows.get(src.get("name")) or previous_by_key.get(src.get("reservation_line_key"))
            target_pl = price_list if prev_row is not None else current_period_price_list
            priced_one = price_items_for_invoice(
                [src], target_pl, pos_profile, invoice.branch, menu, posting_date=pricing_dt,
                customer=invoice.customer, price_overrides=modifier_price_overrides,
            )[0]
            if prev_row is not None:
                priced_one["rate"] = prev_row.get("rate")
                priced_one["price_list_rate"] = prev_row.get("price_list_rate") or prev_row.get("rate")
                priced_one["base_price_list_rate"] = prev_row.get("base_price_list_rate") or prev_row.get("rate")
                for tax_field in ("item_tax_template", "item_tax_rate", "custom_tax_exempt"):
                    if prev_row.get(tax_field) is not None:
                        priced_one[tax_field] = prev_row.get(tax_field)
            priced_items.append(priced_one)
    else:
        priced_items = price_items_for_invoice(
            items, price_list, pos_profile, invoice.branch, menu, posting_date=invoice_pricing_date(invoice),
            customer=invoice.customer, price_overrides=modifier_price_overrides,
        )
        for src, item_dict in zip(items, priced_items, strict=True):
            prev_row = previous_rows.get(src.get("name")) or previous_by_key.get(src.get("reservation_line_key"))
            if prev_row is not None:
                for tax_field in ("item_tax_template", "item_tax_rate", "custom_tax_exempt"):
                    if prev_row.get(tax_field) is not None:
                        item_dict[tax_field] = prev_row.get(tax_field)
    prior_placements = defaultdict(set)
    unkeyed_issued = defaultdict(list)
    for saved, row in zip(invoice.items, past_item, strict=True):
        if not row.get("reservation_line_key"):
            placement = (row["item_code"], row.get("comments") or "", cint(row.get("custom_seat_no")))
            prior_placements[placement].add(row.get("custom_course"))
            if not append and saved.get("name") not in requested_names and not row.get("ury_held"):
                unkeyed_issued[(*placement, row.get("custom_course"))].append(saved.get("warehouse"))
    for src, priced in zip(items, priced_items, strict=True):
        previous = previous_rows.get(src.get("name")) or previous_by_key.get(src.get("reservation_line_key"))
        if previous and not src.get("custom_course") and previous.get("custom_course"):
            src["custom_course"] = priced["custom_course"] = previous.get("custom_course")
        if not src.get("custom_course") and not src.get("reservation_line_key"):
            matches = prior_placements[(src.get("item"), src.get("comment") or "", cint(src.get("custom_seat_no")))]
            if len(matches) == 1:
                src["custom_course"] = next(iter(matches))
    if not append:
        _assert_shared_kitchen_lines_unchanged(past_item, items)
    if get_branch_stock_policy(branch=invoice.branch, company=_sync_order_company).reservation_control_enabled:
        anchored = {_line_ref(row) for row in past_item if row.get("custom_kitchen_anchor")}
        accepted = past_item + items if append else items
        reconcile_order_reservations(
            order_ref=_ensure_invoice_reservation_ref(invoice),
            previous_items=[row for row in past_item if _line_ref(row) not in anchored],
            accepted_items=[row for row in accepted if _line_ref(row) not in anchored],
            branch=invoice.branch,
            company=_sync_order_company,
            actor=frappe.session.user,
        )

    if not append:
        invoice.items = []
    for source, item_dict in zip(items, priced_items, strict=True):
        old_row = previous_rows.get(source.get("name")) if source.get("name") else None
        origin_row = old_row or previous_by_key.get(source.get("reservation_line_key"))
        # Closing issues this saved portion from the kitchen that fired it.
        if (origin_row and origin_row.get("item_code") == item_dict["item_code"]
                and not cint(origin_row.get("ury_held")) and origin_row.get("warehouse")):
            item_dict["warehouse"] = origin_row.get("warehouse")
        elif unkeyed_issued and not origin_row and not source.get("reservation_line_key") and not cint(source.get("ury_held")):
            # Older carts identify a portion by dish, note, seat and course.
            placement = (source["item"], source.get("comment") or "", cint(source.get("custom_seat_no")),
                         source.get("custom_course"))
            warehouses = unkeyed_issued.get(placement)
            if warehouses:
                if len(set(warehouses)) > 1:
                    _ambiguous_issued_lines()
                warehouse = warehouses.pop()
                if warehouse:
                    item_dict["warehouse"] = warehouse
        if origin_row and origin_row.get("custom_origin_invoice_item"):
            item_dict["custom_origin_invoice_item"] = origin_row.get("custom_origin_invoice_item")
            item_dict["custom_kitchen_anchor"] = origin_row.get("custom_kitchen_anchor")
        if source.get("name"):
            item_dict["name"] = source["name"]
        # Preserve server-authorised price overrides, open items, and line comps across normal edits.
        if old_row and (
            cint(old_row.get("custom_price_overridden")) == 1
            or cint(old_row.get("custom_is_open_item")) == 1
            or cint(old_row.get("custom_is_complimentary")) == 1
        ):
            for preserved_field in (
                "rate",
                "price_list_rate",
                "base_price_list_rate",
                "discount_percentage",
                "discount_amount",
                "ignore_pricing_rule",
                "custom_original_price_list_rate",
                "custom_price_overridden",
                "custom_price_override_reason",
                "custom_price_override_approver",
                "custom_is_open_item",
                "custom_open_item_reference",
                "custom_is_complimentary",
                "custom_comp_type",
                "custom_comp_reason",
                "custom_comp_approver",
                "item_tax_template",
                "item_tax_rate",
                "custom_efris_tax_category",
            ):
                if old_row.get(preserved_field) is not None:
                    item_dict[preserved_field] = old_row.get(preserved_field)
            if old_row.get("custom_is_open_item") and old_row.get("item_name"):
                item_dict["item_name"] = old_row.get("item_name")
        item_dict["ury_held"] = cint(source.get("ury_held"))
        if old_row and cint(old_row.get("ury_held")):
            for field in ("fire_at", "fire_confirmation_pending", "custom_course", "custom_seat_no"):
                if old_row.get(field) is not None:
                    item_dict[field] = old_row.get(field)
        invoice.append("items", item_dict)

    # G42: Apply SAP MWST / Simphony order-type tax determination before service charge, expected_total & save
    tx_mod.apply_order_type_tax(invoice, posprofile, customer_doc=customerdoc)
    _sync_service_charge(invoice, posprofile)

    if expected_total is not None:
        # Compare the server-priced, tax-inclusive total without reapplying profile defaults.
        invoice.set_missing_values(for_validate=True)
        invoice.calculate_taxes_and_totals()
        precision = invoice.precision("grand_total")
        actual_total = flt(invoice.grand_total, precision)
        expected_val = flt(expected_total, precision)
        merge_delta = flt(invoice.grand_total - pre_merge_grand_total, precision) if collision_merge else None
        if expected_val != actual_total and (not collision_merge or expected_val != merge_delta):
            _offline_refusal(
                "URY_OFFLINE_TOTAL_MISMATCH", _("Server-priced invoice total differs from the captured total.")
            )

    if client_order_id:
        if not collision_merge:
            invoice.ury_client_order_id = client_order_id
        if effective_round_id is not None:
            _record_round(invoice, effective_round_id, round_digest)

    prev_split_flag = getattr(frappe.flags, "ury_bill_split", False)
    if collision_split_create:
        frappe.flags.ury_bill_split = True
    prev_void_flag = getattr(frappe.flags, "ury_authorized_void", False)
    if sent_void_reason_info is not None:
        frappe.flags.ury_authorized_void = True
    prev_priv_verified = getattr(getattr(invoice, "flags", None), "ury_privileged_action_verified", False)
    if hasattr(invoice, "flags"):
        invoice.flags.ury_privileged_action_verified = True
    try:
        invoice.save(ignore_permissions=collision_merge)
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
    finally:
        frappe.flags.ury_bill_split = prev_split_flag
        frappe.flags.ury_authorized_void = prev_void_flag
        if hasattr(invoice, "flags"):
            invoice.flags.ury_privileged_action_verified = prev_priv_verified

    if collision_plan:
        try:
            frappe.publish_realtime(
                "offline_table_collision",
                {
                    "invoice": invoice.name,
                    "table": table,
                    "policy": collision_plan["action"],
                    "client_order_id": client_order_id,
                    "waiter": frappe.session.user,
                    "branch": invoice.branch,
                },
            )
        except Exception:
            pass

    current_kitchen = _kitchen_snapshot(items)
    previous_kitchen = [] if append else _kitchen_snapshot(past_item)
    held_only_edit = (
        not append
        and current_kitchen == previous_kitchen
        and (any(cint(row.get("ury_held")) for row in past_item) or any(cint(row.get("ury_held")) for row in items))
    )
    cancel_kot_names = []
    if not held_only_edit:
        try:
            kot_kwargs = {"offline_printed": offline_printed}
            if sent_void_reason_info is not None or collision_merge:
                import inspect

                sig_params = inspect.signature(kot_execute).parameters
                if sent_void_reason_info is not None:
                    if "cancel_reason" in sig_params:
                        kot_kwargs["cancel_reason"] = sent_void_reason_info["display"]
                    if "void_reason_code" in sig_params:
                        kot_kwargs["void_reason_code"] = sent_void_reason_info["code"]
                if collision_merge and "ignore_permissions" in sig_params:
                    kot_kwargs["ignore_permissions"] = True
            kot_res = kot_execute(
                invoice.name, customer, table, current_kitchen, previous_kitchen, invoice.custom_comments,
                **kot_kwargs,
            )
            if isinstance(kot_res, list):
                cancel_kot_names = kot_res
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

    if sent_void_reason_info and sent_void_reason_info["is_waste"] and sent_waste_items:
        _void_ops.post_waste_stock_entry(
            invoice,
            posprofile,
            sent_waste_items,
            sent_void_reason_info,
            cancel_kot_names=cancel_kot_names,
            approver=sent_void_approver,
        )

    # table status
    if invoice.restaurant_table and invoice.invoice_printed == 0:
        from ury.ury.api.table_lifecycle import set_table_state

        set_table_state(invoice.restaurant_table, "Seated", since=invoice.creation)
        for merged_table in (invoice.custom_merged_tables or "").split(","):
            if merged_table.strip():
                set_table_state(merged_table.strip(), "Seated", since=invoice.creation)

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
def table_transfer(table, newTable, invoice, approval_token=None, reason=None, reason_note=None,
                   join_roster=0, target_pos_profile=None):
    from ury.ury_pos.floor_actions import assert_table_free_for_transfer, validate_table_restaurant
    from ury.ury.api.table_lifecycle import assert_table_seatable, update_tables_after_transfer
    from ury.ury.api.table_reservation import lock_reservation_tables

    _is_valid_str = _pa_is_valid_str
    assert_not_efris_fiscalised = _pa_assert_not_efris_fiscalised
    assert_not_offline_replay = _pa_assert_not_offline_replay
    authorize_or_perform = _pa_authorize_or_perform
    is_company_policy_active = _pa_is_company_policy_active

    assert_not_offline_replay()

    # Use the host stand's table-first lock order before locking the invoice.
    roster = cint(join_roster) == 1
    destination_group, snapshot = _get_merge_cluster(newTable) if roster else ([newTable], {})
    lock_reservation_tables([table, *destination_group])
    for member in destination_group if roster else ():
        current_links = frappe.db.get_value("URY Table", member, "merged_with", for_update=True)
        links = set(_parse_merged_with(current_links))
        if not links.issubset(destination_group):
            frappe.throw(_("The merged tables must belong to the same room."))
        if links != set(_parse_merged_with(snapshot[member].merged_with)):
            frappe.throw(_("The table group changed. Refresh the floor and try again."))
    if table in destination_group:
        frappe.throw(_("Choose a different table group."))
    pos_invoice = frappe.get_doc("POS Invoice", invoice, for_update=True)
    locked_tables = {
        t: frappe.get_doc("URY Table", t, for_update=True)
        for t in sorted({t for t in (table, *destination_group) if t})
    }
    current_table = locked_tables.get(table)
    new_table = locked_tables[newTable]

    if not frappe.has_permission("POS Invoice", "write", doc=pos_invoice):
        frappe.throw(_("Not permitted to transfer this order"), frappe.PermissionError)

    if cint(pos_invoice.docstatus) != 0:
        frappe.throw(_("Only open draft checks can be transferred."), frappe.ValidationError)
    assert_not_efris_fiscalised(pos_invoice)

    # Anti-race check: reject if another waiter already transferred this check elsewhere.
    old_table_name = current_table.name if current_table and isinstance(current_table.name, str) else table
    if (pos_invoice.restaurant_table or None) != (old_table_name or None):
        frappe.throw(
            coded_message(
                "TRANSFER-CONFLICT",
                _("This check has already been transferred to another table. Reload and try again."),
            ),
            frappe.ValidationError,
        )

    if not pos_invoice.pos_profile:
        frappe.throw(
            _("Cannot verify transfer permission: order has no POS Profile."),
            frappe.PermissionError,
        )

    pos_profile_doc = frappe.get_doc("POS Profile", pos_invoice.pos_profile)

    raw_clock_in_req = pos_profile_doc.get("custom_require_clock_in_to_order")
    if isinstance(raw_clock_in_req, (int, float, str)) and cint(raw_clock_in_req) == 1:
        from ury.ury_pos import timeclock

        timeclock.enforce_clock_in_for_profile(pos_profile_doc, user=frappe.session.user)

    merge_members = _get_merge_cluster(table)[0] if table else []
    if len(merge_members) > 1:
        frappe.throw(_("Table transfer is not allowed for merged tables. Unmerge first."))

    if not new_table.branch or not frappe.has_permission("Branch", "read", doc=new_table.branch):
        frappe.throw(_("Not permitted to transfer checks to this branch."), frappe.PermissionError)

    target_profile = None
    if (target_pos_profile or not current_table or current_table.branch != new_table.branch
            or (roster and current_table.get("restaurant") != new_table.get("restaurant"))):
        if not (roster or target_pos_profile):
            frappe.throw(_("Table transfer between different branches is restricted."))
        from ury.ury_pos.floor_actions import validate_outlet_transfer

        if not target_pos_profile:
            from ury.ury_pos.outlet_context import get_authorized_outlets
            from ury.ury_pos.floor_actions import check_currency, destination_profiles
            currency = check_currency(pos_invoice)
            profiles = destination_profiles(get_authorized_outlets(), new_table.branch, pos_invoice.company, currency, new_table.get("restaurant"))
            if not profiles:
                frappe.throw(_("No authorized destination profile uses the check currency."), frappe.PermissionError)
            target_name = profiles[0]
        else:
            target_name = target_pos_profile
        target_profile = frappe.get_doc("POS Profile", target_name)
        validate_outlet_transfer(pos_invoice, target_profile, table_branch=new_table.branch)
        if not target_profile.branch:
            from ury.ury_pos.outlet_context import get_authorized_outlets
            allowed_branch = new_table.branch and any(
                o["branch"] == new_table.branch and o["company"] == pos_invoice.company
                for o in get_authorized_outlets()
            )
        else:
            allowed_branch = target_profile.branch == new_table.branch
        if not allowed_branch or cint(target_profile.get("disabled")):
            frappe.throw(_("Target table does not belong to an enabled destination outlet."))

    # Tables inherit company via branch/restaurant. Validate that scope once,
    # then require every locked member to have the identical pair of links.
    validate_table_restaurant(new_table, target_profile or pos_profile_doc, pos_invoice.get("company"))
    for member in destination_group:
        partner = locked_tables[member]
        if partner.branch != new_table.branch or partner.get("restaurant") != new_table.get("restaurant"):
            frappe.throw(_("A merged table group must belong to one branch and restaurant."))
        assert_table_seatable(member, new_table.branch)
    if not roster:
        assert_table_free_for_transfer(new_table, exclude_invoice=pos_invoice.name)

    raw_company = getattr(pos_invoice, "company", None) or getattr(pos_profile_doc, "company", None)
    company = raw_company if _is_valid_str(raw_company) else None
    policy_active = is_company_policy_active(company)
    legacy_allowed = _has_role(frappe.get_roles(), pos_profile_doc.transfer_role_permissions)

    claim = {"action": "table_transfer", "from_table": old_table_name, "to_table": newTable, "invoice": invoice}
    if target_pos_profile:
        claim = {"action": "transfer_check_outlet", "from_profile": pos_invoice.pos_profile,
                 "to_profile": target_pos_profile, "to_table": newTable, "invoice": invoice}
        from ury.ury_pos.privileged_auth import DEFAULT_MANAGER_ROLES
        legacy_allowed = legacy_allowed or bool(set(frappe.get_roles()) & DEFAULT_MANAGER_ROLES)

    auth = authorize_or_perform(
        privilege="transfer_check",
        pos_profile=pos_invoice.pos_profile,
        company=company,
        skip_profile_company_lookup=True,
        reference_doctype="POS Invoice",
        reference_name=pos_invoice.name,
        claim=claim,
        token=approval_token,
        reason=reason,
        reason_note=reason_note,
        require_reason=None if policy_active else False,
        allow_fallback_predicate=legacy_allowed,
        deny_message=_("You are not permitted to transfer tables."),
    )

    from ury.ury.api.table_reservation import (
        check_table_reservation,
        parse_to_datetime,
        transfer_seated_reservation,
        validate_seated_reservation_transfer,
    )
    for member in destination_group:
        active_res = check_table_reservation(member, for_update=True)
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

    if old_table_name:
        validate_seated_reservation_transfer(old_table_name, new_table.name, pos_invoice)

    if len(destination_group) > 1:
        from ury.ury.api.table_reservation import open_checks_on

        existing = open_checks_on(destination_group, for_update=True)
        primary = next((row.restaurant_table for row in sorted(existing, key=lambda r: str(r.creation))
                        if row.restaurant_table in destination_group), new_table.name)
        new_table = locked_tables[primary]
    if roster:
        pos_invoice.custom_merged_tables = ",".join(sorted(set(destination_group) - {new_table.name})) or None
    pos_invoice.restaurant_table = new_table.name
    pos_invoice.custom_restaurant_room = new_table.restaurant_room
    changing_outlet = target_profile and (
        target_profile.name != pos_invoice.pos_profile or new_table.branch != pos_invoice.branch
    )
    if changing_outlet:
        from ury.ury_pos.floor_actions import rebind_outlet
        rebind_outlet(pos_invoice, target_profile, table_branch=new_table.branch)
        pos_profile_doc = target_profile
    _sync_service_charge(pos_invoice, pos_profile_doc)
    if callable(getattr(pos_invoice, "calculate_taxes_and_totals", None)):
        pos_invoice.calculate_taxes_and_totals()
    prev_priv_verified = getattr(getattr(pos_invoice, "flags", None), "ury_privileged_action_verified", False)
    if hasattr(pos_invoice, "flags"):
        pos_invoice.flags.ury_privileged_action_verified = True
    try:
        if changing_outlet:
            from ury.ury_pos.transfer_context import outlet_transfer_save
            with outlet_transfer_save(pos_invoice):
                pos_invoice.save()
        else:
            pos_invoice.save()
    finally:
        if hasattr(pos_invoice, "flags"):
            pos_invoice.flags.ury_privileged_action_verified = prev_priv_verified

    if old_table_name:
        transfer_seated_reservation(old_table_name, new_table.name, pos_invoice)
    destination_state = update_tables_after_transfer(old_table_name, new_table.name, pos_invoice)

    if len(destination_group) > 1:
        from ury.ury.api.table_lifecycle import set_table_state
        for member in destination_group:
            if member != new_table.name:
                set_table_state(member, destination_state, check=pos_invoice.name)

    # Do not swallow KOT-transfer failures: a raise here aborts the request
    # (and its transaction) so the table/invoice changes above are rolled
    # back instead of silently leaving order/table/KOT locations
    # inconsistent. An empty matching-KOT set is a valid no-op and does not
    # raise.
    change_table_in_kot(pos_invoice.name, new_table.name, pos_invoice.branch)
    if target_pos_profile:
        return {"invoice": invoice, "pos_profile": pos_invoice.pos_profile, "branch": pos_invoice.branch,
                "restaurant_table": pos_invoice.restaurant_table, "approver": auth["approver"]}



@frappe.whitelist()
def get_captain_transfer_candidates(invoice):
    pos_invoice_doc, pos_profile_doc = _authorize_captain_transfer(invoice)
    return _get_captain_transfer_candidates(pos_invoice_doc, pos_profile_doc)


@frappe.whitelist()
def captain_transfer(currentCaptain, newCaptain, invoice, approval_token=None, reason=None, reason_note=None):
    _is_valid_str = _pa_is_valid_str
    assert_not_offline_replay = _pa_assert_not_offline_replay
    authorize_or_perform = _pa_authorize_or_perform
    is_company_policy_active = _pa_is_company_policy_active

    assert_not_offline_replay()

    try:
        pos_invoice_doc = frappe.get_doc("POS Invoice", invoice, for_update=True)
    except TypeError:
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

    raw_company = getattr(pos_invoice_doc, "company", None) or getattr(pos_profile_doc, "company", None)
    company = raw_company if _is_valid_str(raw_company) else None
    policy_active = is_company_policy_active(company)
    legacy_allowed = _has_role(frappe.get_roles(), pos_profile_doc.transfer_role_permissions)

    # Validate refusal (including newCaptain == current waiter) before conflict check and token consumption.
    refusal = _captain_transfer_refusal(pos_invoice_doc, pos_profile_doc, newCaptain)
    if refusal:
        frappe.throw(refusal, frappe.PermissionError)

    # Anti-race check: reject if another transfer already changed the check's server.
    if currentCaptain and pos_invoice_doc.waiter and currentCaptain != pos_invoice_doc.waiter:
        frappe.throw(
            coded_message(
                "TRANSFER-CONFLICT",
                _("This check is now owned by another server. Reload and try again."),
            ),
            frappe.ValidationError,
        )

    authorize_or_perform(
        privilege="transfer_check",
        pos_profile=pos_profile,
        company=company,
        skip_profile_company_lookup=True,
        reference_doctype="POS Invoice",
        reference_name=pos_invoice_doc.name,
        claim={
            "action": "captain_transfer",
            "from_waiter": pos_invoice_doc.waiter or "",
            "to_waiter": newCaptain,
            "invoice": pos_invoice_doc.name,
        },
        token=approval_token,
        reason=reason,
        reason_note=reason_note,
        require_reason=None if policy_active else False,
        allow_fallback_predicate=legacy_allowed,
        deny_message=_("You are not permitted to transfer captains."),
    )

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
def cancel_order(invoice_id, reason, note=None, is_waste=0, approval_token=None):
    import inspect

    pos_invoice = frappe.get_doc("POS Invoice", invoice_id, for_update=True)
    session_user = getattr(getattr(frappe, "session", None), "user", None) or ""
    profile_name = _void_ops._safe_profile_name(getattr(pos_invoice, "pos_profile", None))
    sent_activity = _void_ops.has_sent_kitchen_activity(pos_invoice)

    # Authorization gate before any lookup or mutation:
    # - With a manager PIN `approval_token`, the cashier/waiter only needs read
    #   access on the check while the manager token supplies cancel_check authority.
    # - For an unsent (all-held, no-KOT, unprinted) draft, the check's own waiter/owner
    #   with write permission may cancel without a manager PIN (Simphony Opt 40).
    # - Otherwise, require native cancel permission.
    is_owner_of_unsent = (
        pos_invoice.docstatus == 0
        and not sent_activity
        and session_user
        and session_user in (getattr(pos_invoice, "waiter", None), getattr(pos_invoice, "owner", None))
        and frappe.has_permission("POS Invoice", "write", doc=pos_invoice)
    )
    if approval_token:
        if not frappe.has_permission("POS Invoice", "read", doc=pos_invoice):
            frappe.throw(
                _("You do not have permission to cancel this invoice."),
                frappe.PermissionError,
            )
    elif not is_owner_of_unsent:
        if not frappe.has_permission("POS Invoice", "cancel", doc=pos_invoice):
            frappe.throw(
                _("You do not have permission to cancel this invoice."),
                frappe.PermissionError,
            )

    # Branch scoping: the invoice must belong to the session user's branch
    # (mirrors the getBranch() pattern used across the URY APIs).
    if session_user != "Administrator":
        user_branch = getBranch()
        if pos_invoice.branch != user_branch:
            frappe.throw(
                _("You are not allowed to cancel an invoice from another branch."),
                frappe.PermissionError,
            )

    if pos_invoice.docstatus == 2:
        frappe.throw(_("Invoice {0} is already cancelled.").format(invoice_id))

    # Closed / fiscalised checks may NEVER be cancelled directly — void a closed check only through a return.
    if pos_invoice.docstatus == 1:
        if _void_ops.is_fiscalised_invoice(pos_invoice):
            frappe.throw(
                _("This check was fiscalised and cannot be cancelled directly. Process a return / credit note instead. [{0}]").format(
                    _void_ops.CODE_VOID_FISCALISED_USE_RETURN
                ),
            )
        frappe.throw(
            _("Closed checks cannot be cancelled directly. Process a return instead. [{0}]").format(
                _void_ops.CODE_VOID_CLOSED_USE_RETURN
            ),
        )

    # Partially paid open checks with received tenders cannot be discarded.
    if _void_ops.received_tender_total(pos_invoice) > 0:
        frappe.throw(
            _("This check already has payments received and cannot be cancelled. Settle and process a return instead. [{0}]").format(
                _void_ops.CODE_VOID_PARTIAL_PAYMENT
            ),
        )

    use_pos_reason = bool(_void_ops._active_pos_reasons("cancel_check"))
    if use_pos_reason:
        reason_info = _void_ops.resolve_pos_reason(
            "cancel_check",
            profile_name,
            reason,
            note=note,
            is_waste=is_waste,
        )
        stored_reason = reason_info["stored"]
    else:
        stored_reason = _validated_void_reason(pos_invoice, reason)
        reason_info = {
            "code": stored_reason,
            "description": stored_reason,
            "note": note.strip() if isinstance(note, str) else "",
            "display": f"[WASTE] {stored_reason}" if cint(is_waste) == 1 else stored_reason,
            "stored": stored_reason,
            "is_waste": cint(is_waste) == 1 or stored_reason.upper().startswith("WASTE"),
        }

    approver = None
    if sent_activity and profile_name and (_void_ops._override_module() is not None or approval_token):
        claim = {
            "invoice": invoice_id,
            "reason": reason_info["code"],
            "note": reason_info["note"],
            "is_waste": 1 if reason_info["is_waste"] else 0,
        }
        approver = _void_ops.require_pos_override(
            "cancel_check",
            profile_name,
            "POS Invoice",
            invoice_id,
            claim,
            approval_token=approval_token,
            amount=company_approval_amount(
                pos_invoice, flt(pos_invoice.get("rounded_total") or pos_invoice.get("grand_total")), frappe
            ),
        )
        # Ensure the approving manager holds cancel/write authority on the check.
        if approval_token and not frappe.has_permission("POS Invoice", "cancel", doc=pos_invoice, user=approver):
            frappe.throw(
                _("The approving manager does not hold permission to cancel this check."),
                frappe.PermissionError,
            )

    assert_no_shared_kitchen_void(pos_invoice)

    from ury.ury.api.ury_availability import apply_count_delta

    apply_count_delta(pos_invoice.branch, pos_invoice.items, [], invoice=pos_invoice)

    # Release the full merge cluster, not only the primary table and CSV partners.
    if pos_invoice.restaurant_table:
        from ury.ury.api.table_lifecycle import holds_until_settlement, table_policy

        policy = table_policy(pos_invoice.get("branch"))
        if not holds_until_settlement(pos_invoice.get("branch")) and not cint(policy.get("custom_enable_bussing_state")):
            release_merge_cluster_tables(pos_invoice.restaurant_table, commit=False)

    # KOT cancellation is part of the order-cancellation transaction. Skip only
    # when the check was genuinely unsent (all items held, no sent KOTs).
    cancel_kot_names = []
    if sent_activity or not isinstance(pos_invoice.get("items"), (list, tuple)) or not pos_invoice.items:
        # Preserve legacy 1-arg test doubles like `lambda name: ...` in test_cancel_order_transaction.
        try:
            sig = inspect.signature(cancel_kot)
            if "reason" in sig.parameters:
                kot_res = cancel_kot(
                    invoice_id,
                    reason=reason_info["display"],
                    void_reason_code=reason_info["code"],
                )
            else:
                kot_res = cancel_kot(invoice_id)
        except (TypeError, ValueError):
            kot_res = cancel_kot(invoice_id)
        if isinstance(kot_res, list):
            cancel_kot_names = kot_res

    waste_stock_entry = None
    if sent_activity and reason_info["is_waste"] and profile_name:
        sent_rows = [
            {
                "item_code": r.get("item_code"),
                "qty": flt(r.get("qty")),
                "warehouse": r.get("warehouse"),
                "uom": r.get("uom"),
                "custom_line_role": r.get("custom_line_role"),
                "custom_bundle_overrides_json": r.get("custom_bundle_overrides_json"),
            }
            for r in (pos_invoice.get("items") or [])
            if flt(r.get("qty")) > 0
            and cint(r.get("ury_held")) == 0
            and (r.get("custom_line_role") or "") != "Combo Upcharge"
        ]
        if sent_rows:
            pos_profile_doc = frappe.get_doc("POS Profile", profile_name)
            waste_stock_entry = _void_ops.post_waste_stock_entry(
                pos_invoice,
                pos_profile_doc,
                sent_rows,
                reason_info,
                cancel_kot_names=cancel_kot_names,
                approver=approver,
            )

    # Release any still-active stock reservations held for this order.
    release_order_reservations(invoice_id, reason=f"Order cancelled: {stored_reason}" if stored_reason else "Order cancelled")

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
        frappe.log_error(
            title="Delayed cancellation alert notification failed",
            message=f"Failed to send fraud alert for invoice {invoice_id}: {str(e)}"
        )

    if approval_token and getattr(pos_invoice, "flags", None) is not None:
        pos_invoice.flags.ignore_permissions = True
    pos_invoice.discard()
    pos_invoice.db_set("cancel_reason", stored_reason)
    if approver:
        try:
            if frappe.get_meta("POS Invoice").has_field("custom_return_approver"):
                pos_invoice.db_set("custom_return_approver", approver, update_modified=False)
        except Exception:
            pass
    return {
        "invoice": invoice_id,
        "status": "Cancelled",
        "reason": stored_reason,
        "is_waste": bool(reason_info["is_waste"]),
        "waste_stock_entry": waste_stock_entry,
        "approver": approver,
    }

def _set_settlement_customer(invoice, customer):
    if invoice.customer != customer:
        _validate_customer_change(invoice, customer)
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


def _enforce_on_account(
    invoice,
    profile,
    customer_disabled,
    target=None,
    *,
    on_account_approval=None,
    on_account_reason=None,
    consume_token=True,
):
    from ury.ury_pos import override
    from ury.ury_pos.credit import balance, credit_limit, default_accounts, require_credit_access
    from ury.ury_pos.offline_tenders import currency_precision

    bills = (invoice, target) if target else (invoice,)
    proposed = 0
    for bill in bills:
        remaining = flt(bill.rounded_total or bill.grand_total) - sum(flt(row.amount) for row in bill.payments)
        if flt(remaining, bill.precision("outstanding_amount")) > 0:
            if flt(bill.conversion_rate) <= 0:
                frappe.throw(_("Missing invoice conversion rate."))
            proposed += remaining * flt(bill.conversion_rate)
    if not proposed:
        invoice.flags.pos_verified_on_account_checked = True
        if target:
            target.flags.pos_verified_on_account_checked = True
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
    raw_company_currency = frappe.get_cached_value("Company", invoice.company, "default_currency")
    acct_currency = (
        raw_company_currency.strip()
        if isinstance(raw_company_currency, str) and raw_company_currency.strip()
        else (invoice.get("currency") or profile.get("currency") or "")
    )
    claim_prec = currency_precision(acct_currency)
    precision = max(invoice.precision("base_grand_total"), claim_prec)
    if flt(projected, precision) > flt(limit, precision):
        rounded_proposed = round(flt(proposed), claim_prec)
        rounded_limit = round(flt(limit), claim_prec)
        approval_key = (customer, rounded_proposed, rounded_limit)
        already_verified = getattr(getattr(invoice, "flags", None), "pos_verified_on_account_override", None)
        if already_verified == approval_key:
            invoice.flags.pos_verified_on_account_checked = True
            if target:
                target.flags.pos_verified_on_account_checked = True
            return True
        # Native Customer Credit Limit / bypass_credit_limit_check / credit_controller
        # NEVER silently bypass at the till: an over-limit charge requires consumed
        # `on_account_over_limit` manager PIN approval.
        if not on_account_approval:
            frappe.throw(
                coded_message("CREDIT-004", _("The charge exceeds the available credit."))
                + " [POS-AUTH-REQUIRED]",
                frappe.ValidationError,
            )
        # Two-step draft save (`make_invoice(..., submit_invoice=0)`): defer consuming
        # the single-use approval token until actual submission (`submit_order_invoice`).
        if not consume_token:
            return True
        claim = {
            "customer": customer,
            "company": invoice.company,
            "proposed": rounded_proposed,
            "limit": rounded_limit,
            "currency": acct_currency,
            "reason": (on_account_reason or "").strip(),
        }
        consumed = override.consume(
            on_account_approval,
            "on_account_over_limit",
            profile.name,
            invoice.name,
            claim,
            approval_amount=rounded_proposed,
        )
        approver = consumed.get("approver") if isinstance(consumed, dict) else getattr(consumed, "approver", None)
        approved_reason = (
            (
                (consumed.get("claim") or {}).get("reason")
                if isinstance(consumed, dict)
                else (getattr(consumed, "claim", None) or {}).get("reason")
            )
            or (on_account_reason or "").strip()
        )
        invoice.custom_override_approver = approver
        invoice.custom_override_reason = str(approved_reason)[:200]
        invoice.flags.pos_verified_on_account_override = approval_key
        if target:
            target.custom_override_approver = approver
            target.custom_override_reason = str(approved_reason)[:200]
            target.flags.pos_verified_on_account_override = approval_key
    invoice.flags.pos_verified_on_account_checked = True
    if target:
        target.flags.pos_verified_on_account_checked = True
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


@contextmanager
def _split_discount_pricing(bill, partner, *, preview=False, requested=True):
    from ury.ury_pos.split_balances import reprice_split_discount

    documents = (bill, partner) if requested else ()
    precisions = None if preview else _split_balance_precisions
    with reprice_split_discount(documents, precisions=precisions) as checks:
        yield
        if not preview:
            for check in checks:
                with with_currency_precision(check):
                    check.calculate_taxes_and_totals()


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
    complimentary=0,
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
        complimentary=complimentary,
    )
    bill = frappe.get_doc("POS Invoice", invoice)
    bill.check_permission("read")
    _enforce_order_access(bill)
    if bill.pos_profile and bill.pos_profile != pos_profile:
        frappe.throw(_('Use the order’s own POS Profile.'), frappe.PermissionError)

    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    partner = frappe.get_doc("POS Invoice", bill.custom_merged_pos_invoice) if bill.custom_merged_pos_invoice else None

    bill_snap = _snapshot_doc_discount_state(bill)
    bill_orig_taxes = list(bill.get("taxes") or [])
    partner_snap = _snapshot_doc_discount_state(partner) if partner is not None else None
    partner_orig_taxes = list(partner.get("taxes") or []) if partner is not None else None
    try:
        _sync_service_charge(bill, profile)
        if partner is not None:
            _sync_service_charge(partner, profile)
        with _split_discount_pricing(bill, partner, preview=True, requested=spec["has_any_arg"]):
            return apply_discount_to_invoice(
                bill,
                profile,
                spec,
                require_reason=False,
                enforce_auth=False,
                linked_partner=partner,
            )
    finally:
        bill.set("taxes", bill_orig_taxes)
        _restore_doc_discount_state(bill, bill_snap)
        if partner is not None and partner_snap is not None:
            partner.set("taxes", partner_orig_taxes)
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
    complimentary=0,
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
        complimentary=complimentary,
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
    with _split_discount_pricing(bill, partner, requested=spec["has_any_arg"]):
        summary = apply_discount_to_invoice(
            bill,
            profile,
            spec,
            require_reason=True,
            enforce_auth=True,
            linked_partner=partner,
        )
        _sync_service_charge(bill, profile)
        if partner is not None:
            _sync_service_charge(partner, profile)
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    frappe.flags.ury_server_discount_write = True
    try:
        if partner is not None:
            with with_currency_precision(partner):
                partner.save(ignore_permissions=True)
        with with_currency_precision(bill):
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
    consumed = enforce_remove_discount_auth(bill, profile, row=row, approval=approval)
    removal_actor = (consumed.get("approver") if isinstance(consumed, dict) else None) or frappe.session.user
    bill.flags.discount_removal = frappe._dict(approver=removal_actor, action="remove")
    partner = (
        frappe.get_doc("POS Invoice", bill.custom_merged_pos_invoice, for_update=True)
        if bill.custom_merged_pos_invoice
        else None
    )
    with _split_discount_pricing(bill, partner):
        if row:
            target_row = next((r for r in (bill.get("items") or []) if str(r.get("name")) == str(row)), None)
            if not target_row:
                frappe.throw(_("Line item {0} is not on this check.").format(row), frappe.ValidationError)
            _set_row_field(target_row, "discount_percentage", 0)
            _set_row_field(target_row, "discount_amount", 0)
            if target_row.get("price_list_rate") is not None:
                _set_row_field(target_row, "rate", target_row.get("price_list_rate"))
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

        _sync_service_charge(bill, profile)
        if partner is not None:
            _sync_service_charge(partner, profile)
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    frappe.flags.ury_server_discount_write = True
    try:
        if partner is not None:
            with with_currency_precision(partner):
                partner.save(ignore_permissions=True)
        with with_currency_precision(bill):
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
        "status": ["in", ["Paid", "Partially Paid", "Partially Refunded"]],
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
    backed by a confirmed Paid/Partially Paid/Partially Refunded Phone Payment Request,
    are received. Phone rows are always re-checked and capped against live Payment Request
    paid amounts so cancelled, failed, expired, or partially/fully refunded requests never
    deadlock the check.
    """
    confirmed_prs = _confirmed_phone_payment_requests(invoice.get("name"), invoice.get("company"))
    live_pr_by_name = {pr.get("name"): pr for pr in confirmed_prs if pr.get("name")}
    live_paid_by_mode = {}
    for pr in confirmed_prs:
        pr_mode = pr.get("mode_of_payment")
        pr_paid = flt(pr.get("grand_total")) - flt(pr.get("outstanding_amount"))
        if pr_mode and pr_paid > 0:
            live_paid_by_mode[pr_mode] = live_paid_by_mode.get(pr_mode, 0.0) + pr_paid

    rows = []
    matched_pr_names = set()
    matched_unreferenced_phone_modes = {}

    for row in invoice.get("payments") or []:
        amt = flt(row.get("amount"))
        if amt <= 0 or cint(row.get("custom_tender_received")) != 1:
            continue
        mode = row.get("mode_of_payment")
        ref = (row.get("reference_no") or "").strip()
        if _is_phone_mode(row):
            if ref:
                pr = live_pr_by_name.get(ref)
                if not pr:
                    continue
                live_amt = round(flt(pr.get("grand_total")) - flt(pr.get("outstanding_amount")), 9)
                if live_amt <= 0:
                    continue
                matched_pr_names.add(ref)
                if mode:
                    live_paid_by_mode[mode] = max(0.0, live_paid_by_mode.get(mode, 0.0) - live_amt)
                copied = _copy_payment_row(row)
                copied["amount"] = live_amt
                rows.append(copied)
            else:
                avail = live_paid_by_mode.get(mode, 0.0) if mode else 0.0
                if not mode or avail <= 0:
                    continue
                used = min(amt, avail)
                live_paid_by_mode[mode] = max(0.0, avail - used)
                matched_unreferenced_phone_modes[mode] = matched_unreferenced_phone_modes.get(mode, 0.0) + used
                copied = _copy_payment_row(row)
                copied["amount"] = used
                rows.append(copied)
            continue

        rows.append(row)
        if ref:
            matched_pr_names.add(ref)

    for pr in confirmed_prs:
        pr_name = pr.get("name")
        if pr_name in matched_pr_names:
            continue
        paid_amt = flt(pr.get("grand_total")) - flt(pr.get("outstanding_amount"))
        mode = pr.get("mode_of_payment")
        if not mode or paid_amt <= 0:
            continue
        if matched_unreferenced_phone_modes.get(mode, 0.0) >= paid_amt:
            matched_unreferenced_phone_modes[mode] -= paid_amt
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
                "status": ["in", ["Paid", "Partially Paid", "Partially Refunded"]],
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
                "custom_tender_currency",
                "custom_tender_amount",
                "custom_exchange_rate",
                "custom_exchange_rate_date",
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
    for field in (
        "reference_no",
        "reference_date",
        "clearance_date",
        "custom_tender_currency",
        "custom_tender_amount",
        "custom_exchange_rate",
        "custom_exchange_rate_date",
    ):
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
                "custom_tender_currency": d.get("custom_tender_currency"),
                "custom_tender_amount": d.get("custom_tender_amount"),
                "custom_exchange_rate": d.get("custom_exchange_rate"),
                "custom_exchange_rate_date": d.get("custom_exchange_rate_date"),
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
            for field in (
                "reference_no",
                "reference_date",
                "clearance_date",
                "custom_tender_currency",
                "custom_tender_amount",
                "custom_exchange_rate",
                "custom_exchange_rate_date",
            ):
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
    room_charge_stay=None,
    room_charge_approval=None,
    room_charge_reason=None,
    on_account_approval=None,
    on_account_reason=None,
    offline_replay=0,
    offline_approval=None,
    offline_reason=None,
    tip=None,
    tip_server=None,
    halo_approval_token=None,
    halo_approval_reason=None,
    discount_percent=None,
    discount_amount=None,
    discount_reason=None,
    discount_note=None,
    coupon_code=None,
    line_discounts=None,
    approval=None,
    clear_discount=0,
    complimentary=0,
    fiscal_receipt_requested=1,
):
    submit_invoice = frappe.utils.cint(submit_invoice) if submit_invoice is not None else 1
    tip_omitted = tip is None
    tip = flt(tip)
    if tip < 0:
        frappe.throw(coded_message("TIP-NEGATIVE", _("A tip cannot be negative.")))
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
        complimentary=complimentary,
    )
    pos_prof = frappe.get_cached_doc("POS Profile", pos_profile)
    # G36: optional clock-in gate before settling a check.
    if cint(pos_prof.get("custom_require_clock_in_to_order")) == 1:
        from ury.ury_pos import timeclock

        timeclock.enforce_clock_in_for_profile(pos_prof, user=frappe.session.user)
    # G43: prevent cross-till cashier settlement while the operator holds an open shift on another outlet.
    try:
        from ury.ury_pos import outlet_context

        outlet_context.assert_no_conflicting_open_till(pos_profile, user=frappe.session.user)
    except frappe.ValidationError:
        raise
    except Exception:
        pass
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

    payments = prepare_settlement_payments(
        payments=payments,
        pos_profile=pos_prof,
        posting_date=invoice.get("posting_date"),
        invoice_name=invoice.name,
        is_return=bool(invoice.get("is_return")),
        invoice_doc=invoice,
    )

    existing_received_tenders = _received_payment_rows(invoice)

    # Room Charge: the cashier's room/folio text, for a hotel bridge that adds
    # POS Invoice.room_number; set before payments so its hooks can read it.
    incoming_room = str(room_number).strip() if room_number is not None else ""
    if incoming_room and check_is_comped_for_room_charge(invoice):
        frappe.throw(
            coded_message(
                "COMP-ROOM-CHARGE-FORBIDDEN",
                _("A complimentary check cannot carry a room charge."),
            ),
            frappe.ValidationError,
        )
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
    tx_mod = _tax_mod()
    tx_mod.apply_order_type_tax(invoice, pos_prof)

    # The cashier's tip (cash, or charged on a card or room-charge tender):
    # its Tips Payable line is on the bill before the totals the tenders are
    # allocated against. Owed to the bill's server unless another is named.
    if tip_omitted and _pa_is_valid_str(invoice.get("custom_ordering_session")):
        tip = flt(invoice.get("custom_charged_tip"))
        tip_server = invoice.get("custom_tip_server") or tip_server
    invoice.custom_charged_tip = tip
    invoice.custom_tip_server = (tip_server or invoice.waiter) if tip else None
    sync_charged_tip(invoice)

    _sync_service_charge(invoice, pos_prof)

    target = None
    if invoice.custom_merged_pos_invoice:
        target = frappe.get_doc("POS Invoice", invoice.custom_merged_pos_invoice, for_update=True)
        target.cashier = frappe.session.user
        _sync_service_charge(target, pos_prof)
        fx_proofs = getattr(getattr(invoice, "flags", None), "get", lambda *a: None)("pos_verified_fx_overrides")
        if fx_proofs:
            from ury.ury_pos.fx import mark_verified_fx_override
            for proof in fx_proofs:
                mark_verified_fx_override(target, proof.mode_of_payment, proof.from_currency, proof.to_currency, proof.rate)

    if discount_spec["has_any_arg"]:
        with _split_discount_pricing(invoice, target):
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

        alloc_base_prec = invoice.precision("paid_amount") if callable(getattr(invoice, "precision", None)) else 2
        if not isinstance(alloc_base_prec, int) or isinstance(alloc_base_prec, bool):
            alloc_base_prec = 2
        fx_alloc_prec = 4

        for d in alloc_rows:
            amt = float(d["amount"])
            mode = d["mode_of_payment"]
            extra_fields = {
                k: d[k]
                for k in (
                    "reference_no",
                    "reference_date",
                    "clearance_date",
                    "custom_tender_currency",
                    "custom_exchange_rate",
                    "custom_exchange_rate_date",
                )
                if d.get(k)
            }
            orig_total = amt if amt > 0 else 1.0
            orig_fx = flt(d.get("custom_tender_amount"))
            rem_fx = flt(orig_fx, fx_alloc_prec) if orig_fx > 0 else 0.0
            primary_row = None

            if doc_req > 0:
                give = min(amt, doc_req)
                row_payload = dict(mode_of_payment=mode, amount=give, **extra_fields)
                if orig_fx > 0:
                    give_fx = (
                        flt(rem_fx, fx_alloc_prec)
                        if abs(amt - give) <= 1e-9
                        else min(rem_fx, flt(orig_fx * (give / orig_total), fx_alloc_prec))
                    )
                    row_payload["custom_tender_amount"] = give_fx
                    rem_fx = flt(rem_fx - give_fx, fx_alloc_prec)
                primary_row = invoice.append("payments", row_payload)
                amt = flt(amt - give, alloc_base_prec)
                doc_req = flt(doc_req - give, alloc_base_prec)

            if target_req > 0 and amt > 0:
                give = min(amt, target_req)
                row_payload = dict(mode_of_payment=mode, amount=give, **extra_fields)
                if orig_fx > 0:
                    give_fx = (
                        flt(rem_fx, fx_alloc_prec)
                        if abs(amt - give) <= 1e-9
                        else min(rem_fx, flt(orig_fx * (give / orig_total), fx_alloc_prec))
                    )
                    row_payload["custom_tender_amount"] = give_fx
                    rem_fx = flt(rem_fx - give_fx, fx_alloc_prec)
                target.append("payments", row_payload)
                amt = flt(amt - give, alloc_base_prec)
                target_req = flt(target_req - give, alloc_base_prec)

            if amt > 0:
                # Preserve any remaining over-tender on the primary bill so
                # ury_pos_tender.before_validate applies the tender's over-tender
                # rule (Refuse / Post to Charged Tip) or cash change, keeping FX
                # fields and converted base-amount math exact across both branches.
                existing = primary_row
                if existing is None:
                    d_curr = extra_fields.get("custom_tender_currency")
                    d_rate = flt(extra_fields.get("custom_exchange_rate"), 9) if extra_fields.get("custom_exchange_rate") else None
                    existing = next(
                        (
                            row
                            for row in reversed(invoice.payments)
                            if row.mode_of_payment == mode
                            and (
                                orig_fx <= 0
                                or (
                                    row.get("custom_tender_currency") == d_curr
                                    and (d_rate is None or abs(flt(row.get("custom_exchange_rate"), 9) - d_rate) <= 1e-6)
                                )
                            )
                        ),
                        None,
                    )
                if existing is not None:
                    existing.amount = flt(flt(existing.amount) + amt, alloc_base_prec)
                    if orig_fx > 0:
                        existing.custom_tender_amount = flt(
                            flt(existing.get("custom_tender_amount")) + rem_fx,
                            fx_alloc_prec,
                        )
                        for k, v in extra_fields.items():
                            if not existing.get(k):
                                setattr(existing, k, v)
                        rem_fx = 0.0
                else:
                    row_payload = dict(mode_of_payment=mode, amount=amt, **extra_fields)
                    if orig_fx > 0:
                        row_payload["custom_tender_amount"] = flt(rem_fx, fx_alloc_prec)
                        rem_fx = 0.0
                    invoice.append("payments", row_payload)
                
        if not target.payments and payments:
            target.append("payments", dict(mode_of_payment=payments[0]["mode_of_payment"], amount=0))
        _validate_confirmed_phone_payments(invoice, invoice.payments)
        _validate_confirmed_phone_payments(target, target.payments)
        if _enforce_on_account(
            invoice,
            pos_prof,
            customer_disabled,
            target,
            on_account_approval=on_account_approval,
            on_account_reason=on_account_reason,
            consume_token=bool(submit_invoice),
        ):
            if target.company != invoice.company or target.pos_profile != invoice.pos_profile or target.docstatus != 0:
                frappe.throw(_('Merged credit bills must be open on the same till.'))
            _set_settlement_customer(target, customer)
            target.set_paid_amount()
            target.set_outstanding_amount()
        target.flags.ignore_payment_sync = True
        
        frappe.flags.in_bill_merge_sync = True
        prev_tender_flag = getattr(frappe.flags, "ury_server_tender_received", False)
        prev_override_flag = getattr(frappe.flags, "pos_server_override_fields", False)
        prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
        if target_existing:
            frappe.flags.ury_server_tender_received = True
        frappe.flags.pos_server_override_fields = True
        if discount_spec["has_any_arg"]:
            frappe.flags.ury_server_discount_write = True
        try:
            target.save(ignore_permissions=True)
        finally:
            frappe.flags.ury_server_tender_received = prev_tender_flag
            frappe.flags.pos_server_override_fields = prev_override_flag
            frappe.flags.ury_server_discount_write = prev_disc_flag
            frappe.flags.in_bill_merge_sync = False
            
        invoice.flags.ignore_payment_sync = True
    else:
        reconciled_payments = _reconcile_settlement_payments(invoice, payments)
        _validate_confirmed_phone_payments(invoice, reconciled_payments)
        invoice.set("payments", [])
        for row_dict in reconciled_payments:
            invoice.append("payments", row_dict)

    from ury.ury_pos import offline_tenders, room_charge

    offline_tenders.enforce_offline_tender_replay(
        invoice,
        pos_prof,
        invoice.payments,
        offline_replay=offline_replay,
        offline_approval=offline_approval,
        offline_reason=offline_reason,
        consume_token=bool(submit_invoice),
        _frappe=frappe,
    )
    rc_outcome = room_charge.enforce_room_charge_settlement(
        invoice,
        pos_prof,
        invoice.payments,
        room_number=incoming_room or None,
        room_charge_stay=room_charge_stay,
        room_charge_approval=room_charge_approval,
        room_charge_reason=room_charge_reason,
        consume_token=bool(submit_invoice),
        _frappe=frappe,
    )

    invoice.set_paid_amount()
    if not target:
        _enforce_on_account(
            invoice,
            pos_prof,
            customer_disabled,
            on_account_approval=on_account_approval,
            on_account_reason=on_account_reason,
            consume_token=bool(submit_invoice),
        )
    invoice.set_outstanding_amount()
    if not invoice.get("is_return"):
        due_total = flt(invoice.rounded_total or invoice.grand_total)
        paid_total = flt(invoice.paid_amount)
        if paid_total > due_total:
            if callable(getattr(invoice, "calculate_change_amount", None)):
                invoice.calculate_change_amount()
            if flt(invoice.get("change_amount")) <= 0:
                invoice.change_amount = flt(paid_total - due_total, invoice.precision("change_amount"))
            ensure_local_change_payment_row(invoice, pos_profile=pos_prof)
    if client_order_id:
        invoice.ury_client_order_id = client_order_id
        if round_id is not None:
            _record_round(invoice, round_id)
    # Consume optional Manager PIN token for Over-HALO tenders before invoice.save() triggers before_validate
    if halo_approval_token:
        from ury.ury_pos.till_controls import apply_halo_approval_token

        apply_halo_approval_token(invoice, halo_approval_token, reason=halo_approval_reason)

    # invoice.owner = owner
    prev_tender_flag = getattr(frappe.flags, "ury_server_tender_received", False)
    prev_override_flag = getattr(frappe.flags, "pos_server_override_fields", False)
    prev_disc_flag = getattr(frappe.flags, "ury_server_discount_write", False)
    if existing_received_tenders:
        frappe.flags.ury_server_tender_received = True
    frappe.flags.pos_server_override_fields = True
    if discount_spec["has_any_arg"]:
        frappe.flags.ury_server_discount_write = True
    try:
        with with_currency_precision(invoice):
            invoice.save()
    finally:
        frappe.flags.ury_server_tender_received = prev_tender_flag
        frappe.flags.pos_server_override_fields = prev_override_flag
        frappe.flags.ury_server_discount_write = prev_disc_flag

    if not submit_invoice:
        return {"name": invoice.name, "status": "Draft"}

    # Serialize bill settlement with till closing (FOR UPDATE on open shift) and refuse
    # offline replays arriving after the shift closed.
    from ury.ury_pos.till_controls import lock_open_shift_for_settlement, validate_offline_settlement_shift

    raw_offline_captured = invoice.get("ury_offline_captured_at")
    offline_captured_at = (
        raw_offline_captured
        if isinstance(raw_offline_captured, (str, date, datetime)) and raw_offline_captured
        else None
    )
    if client_order_id or offline_captured_at:
        validate_offline_settlement_shift(
            pos_profile,
            offline_captured_at=offline_captured_at,
            client_order_id=client_order_id,
        )
    else:
        lock_open_shift_for_settlement(pos_profile)

    tx_mod.validate_efris_readiness(invoice, throw=True)
    if target:
        # Saving the primary runs on_update, which saves a separate copy of the partner.
        target.reload()
        tx_mod.validate_efris_readiness(target, throw=True)
    prev_rc_flag = getattr(frappe.flags, "kamra_room_charge_override", None)
    if rc_outcome and rc_outcome.get("override_approver"):
        frappe.flags.kamra_room_charge_override = {
            "reservation": rc_outcome.get("reservation"),
            "room": rc_outcome.get("room_number"),
            "amount": rc_outcome.get("amount"),
            "currency": rc_outcome.get("currency"),
            "approver": rc_outcome.get("override_approver"),
            "reason": rc_outcome.get("override_reason"),
        }
    try:
        _submit_and_release_tables(invoice, pos_prof, customer_disabled, target,
                                   fiscal_receipt_requested=frappe.utils.cint(fiscal_receipt_requested))
    finally:
        frappe.flags.kamra_room_charge_override = prev_rc_flag
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


def _submit_and_release_tables(
    invoice,
    profile,
    customer_disabled,
    target=None,
    *,
    on_account_approval=None,
    on_account_reason=None,
    fiscal_receipt_requested=True,
):
    credit_partner = None
    if target:
        invoice.flags.ignore_payment_sync = True
    if (
        _enforce_on_account(
            invoice,
            profile,
            customer_disabled,
            target,
            on_account_approval=on_account_approval,
            on_account_reason=on_account_reason,
        )
        and target
    ):
        if (target.customer, target.company, target.pos_profile, target.docstatus) != (
            invoice.customer, invoice.company, invoice.pos_profile, 0
        ):
            frappe.throw(_('Merged credit bills must belong to the same account and till.'))
        credit_partner = target
    prev_override_flag = getattr(frappe.flags, "pos_server_override_fields", False)
    frappe.flags.pos_server_override_fields = True
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
        if fiscal_receipt_requested:
            from ury.ury_pos.api import _record_settlement_fiscal_intent
            _record_settlement_fiscal_intent(invoice)
    except frappe.PermissionError:
        raise
    except Exception as e:
        frappe.throw(f"Error while settling order: {e}")
    finally:
        frappe.flags.pos_server_override_fields = prev_override_flag

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
        from ury.ury.api.table_lifecycle import update_table_after_check

        if not update_table_after_check(
            _get_table_group(release_invoice.restaurant_table, release_invoice.custom_merged_tables),
            release_invoice.get("branch"), exclude_invoice=release_invoice.name,
        ):
            _free_tables_if_no_open_invoices(
                release_invoice.restaurant_table,
                release_invoice.custom_merged_tables,
            )


@frappe.whitelist()
def submit_order_invoice(
    invoice,
    on_account_approval=None,
    on_account_reason=None,
    room_charge_approval=None,
    room_charge_reason=None,
    offline_approval=None,
    offline_reason=None,
):
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
    from ury.ury_pos import offline_tenders, room_charge

    offline_tenders.enforce_offline_tender_replay(
        doc,
        profile,
        doc.payments,
        offline_approval=offline_approval,
        offline_reason=offline_reason,
        _frappe=frappe,
    )
    rc_outcome = room_charge.enforce_room_charge_settlement(
        doc,
        profile,
        doc.payments,
        room_charge_approval=room_charge_approval,
        room_charge_reason=room_charge_reason,
        _frappe=frappe,
    )
    target = frappe.get_doc('POS Invoice', doc.custom_merged_pos_invoice, for_update=True) if doc.custom_merged_pos_invoice else None
    tx_mod = _tax_mod()

    tx_mod.validate_efris_readiness(doc, throw=True)
    if target:
        tx_mod.validate_efris_readiness(target, throw=True)
        doc.flags.ignore_payment_sync = True
    prev_rc_flag = getattr(frappe.flags, "kamra_room_charge_override", None)
    if rc_outcome and rc_outcome.get("override_approver"):
        frappe.flags.kamra_room_charge_override = {
            "reservation": rc_outcome.get("reservation"),
            "room": rc_outcome.get("room_number"),
            "amount": rc_outcome.get("amount"),
            "currency": rc_outcome.get("currency"),
            "approver": rc_outcome.get("override_approver"),
            "reason": rc_outcome.get("override_reason"),
        }
    try:
        _submit_and_release_tables(
            doc,
            profile,
            customer_disabled,
            target,
            on_account_approval=on_account_approval,
            on_account_reason=on_account_reason,
        )
    finally:
        frappe.flags.kamra_room_charge_override = prev_rc_flag
    return {"name": doc.name, "status": "Submitted"}


@frappe.whitelist()
def change_order_type(
    invoice,
    order_type,
    table=None,
    room=None,
    no_of_pax=None,
    approval_token=None,
    reason=None,
):
    """Switch order_type on a draft POS Invoice, re-evaluating Tax Category, Tax Rule,
    taxes_and_charges, and non-exempt item taxes while logging the transition."""
    order_type = (order_type or "").strip()
    valid_order_types = {"Dine In", "Take Away", "Delivery", "Phone In", "Aggregators"}
    if order_type not in valid_order_types:
        frappe.throw(_("Invalid order type: {0}.").format(order_type), frappe.ValidationError)

    inv = frappe.get_doc("POS Invoice", invoice)
    if not frappe.has_permission("POS Invoice", "write", doc=inv):
        frappe.throw(_("Not permitted to modify this order."), frappe.PermissionError)
    if int(inv.get("docstatus") or 0) != 0:
        frappe.throw(_("Only draft orders can change order type."), frappe.ValidationError)

    user_roles = frappe.get_roles()
    is_super = frappe.session.user == "Administrator" or "System Manager" in user_roles
    user_branch = None
    if not is_super:
        user_branch = getBranch()
        if inv.get("branch") and user_branch and inv.branch != user_branch:
            frappe.throw(_("Not permitted outside your active branch."), frappe.PermissionError)

    flags = _enforce_order_access(inv, pos_profile_name=inv.get("pos_profile"), require_modify=True)
    pos_prof = frappe.get_doc("POS Profile", inv.pos_profile)
    if not (
        cint(pos_prof.get("custom_edit_order_type")) == 1
        or flags.get("has_elevated_access")
        or is_super
    ):
        frappe.throw(
            _("Changing order type is disabled for this POS Profile."),
            frappe.PermissionError,
        )

    if order_type == "Dine In" and _has_role(user_roles, pos_prof.get("role_restricted_for_table_order")):
        frappe.throw(
            _("You are not permitted to take Dine In table orders."),
            frappe.PermissionError,
        )

    old_order_type = inv.get("order_type") or ""
    old_tax_category = inv.get("tax_category") or ""
    old_template = inv.get("taxes_and_charges") or ""
    old_table = inv.get("restaurant_table")
    old_merged = inv.get("custom_merged_tables")
    old_price_list = (inv.get("selling_price_list") or "").strip()
    old_period = (inv.get("custom_serving_period") or "").strip()

    # Validate any requested table for branch ownership, room access, occupancy, and reservation lock
    table_info = None
    if table:
        raw_table_info = frappe.db.get_value(
            "URY Table",
            table,
            ["name", "branch", "restaurant_room", "occupied"],
            as_dict=True,
        )
        if not isinstance(raw_table_info, dict) or not raw_table_info.get("branch"):
            frappe.throw(_("Table not found."), frappe.PermissionError)
        table_info = raw_table_info
        table_branch = table_info.get("branch")
        table_room = table_info.get("restaurant_room")

        if inv.get("branch") and table_branch != inv.get("branch"):
            frappe.throw(
                _("Not permitted to assign a table outside this order's branch."),
                frappe.PermissionError,
            )
        if not is_super and user_branch and table_branch != user_branch:
            frappe.throw(
                _("Not permitted to take orders for a table outside your active branch."),
                frappe.PermissionError,
            )

        if table_room and not (
            flags.get("has_elevated_access") or flags.get("has_billing_access") or is_super
        ):
            assigned_rooms = {row.get("name") for row in getRoom()}
            if None not in assigned_rooms and table_room not in assigned_rooms:
                frappe.throw(
                    _("Not permitted to take orders for a table outside your assigned room."),
                    frappe.PermissionError,
                )

    seating_table = table or old_table
    if order_type == "Dine In" and seating_table and (seating_table != old_table or old_order_type != "Dine In"):
        from ury.ury.api.table_lifecycle import assert_table_seatable

        assert_table_seatable(seating_table, table_info.get("branch") if table_info else inv.get("branch"))

    if table and table != old_table:
        if cint(table_info.get("occupied")) == 1 or frappe.db.exists(
            "POS Invoice",
            {"restaurant_table": table, "docstatus": 0, "name": ["!=", inv.name]},
        ):
            frappe.throw(
                _("Table {0} is already occupied.").format(table),
                frappe.ValidationError,
            )

        if order_type == "Dine In":
            from ury.ury.api.table_reservation import check_table_reservation, parse_to_datetime

            active_res = check_table_reservation(table)
            if (
                active_res
                and active_res.get("is_lock_window_active")
                and active_res.get("status") == "Confirmed"
            ):
                res_time_raw = active_res.get("reserved_at")
                formatted_res_time = ""
                if res_time_raw:
                    try:
                        res_dt = parse_to_datetime(res_time_raw)
                        formatted_res_time = res_dt.strftime("%I:%M %p").lstrip("0")
                    except Exception:
                        formatted_res_time = str(res_time_raw)
                frappe.throw(
                    _("Table {0} is reserved for {1}. Please choose another table.").format(
                        table, formatted_res_time
                    ),
                    frappe.ValidationError,
                )

    inv.no_of_pax = _validate_dine_in_pax(
        no_of_pax if no_of_pax is not None else (inv.get("no_of_pax") or 1), order_type, pos_prof
    )
    inv.order_type = order_type
    if order_type == "Dine In":
        if table:
            inv.restaurant_table = table
        if room is not None:
            inv.custom_restaurant_room = room
        elif table_info is not None:
            inv.custom_restaurant_room = table_info.get("restaurant_room")
    else:
        inv.restaurant_table = table or None
        inv.custom_merged_tables = None
        if room is not None:
            inv.custom_restaurant_room = room
        elif table_info is not None:
            inv.custom_restaurant_room = table_info.get("restaurant_room")
        else:
            inv.custom_restaurant_room = None

    # Re-resolve selling_price_list and item pricing for the new order type while preserving
    # open-check Serving Period locks when the stamped period still governs the new order type.
    dp_mod = _dayparts_mod()
    resolved_menu = dp_mod.resolve_menu_and_price_list(
        inv.get("branch"),
        restaurant=inv.get("restaurant"),
        table=inv.get("restaurant_table"),
        room=inv.get("custom_restaurant_room"),
        order_type=order_type,
        customer=inv.get("customer"),
        at=inv.get("ury_offline_captured_at"),
        cashier=True,
    )
    keep_period_lock = False
    if (
        old_period
        and old_price_list
        and order_type != "Aggregators"
        and resolved_menu.get("source") != "room"
        and dp_mod.serving_period_applies_to_order_type(inv.get("restaurant"), old_period, order_type)
    ):
        if old_price_list in dp_mod.allowed_price_lists_for_period(inv.get("restaurant"), old_period):
            keep_period_lock = True

    if keep_period_lock:
        new_price_list = old_price_list
    else:
        new_price_list = resolved_menu.get("price_list")
        if order_type == "Aggregators" and not new_price_list:
            frappe.throw(
                _("Price list for customer {0} in branch {1} not found in Aggregator Settings.").format(
                    inv.get("customer"), inv.get("branch")
                ),
                frappe.ValidationError,
            )
        if old_period and not dp_mod.serving_period_applies_to_order_type(
            inv.get("restaurant"), old_period, order_type
        ):
            inv.custom_serving_period = (resolved_menu.get("serving_period") or {}).get("period_name") or None

    if getattr(inv, "flags", None) is not None:
        inv.flags.ury_server_order_type_change = True
        inv.flags.ury_trusted_server_pricing = True

    if new_price_list and new_price_list != old_price_list:
        inv.selling_price_list = new_price_list
        if getattr(inv, "flags", None) is not None:
            inv.flags.ury_authoritative_price_list = new_price_list
        pricing_dt = invoice_pricing_date(inv)
        try:
            release_field, released = item_price_release_states()
        except Exception:
            release_field, released = (None, None)
        new_menu = resolved_menu.get("menu")
        for row in inv.get("items") or []:
            if cint(row.get("is_disposable")) == 1:
                continue
            if row.get("custom_line_role") == "Combo Upcharge" and (row.get("custom_parent_line_key") or "").strip():
                # The linked modifier is priced by the modifier validator.
                continue
            if (
                cint(row.get("custom_price_overridden")) == 1
                or cint(row.get("custom_is_open_item")) == 1
                or cint(row.get("custom_is_complimentary")) == 1
            ):
                continue
            code = row.get("item_code")
            if not code:
                continue
            new_rate = None
            try:
                ip = price_valid_on(
                    code, new_price_list, pricing_dt, release_field, released, customer=inv.get("customer")
                )
                if ip and ip.get("price_list_rate") is not None:
                    new_rate = flt(ip.get("price_list_rate"))
            except Exception:
                new_rate = None
            if new_rate is None and new_menu:
                menu_rate = frappe.db.get_value(
                    "URY Menu Item", {"parent": new_menu, "item": code}, "rate"
                )
                if menu_rate is not None:
                    new_rate = flt(menu_rate)
            if new_rate is not None:
                row.price_list_rate = new_rate
                row.base_price_list_rate = new_rate
                row.rate = new_rate
                row.pricing_rules = None
                row.discount_percentage = 0
                row.discount_amount = 0
    elif old_price_list and getattr(inv, "flags", None) is not None:
        inv.flags.ury_authoritative_price_list = old_price_list

    customer_doc = (
        frappe.get_doc("Customer", inv.customer) if inv.get("customer") else None
    )

    tx_mod = _tax_mod()

    tax_result = tx_mod.apply_order_type_tax(
        inv,
        pos_prof,
        customer_doc=customer_doc,
        reset_non_exempt_item_taxes=True,
    )
    inv.set_missing_values(for_validate=True)
    inv.calculate_taxes_and_totals()

    reason_suffix = f" Reason: {reason.strip()}" if (reason and str(reason).strip()) else ""
    inv.add_comment(
        "Comment",
        _(
            "Order type changed from {0} to {1}; tax category {2} -> {3}; tax template {4} -> {5}.{6}"
        ).format(
            old_order_type or "None",
            order_type,
            old_tax_category or "None",
            tax_result.get("tax_category") or "None",
            old_template or "None",
            tax_result.get("taxes_and_charges") or "None",
            reason_suffix,
        ),
    )
    prev_priv_verified = getattr(getattr(inv, "flags", None), "ury_privileged_action_verified", False)
    if getattr(inv, "flags", None) is not None:
        inv.flags.ury_privileged_action_verified = True
    try:
        inv.save()
    finally:
        if getattr(inv, "flags", None) is not None:
            inv.flags.ury_privileged_action_verified = prev_priv_verified
            inv.flags.ury_server_order_type_change = False
            inv.flags.ury_trusted_server_pricing = False

    if inv.get("restaurant_table") and inv.get("restaurant_table") != old_table:
        frappe.db.set_value(
            "URY Table",
            inv.restaurant_table,
            {"occupied": 1, "latest_invoice_time": inv.get("creation") or now_datetime()},
        )
    if old_table and inv.get("restaurant_table") != old_table:
        _free_tables_if_no_open_invoices(old_table, old_merged)

    return inv.as_dict()
        
        

# Cancel KOT Doc Creation
def cancel_kot(invoice_id, reason=None, void_reason_code=None):

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

    # Process items for a canceled KOT, linking the void/waste reason onto the ticket
    import inspect

    cancel_kwargs = {}
    if void_reason_code and "void_reason_code" in inspect.signature(process_items_for_cancel_kot).parameters:
        cancel_kwargs["void_reason_code"] = void_reason_code
    created_cancel_kots = process_items_for_cancel_kot(
        invoice_id,
        pos_invoice.customer,
        restaurant_table,
        items,
        reason or "",
        pos_profile_id,
        cancel_kot_naming_series,
        "Cancelled",
        items,
        **cancel_kwargs,
    )

    return created_cancel_kots


def change_table_in_kot(invoice, new_table, branch):
    from ury.ury.api.kitchen_ticket_history import owned_kots

    kot_list = owned_kots(frappe, invoice, active=True, filters={
        "order_status": ["!=", "Served"],
    }, fields=["name", "production", "branch"])

    # Update each KOT's restaurant_table and send a real-time update
    for kot in kot_list:
        frappe.db.set_value("URY KOT", kot.name, "restaurant_table", new_table)
        kot_channel = "{}_{}_{}".format("kot_update", kot.branch or branch, kot.production)
        frappe.publish_realtime(kot_channel)
