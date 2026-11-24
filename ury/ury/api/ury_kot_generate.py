import json
from collections import defaultdict

import frappe
from frappe.utils import cint, flt, get_datetime

from ury.ury.api.ury_bundle_resolver import parse_bundle_overrides
from ury.ury.api.ury_kot_routing import resolve_production_units
from ury.ury.api.ury_production_context import resolve_production_context
# Reuse the reservation service's stable per-line key derivation instead of
# inventing a second concept. `_line_ref` prefers an explicit client-supplied
# identifier (reservation_line_key/name/etc.); `_line_context` falls back to
# comment/course/etc.; `_line_key` combines either with an occurrence counter
# so two lines of the same item_code never collapse into one key. See
# sa-architecture-closure: cancellation/KOT-delta matching previously used
# bare item_code, so the same item on two lines (different comments,
# courses, or a plain duplicate) could have the wrong line cancelled.
from ury.ury.api.ury_order_reservation_service import _line_context, _line_key, _line_ref


def _modifier_summary_for_kot_item(item):
    if item.get("modifier_summary"):
        return item.get("modifier_summary")
    if cint(item.get("custom_is_modifier")) == 1:
        prefix = (item.get("custom_modifier_prefix") or "").strip()
        extra = (item.get("comments") or "").strip()
        parts = [p for p in (prefix, extra) if p]
        return " · ".join(parts)
    raw = item.get("custom_modifiers_json")
    if raw:
        try:
            mods = json.loads(raw) if isinstance(raw, str) else list(raw)
            parts = []
            for m in mods:
                if not isinstance(m, dict):
                    continue
                prefix = (m.get("prefix") or "").strip()
                label = (m.get("label") or m.get("option_code") or "").strip()
                text = f"{prefix} {label}".strip() if prefix else label
                if text:
                    parts.append(text)
            if parts:
                return " · ".join(parts)
        except (TypeError, ValueError):
            pass
    return (item.get("comments") or "").strip()


# Load JSON data or return as is if it's already a Python dictionary
def load_json(data):
    if isinstance(data, str):
        return json.loads(data)
    return data


# Create a list of order items from a list of input items
def create_order_items(items):
    order_items = []
    for item in items:
        order_item = {
            "item_code": item.get("item", item.get("item_code")),
            "qty": item["qty"],
            "item_name": item["item_name"],
            "comments": item.get("comment", item.get("comments", "")),
            # Preserve the stable per-line key when the caller has already
            # computed one (see _line_keyed_items below) so it survives the
            # trip through KOT item creation instead of being dropped.
            "reservation_line_key": item.get("reservation_line_key"),
            "custom_parent_line_key": item.get("custom_parent_line_key") or None,
            "custom_line_role": item.get("custom_line_role") or "Standard",
            "custom_is_modifier": cint(item.get("custom_is_modifier")),
            "custom_modifier_prefix": item.get("custom_modifier_prefix") or "",
            "custom_modifiers_json": item.get("custom_modifiers_json") or "",
            "custom_bundle_overrides_json": item.get("custom_bundle_overrides_json") or "",
            "custom_inherit_parent_routing": cint(item.get("custom_inherit_parent_routing", 1)),
            "modifier_summary": item.get("modifier_summary") or "",
        }
        order_items.append(order_item)
    return order_items


def _line_keyed_items(items):
    """Group raw order-item dicts (as sent by the POS client, or the
    past_item snapshot built by sync_order) by a stable per-line key instead
    of collapsing same-item lines by item_code.

    Returns an ordered dict: line_key -> {item_code, item_name, qty
    (aggregated), comments, reservation_line_key, ...}. Ordering follows first
    appearance so callers can diff two calls (previous vs current) made on
    lists that are otherwise in the same relative order.
    """
    seen = defaultdict(int)
    result = {}
    for row in items or []:
        item_code = row.get("item") or row.get("item_code")
        if not item_code:
            continue
        base_context = _line_ref(row) or json.dumps(
            _line_context(row), sort_keys=True, default=str
        )
        occurrence_base = "{0}:{1}".format(item_code, base_context)
        seen[occurrence_base] += 1
        key = _line_key(item_code, row, seen[occurrence_base])
        if key not in result:
            result[key] = {
                "reservation_line_key": key,
                "raw_line_key": row.get("reservation_line_key") or None,
                "item_code": item_code,
                "item_name": row.get("item_name"),
                "qty": 0,
                "comments": row.get("comment", row.get("comments", "")),
                "custom_parent_line_key": row.get("custom_parent_line_key") or None,
                "custom_line_role": row.get("custom_line_role") or "Standard",
                "custom_is_modifier": cint(row.get("custom_is_modifier")),
                "custom_modifier_prefix": row.get("custom_modifier_prefix") or "",
                "custom_modifiers_json": row.get("custom_modifiers_json") or "",
                "custom_bundle_overrides_json": row.get("custom_bundle_overrides_json") or "",
                "custom_inherit_parent_routing": cint(row.get("custom_inherit_parent_routing", 1)),
            }
        result[key]["qty"] = flt(result[key]["qty"]) + flt(row.get("qty"))
    return result


# Create a KOT (Kitchen Order Ticket) document
def create_kot_doc(
    invoice_id,
    customer,
    restaurant_table,
    items,
    kot_type,
    comments,
    pos_profile_id,
    kot_naming_series,
    production,
    validation_dedup_key=None,
    offline_printed=0,
):
    pos_invoice = frappe.get_doc("POS Invoice", invoice_id)
    order_number = pos_invoice.custom_ury_order_number
    is_aggregator = 0
    if pos_invoice.order_type == "Aggregators":
        is_aggregator = 1
    kot_doc = frappe.get_doc(
        {
            "doctype": "URY KOT",
            "invoice": invoice_id,
            "restaurant_table": restaurant_table,
            "custom_merged_tables": pos_invoice.get("custom_merged_tables"),
            "customer_name": customer,
            "custom_guest_label": pos_invoice.get("custom_guest_label") or None,
            "pos_profile": pos_profile_id,
            "comments": comments,
            "type": kot_type,
            "naming_series": kot_naming_series,
            "production": production,
            "aggregator_id":pos_invoice.custom_aggregator_id,
            "is_aggregator":is_aggregator,
            "order_no":order_number
        }
    )
    if validation_dedup_key:
        # Populates the same "<invoice>::<production>" key the scheduler's
        # create_kot() fallback (ury_kot_validation.py) writes for its own
        # first-KOT-for-this-invoice+production case. Both writers now
        # compete on the same unique index, so a real check-then-insert race
        # between the live path and the scheduler tick is caught at the DB
        # level instead of silently duplicating. Only the first ("New
        # Order") KOT for an invoice+production carries this key -- later,
        # legitimate KOTs for the same invoice+production (Order Modified,
        # etc.) must not collide with it, so callers only pass this for that
        # first-KOT case.
        kot_doc.validation_dedup_key = validation_dedup_key
    # Printed at the station while the link was down: the kitchen already has it.
    if cint(offline_printed) == 1:
        kot_doc.ury_offline_printed = 1
    if restaurant_table:
        room = frappe.db.get_value("URY Table", restaurant_table, "restaurant_room")
        restaurant = frappe.db.get_value("URY Table", restaurant_table, "restaurant")
        menu = frappe.db.get_value("Menu for Room", {"room": room,"parent":restaurant}, "menu")

    else:
        # No-table orders (Takeaway/Delivery/Aggregators/QR-pickup) have no
        # session-branch-independent context of their own -- the invoice
        # being ticketed is the only reliable source of branch. Using
        # getBranch() here derives the menu from the ACTING USER's session
        # branch instead of the invoice's own branch, which is wrong
        # whenever those differ (e.g. a billing/back-office user operating
        # across branches). Same bug class as PR #373 bug #1
        # (_resolve_or_create_pos_invoice not setting invoice.branch).
        menu = frappe.db.get_value("URY Restaurant", {"branch": pos_invoice.branch}, "active_menu")

    for item in items:
        course = frappe.db.get_value("URY Menu Item", {"item": item["item_code"],"parent":menu}, "course")
        kot_doc.append(
            "kot_items",
            {
                "item": item["item_code"],
                "item_name": item["item_name"],
                "quantity": item["qty"],
                "comments": item["comments"],
                "course": course,
                "reservation_line_key": item.get("reservation_line_key"),
                "parent_line_key": item.get("custom_parent_line_key") or None,
                "is_modifier": cint(item.get("custom_is_modifier")),
                "modifier_summary": _modifier_summary_for_kot_item(item),
            },
        )
    kot_doc.insert()
    kot_doc.submit()

    # N4: additive, non-blocking Work Order creation for MADE_TO_ORDER
    # items on this KOT. Never allowed to block/fail KOT creation itself --
    # `create_work_orders_for_kot` already isolates and swallows per-item
    # failures internally, but this call is wrapped defensively as well so a
    # completely unexpected error in that module can never propagate into
    # the live KOT-creation path.
    try:
        from ury.ury.api.ury_mto_work_order_service import create_work_orders_for_kot

        create_work_orders_for_kot(kot_doc.name)
    except Exception:
        frappe.log_error(
            title="ury_kot_generate.create_kot_doc: MTO Work Order creation failed",
            message=frappe.get_traceback(),
        )

    if cint(offline_printed) == 1:
        from ury.ury.api.ury_fulfilment_posting_service import _service_mutation
        from ury.ury.api.ury_kot_display import record_kot_served

        # A station-fired ticket takes the normal READY -> Served path. The cashier
        # holds no kitchen role, so the system records it; the till opt-in is the gate.
        with _service_mutation():
            record_kot_served(kot_doc.name, get_datetime(kot_doc.creation))

    return kot_doc.name

# Function to get all production item groups for a given branch
def get_all_production_item_groups(branch):
    productions = frappe.db.get_all(
        "URY Production Unit", filters={"branch": branch}, fields=["name"]
    )
    if productions:
        all_production_item_groups = set()
        for production in productions:
            productionItemGroupslist = frappe.get_all(
                "URY Production Item Groups",
                fields=["item_group"],
                filters={
                    "parent": production.name,
                    "parenttype": "URY Production Unit",
                },
                order_by="idx",
            )
            productionItemGroups = [
                item_group.item_group for item_group in productionItemGroupslist
            ]
            all_production_item_groups.update(productionItemGroups)
        return all_production_item_groups


def _extract_raw_line_key(item):
    raw = item.get("raw_line_key") or item.get("reservation_line_key") or ""
    if raw.startswith("ref:") and ":" in raw[4:]:
        return raw.split(":", 2)[2]
    return raw


def _resolve_units_for_item_code(item_code, pos_profile, pos_invoice):
    production_config = resolve_production_context(
        item_code, pos_profile.branch, company=pos_invoice.company
    )
    production_policy = production_config.production_policy if production_config else None
    return resolve_production_units(
        item_code=item_code,
        company=pos_invoice.company,
        branch=pos_profile.branch,
        production_policy=production_policy,
    )


def _route_items_with_modifiers_and_combos(kot_items, pos_profile, pos_invoice, *, is_cancel=False):
    """Route items to production units applying:
    - Simphony Opt 22: modifiers with `custom_inherit_parent_routing=1` inherit their parent's
      station(s) and appear immediately beneath their parent row.
    - G4 Combos: expand `custom_bundle_overrides_json` onto KOTs, routing `inherit_parent_routing=0`
      choices (e.g. Beverage -> Bar) to their own station, and skip billing-only `Combo Upcharge` rows.
    """
    parent_units_by_key = {}
    station_buckets = {}  # station -> list of (parent_order_key, item_dict)

    def _add_to_station(station, item_dict, attach_under_parent_key=None):
        bucket = station_buckets.setdefault(station, [])
        if attach_under_parent_key:
            # Insert right after the parent (and any previously attached children of that parent)
            insert_idx = None
            for idx, (owner_key, _row) in enumerate(bucket):
                if owner_key == attach_under_parent_key:
                    insert_idx = idx + 1
            if insert_idx is not None:
                bucket.insert(insert_idx, (attach_under_parent_key, item_dict))
                return
        owner = attach_under_parent_key or _extract_raw_line_key(item_dict) or item_dict.get("reservation_line_key") or ""
        bucket.append((owner, item_dict))

    # Pre-index saved invoice items in case a later round adds/cancels a modifier whose parent was sent earlier
    saved_parent_item_by_key = {}
    for inv_row in pos_invoice.get("items") or []:
        if not cint(inv_row.get("custom_is_modifier")):
            k = inv_row.get("reservation_line_key") or inv_row.get("name")
            if k:
                saved_parent_item_by_key[k] = inv_row.get("item_code")

    child_items = []
    for item in kot_items:
        if cint(item.get("custom_is_modifier")) == 1:
            child_items.append(item)
            continue

        item_code = item["item_code"]
        resolved_units = _resolve_units_for_item_code(item_code, pos_profile, pos_invoice)
        raw_key = _extract_raw_line_key(item)
        if raw_key:
            parent_units_by_key[raw_key] = list(resolved_units)
        if item.get("reservation_line_key"):
            parent_units_by_key[item["reservation_line_key"]] = list(resolved_units)

        for production_unit in resolved_units:
            _add_to_station(production_unit, item)

        # G4 Combo expansion: route chosen components (inheriting parent station or routing to own station e.g. Bar)
        raw_overrides = item.get("custom_bundle_overrides_json")
        if raw_overrides and not is_cancel:
            overrides = parse_bundle_overrides(raw_overrides)
            for ov in overrides:
                chosen_code = ov["chosen_item"]
                comp_name = (
                    frappe.db.get_value("Item", chosen_code, "item_name")
                    or ov.get("label")
                    or chosen_code
                )
                comp_qty = flt(ov.get("qty") or 1) * flt(item.get("qty") or 1)
                comp_row = {
                    "item_code": chosen_code,
                    "item_name": comp_name,
                    "qty": comp_qty,
                    "comments": ov.get("group") or "",
                    "reservation_line_key": f"{raw_key}::combo::{chosen_code}" if raw_key else None,
                    "custom_parent_line_key": raw_key or item.get("reservation_line_key"),
                    "custom_line_role": "Combo Component",
                    "custom_is_modifier": 1,
                    "custom_modifier_prefix": "",
                    "custom_inherit_parent_routing": cint(ov.get("inherit_parent_routing", 1)),
                    "modifier_summary": ov.get("label") or comp_name,
                }
                if cint(ov.get("inherit_parent_routing", 1)) == 1 and resolved_units:
                    comp_units = list(resolved_units)
                else:
                    comp_units = _resolve_units_for_item_code(chosen_code, pos_profile, pos_invoice) or list(resolved_units)
                for unit in comp_units:
                    _add_to_station(unit, comp_row, attach_under_parent_key=raw_key)

    # Route linked child modifiers (Simphony Opt 22)
    for child in child_items:
        # Non-stock Combo Upcharge fee SKUs are billing rows only; physical components routed above.
        if child.get("custom_line_role") == "Combo Upcharge":
            continue

        parent_key = child.get("custom_parent_line_key") or ""
        inherit = cint(child.get("custom_inherit_parent_routing", 1)) == 1
        target_units = None

        if inherit and parent_key:
            if parent_key in parent_units_by_key:
                target_units = parent_units_by_key[parent_key]
            elif parent_key in saved_parent_item_by_key:
                target_units = _resolve_units_for_item_code(
                    saved_parent_item_by_key[parent_key], pos_profile, pos_invoice
                )
                parent_units_by_key[parent_key] = list(target_units)

        if target_units is None:
            target_units = _resolve_units_for_item_code(child["item_code"], pos_profile, pos_invoice)

        for unit in target_units:
            _add_to_station(unit, child, attach_under_parent_key=parent_key or None)

    return {
        station: [row for _owner, row in entries]
        for station, entries in station_buckets.items()
    }


# Process items to create KOT documents
def process_items_for_kot(
    invoice_id,
    customer,
    restaurant_table,
    items,
    comments,
    pos_profile_id,
    kot_naming_series,
    kot_type,
    offline_printed=0,
):
    kot_items = create_order_items(items)
    pos_profile = frappe.get_doc("POS Profile", pos_profile_id)
    pos_invoice = frappe.get_doc("POS Invoice", invoice_id)

    created_kot_names = []

    # Verify production units exist for the branch
    productions = frappe.db.get_all(
        "URY Production Unit", filters={"branch": pos_profile.branch}, fields=["name"]
    )
    if not productions:
        frappe.throw(
            "Create Restaurant Production unit against POS Profile: %s " % pos_profile.name
        )

    # Build a map of production unit -> items using Opt 22 parent inheritance + combo split
    production_items_map = _route_items_with_modifiers_and_combos(
        kot_items, pos_profile, pos_invoice, is_cancel=False
    )

    # Print warning if any non-fee item was not routed (legacy behavior)
    all_routed_items = set()
    for items_list in production_items_map.values():
        for item in items_list:
            all_routed_items.add(item["item_code"])
    for item in kot_items:
        if item.get("custom_line_role") == "Combo Upcharge":
            continue
        if item["item_code"] not in all_routed_items:
            item_group = frappe.db.get_value("Item", item["item_code"], "item_group")
            frappe.msgprint(
                f"Item group '{item_group}' for item '{item['item_code']}' is not in any production."
            )

    # Create one KOT per production unit
    for production_unit, production_items in production_items_map.items():
        invoice_exist = frappe.db.exists(
            "URY KOT",
            {
                "invoice": invoice_id,
                "docstatus": 1,
                "production": production_unit,
            },
        )
        # This is the same "no KOT exists yet for this invoice+production" case
        # the scheduler's create_kot() fallback guards against with validation_dedup_key --
        # only tag the first ("New Order") KOT here, never the subsequent legitimate ones
        # (Order Modified etc.), so later KOTs for the same invoice+production are not
        # rejected by the unique index.
        validation_dedup_key = None
        current_kot_type = kot_type
        if invoice_exist:
            current_kot_type = "Order Modified"
        else:
            validation_dedup_key = "{0}::{1}".format(invoice_id, production_unit)

        kot_name = create_kot_doc(
            invoice_id,
            customer,
            restaurant_table,
            production_items,
            current_kot_type,
            comments,
            pos_profile_id,
            kot_naming_series,
            production_unit,
            validation_dedup_key=validation_dedup_key,
            offline_printed=offline_printed,
        )
        created_kot_names.append(kot_name)

    return created_kot_names


# Process items to create a cancel KOT document
def process_items_for_cancel_kot(
    invoice_id,
    customer,
    restaurant_table,
    items,
    comments,
    pos_profile_id,
    cancel_kot_naming_series,
    kot_type,
    invoiceItems,
):

    kot_items = create_order_items(items)
    pos_profile = frappe.get_doc("POS Profile", pos_profile_id)
    pos_invoice = frappe.get_doc("POS Invoice", invoice_id)

    created_kot_names = []

    # Build a map of production unit -> items respecting Opt 22 parent inheritance
    production_items_map = _route_items_with_modifiers_and_combos(
        kot_items, pos_profile, pos_invoice, is_cancel=True
    )

    # Create one cancel KOT per production unit
    for production_unit, production_items in production_items_map.items():
        kot_name = create_cancel_kot_doc(
            invoice_id,
            restaurant_table,
            production_items,
            kot_type,
            customer,
            comments,
            pos_profile_id,
            cancel_kot_naming_series,
            invoiceItems,
            production_unit,
        )
        created_kot_names.append(kot_name)

    return created_kot_names


# Create a cancel KOT document
def create_cancel_kot_doc(
    invoice_id,
    restaurant_table,
    cancel_items,
    kot_type,
    customer,
    comments,
    pos_profile_id,
    cancel_kot_naming_series,
    invoiceItems,
    production,
):
    pos_invoice = frappe.get_doc("POS Invoice", invoice_id)
    order_number = pos_invoice.custom_ury_order_number  
    is_aggregator = 0
    if pos_invoice.order_type == "Aggregators":
        is_aggregator = 1
    history = frappe.db.get_list(
        "URY KOT", filters={"invoice": invoice_id, "production": production, "docstatus": 1,
                            "type": ("in", ("New Order", "Order Modified", "Duplicate", "Cancelled", "Partially cancelled"))},
        fields=["name"], order_by="creation asc, name asc", limit_page_length=0,
    )
    releases = []

    def consume(item, key, note, qty, originals=None):
        matched = []
        for release in reversed(releases):
            name, row, remaining = release
            if originals is not None and name not in originals:
                continue
            if row.item != item or (row.get("comments") or "") != (note or ""):
                continue
            if key and row.get("reservation_line_key") and key != row.get("reservation_line_key"):
                continue
            take = min(qty, remaining)
            if take > 0:
                matched.append(name)
                release[2] -= take
                qty -= take
            if qty <= 0:
                break
        return matched

    for entry in history:
        ticket = frappe.get_doc("URY KOT", entry.name)
        if ticket.get("type") in ("Cancelled", "Partially cancelled"):
            originals = {name.strip() for name in (ticket.get("original_kot") or "").split(",")}
            for row in ticket.kot_items:
                consume(row.item, row.get("reservation_line_key"), row.get("comments"), flt(row.cancelled_qty), originals)
        else:
            releases.extend([ticket.name, row, flt(row.get("quantity"))] for row in ticket.kot_items)

    original_kots = []
    for row in cancel_items:
        original_kots.extend(consume(row["item_code"], row.get("reservation_line_key"), row.get("comments"), abs(flt(row["qty"]))))
    set_kots = ",".join(dict.fromkeys(original_kots))
    kot_cancel_doc = frappe.get_doc(
        {
            "doctype": "URY KOT",
            "naming_series": cancel_kot_naming_series,
            "original_kot": set_kots,
            "restaurant_table": restaurant_table,
            "customer_name": customer,
            "custom_guest_label": pos_invoice.get("custom_guest_label") or None,
            "type": kot_type,
            "invoice": invoice_id,
            "pos_profile": pos_profile_id,
            "comments": comments,
            "production": production,
            "is_aggregator":is_aggregator,
            "order_no":order_number
        }
    )

    if restaurant_table:
        room = frappe.db.get_value("URY Table", restaurant_table, "restaurant_room")
        restaurant = frappe.db.get_value("URY Table", restaurant_table, "restaurant")
        menu = frappe.db.get_value("Menu for Room", {"room": room,"parent":restaurant}, "menu")

    else:
        # No-table cancel KOTs must derive branch from the invoice being
        # cancelled, not from the acting user's session (getBranch()) --
        # same bug class as PR #373 bug #1 and the create_kot_doc() fix
        # above. Using the session branch picks the wrong branch's active
        # menu (and therefore the wrong course grouping) whenever the
        # cancelling user's session branch differs from the invoice's own
        # branch.
        menu = frappe.db.get_value("URY Restaurant", {"branch": pos_invoice.branch}, "active_menu")
    for cancelItem in cancel_items:
        course = frappe.db.get_value("URY Menu Item", {"item": cancelItem["item_code"],"parent":menu}, "course")
        cancel_line_key = cancelItem.get("reservation_line_key")
        # Resolve the single invoice line this cancellation belongs to.
        # Matching by reservation_line_key when available prevents this
        # from appending a cancel row (and picking up the wrong quantity)
        # for EVERY invoice line that shares the same item_code -- the
        # previous behaviour had no `break`, so two lines of the same item
        # produced duplicate/incorrect cancel entries.
        matched_item = None
        if cancel_line_key:
            for item in invoiceItems:
                if item.get("reservation_line_key") == cancel_line_key:
                    matched_item = item
                    break
        if matched_item is None:
            for item in invoiceItems:
                if (cancelItem["item_code"] == item["item_code"]
                        and (cancelItem.get("comments") or "") == (item.get("comments") or "")
                        and not (cancel_line_key and item.get("reservation_line_key"))):
                    matched_item = item
                    break
        if matched_item is None:
            continue
        kot_cancel_doc.append(
            "kot_items",
            {
                "item": cancelItem["item_code"],
                "item_name": cancelItem["item_name"],
                "cancelled_qty": abs(flt(cancelItem["qty"])),
                "quantity": matched_item["qty"],
                "comments": cancelItem["comments"],
                "course": course,
                "reservation_line_key": cancel_line_key,
                "parent_line_key": cancelItem.get("custom_parent_line_key") or None,
                "is_modifier": cint(cancelItem.get("custom_is_modifier")),
                "modifier_summary": _modifier_summary_for_kot_item(cancelItem),
            },
        )

    kot_cancel_doc.insert()
    kot_cancel_doc.submit()
    return kot_cancel_doc.name


# Whitelisted function to handle KOT entry
@frappe.whitelist()
def kot_execute(
    invoice_id,
    customer,
    restaurant_table=None,
    current_items=[],
    previous_items=[],
    comments=None,
    offline_printed=0,
):
    return _execute_kot(invoice_id, customer, restaurant_table, current_items, previous_items,
                        comments, offline_printed, desk=True)


def _execute_kot(invoice_id, customer, restaurant_table=None, current_items=None, previous_items=None,
                 comments=None, offline_printed=0, *, desk=False):
    from frappe import _

    pos_invoice = frappe.get_doc("POS Invoice", invoice_id)
    if not frappe.has_permission("POS Invoice", "write", doc=pos_invoice):
        frappe.throw(_("Not permitted to execute KOT for this invoice"), frappe.PermissionError)
    if cint(offline_printed) == 1:
        from ury.ury.doctype.ury_order.ury_order import require_offline_capture

        require_offline_capture(pos_invoice.pos_profile)

    current_items = load_json(current_items)
    previous_items = load_json(previous_items)

    if desk:
        previous_items = [row for row in previous_items or [] if not cint(row.get("ury_held"))]
        saved = pos_invoice.get("items") or []
        if any(row.get("reservation_line_key") or cint(row.get("ury_held")) for row in saved):
            by_name = {row.name: row for row in saved}
            current_items = [dict(item_code=row.item_code, item_name=row.item_name, qty=row.qty,
                                  comments=row.get("comment") or "",
                                  reservation_line_key=row.get("reservation_line_key") or row.name)
                             for row in saved if not cint(row.get("ury_held"))]
            baseline = []
            for row in previous_items:
                child = by_name.get(row.get("name"))
                if child and cint(child.get("ury_held")):
                    continue
                key = child.get("reservation_line_key") if child else None
                baseline.append(dict(row, reservation_line_key=key or _line_ref(row)))
            previous_items = baseline

    # Diff current vs previous items PER STABLE LINE (see _line_keyed_items),
    # not per item_code. Two lines of the same item_code (different
    # comments/courses, or a plain duplicate) are now tracked as distinct
    # lines, so a qty change/removal on one line can never be attributed to
    # the other. See sa-architecture-closure.
    current_lines = _line_keyed_items(current_items)
    previous_lines = _line_keyed_items(previous_items)

    positive_qty_items = []
    negative_qty_items = []

    for key, cur in current_lines.items():
        prev = previous_lines.get(key)
        prev_qty = flt(prev["qty"]) if prev and prev["comments"] == cur["comments"] else 0
        # Round to the site precision so float subtraction residue (for
        # example 0.30000000000000004 - 0.3) is not read as a quantity change.
        delta = flt(
            flt(cur["qty"]) - prev_qty,
            frappe.get_precision("URY KOT Items", "quantity"),
        )
        if delta == 0:
            continue
        item = dict(cur)
        item["qty"] = delta
        if delta > 0:
            positive_qty_items.append(item)
        else:
            negative_qty_items.append(item)

    removed_item = []
    for key, prev in previous_lines.items():
        if key in current_lines and current_lines[key]["comments"] == prev["comments"]:
            continue
        item = dict(prev)
        item["qty"] = -flt(prev["qty"])
        removed_item.append(item)

    pos_profile_id = pos_invoice.pos_profile
    pos_profile = frappe.get_doc("POS Profile", pos_profile_id)
    kot_naming_series = pos_profile.custom_kot_naming_series
    if kot_naming_series:
        cancel_kot_naming_series = "CNCL-" + kot_naming_series
    else:
        frappe.throw(
            "KOT Naming Series is mandatory for the auto creation of KOT.Ensure it is configured in the POS Profile: %s"
            % pos_profile.name
        )

    from ury.ury.api.ury_waiter_print import print_combined_waiter_order_slip

    total_cancel_items = negative_qty_items + removed_item
    created_kot_names = []

    if positive_qty_items:
        created_kot_names.extend(
            process_items_for_kot(
                invoice_id,
                customer,
                restaurant_table,
                positive_qty_items,
                comments,
                pos_profile_id,
                kot_naming_series,
                "New Order",
                offline_printed=offline_printed,
            )
        )
    if total_cancel_items:
        created_kot_names.extend(
            process_items_for_cancel_kot(
                invoice_id,
                customer,
                restaurant_table,
                total_cancel_items,
                comments,
                pos_profile_id,
                cancel_kot_naming_series,
                "Partially cancelled",
                list(previous_lines.values()),
            )
        )

    if created_kot_names and cint(offline_printed) != 1:
        print_combined_waiter_order_slip(invoice_id, created_kot_names, restaurant_table)


# Compare two arrays and return the items that are different
def compare_two_array(array_1, array_2):
    finalarray = []
    for index, x in enumerate(array_1):
        a = list(
            filter(
                lambda y: y["item_code"] == x["item_code"] and (y.get("comments") or "") == (x.get("comments") or "") and flt(y["qty"]) == flt(x["qty"]),
                array_2,
            )
        )
        if len(a) == 0:
            b = list(filter(lambda z: z["item_code"] == x["item_code"] and (z.get("comments") or "") == (x.get("comments") or ""), array_2))
            for qtb in b:
                x["qty"] = flt(x["qty"]) - flt(qtb["qty"])
            finalarray.append(x)
    return finalarray


# Get the items that have been removed from the second array compared to the first array
def get_removed_items(array_1, array_2):
    removed_objects = [
        obj
        for obj in array_1
        if (obj["item_code"], obj.get("comments") or "")
        not in [(x["item_code"], x.get("comments") or "") for x in array_2]
    ]
    return removed_objects
