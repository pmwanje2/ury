import frappe
import json
import sys
from frappe import _
from datetime import date, datetime, timedelta
from frappe.utils import cint, nestedset, validate_phone_number

try:
    from frappe.utils import flt
except ImportError:
    def flt(value, precision=None):
        num = float(value or 0)
        return round(num, precision) if precision is not None else num

from urllib.parse import quote

from ury.brand import coded_message
from ury.ury_pos.default_customer import get_default_customer_config
from ury.ury.api.item_price import item_price_release_states, price_valid_on

_DEFAULT_EFRIS_PRINT_WAIT_SECONDS = 5
_MAX_EFRIS_PRINT_WAIT_SECONDS = 15


def _efris_print_wait_seconds(profile):
    raw = profile.get("custom_efris_print_wait_seconds")
    if raw is None or raw == "":
        return _DEFAULT_EFRIS_PRINT_WAIT_SECONDS
    return max(0, min(cint(raw), _MAX_EFRIS_PRINT_WAIT_SECONDS))


def _permission_error_types():
    types = []
    exc = getattr(frappe, "PermissionError", None)
    if isinstance(exc, type) and issubclass(exc, BaseException):
        types.append(exc)
    real_frappe = sys.modules.get("frappe")
    real_exc = getattr(real_frappe, "PermissionError", None) if real_frappe is not None else None
    if isinstance(real_exc, type) and issubclass(real_exc, BaseException) and real_exc not in types:
        types.append(real_exc)
    return tuple(types)


def _safe_flt_value(value, default=0.0):
    try:
        return frappe.utils.flt(value)
    except (TypeError, ValueError):
        return default


def _safe_int_value(value, default=0):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return default
    return default


def _drawer_kick_profile_fields(profile):
    """Return G23 cash drawer pin/hex/auto-kick settings and config-driven drawer-opening modes."""
    resolved_pin = _safe_int_value(profile.get("custom_drawer_pin"), default=2)
    if resolved_pin not in (2, 5):
        resolved_pin = 2
    raw_hex_val = profile.get("custom_drawer_kick_hex")
    raw_hex = raw_hex_val.strip() if isinstance(raw_hex_val, str) else ""
    auto_kick_raw = profile.get("custom_auto_kick_on_cash_tender")
    auto_kick = 1 if auto_kick_raw is None else (1 if _safe_int_value(auto_kick_raw, default=1) else 0)
    drawer_modes = []
    for payment in (profile.get("payments") or []):
        mop = getattr(payment, "mode_of_payment", None) or (
            payment.get("mode_of_payment") if isinstance(payment, dict) else None
        )
        if not isinstance(mop, str) or not mop.strip():
            continue
        mop = mop.strip()
        if mop in drawer_modes:
            continue
        opens = frappe.db.get_value("Mode of Payment", mop, "custom_open_cash_drawer")
        if _safe_int_value(opens, default=0):
            drawer_modes.append(mop)
    return {
        "custom_drawer_pin": resolved_pin,
        "custom_drawer_kick_hex": raw_hex,
        "custom_auto_kick_on_cash_tender": auto_kick,
        "drawer_opening_modes": drawer_modes,
    }


#GetTable  decripted temporarily
# @frappe.whitelist()
# def getTable(room):
#     branch_name = getBranch()
#     tables = frappe.get_all(
#         "URY Table",
#         fields=["name", "occupied", "latest_invoice_time", "is_take_away", "restaurant_room","table_shape","no_of_seats","layout_x","layout_y"],
#         filters={"branch": branch_name,"restaurant_room":room,}
#     )
#     return tables

def resolve_restaurant_menu(
    branch,
    room=None,
    order_type=None,
    cashier=False,
    pos_profile=None,
    customer=None,
    at=None,
):
    """
    Resolve and return menu for a given branch, room, and order type using the
    5-tier precedence (Aggregators -> room_wise_menu -> active Serving Period ->
    order_type_wise_menu -> active_menu), plus per-item daypart availability and
    active time-window Pricing Rule promo badges.
    """
    from ury.ury_pos.dayparts import (
        active_item_promos,
        evaluate_item_daypart,
        resolve_menu_and_price_list,
    )

    if isinstance(pos_profile, str):
        pos_profile = frappe.get_doc("POS Profile", pos_profile.strip()) if pos_profile.strip() else None

    restaurant = frappe.db.get_value("URY Restaurant", {"branch": branch}, "name")
    resolved = resolve_menu_and_price_list(
        branch,
        restaurant=restaurant,
        room=room,
        order_type=order_type,
        customer=customer,
        at=at,
        cashier=cashier,
    )
    menu = resolved.get("menu")
    price_list = resolved.get("price_list")
    active_period = resolved.get("serving_period")
    local_dt = resolved.get("local_dt")
    tz_name = resolved.get("timezone")

    if not menu:
        frappe.throw(_("Please set an active menu for Restaurant {0}").format(restaurant))

    menu_items = frappe.get_all(
        "URY Menu Item",
        filters={"parent": menu, "disabled": 0},
        fields=[
            "item",
            "item_name",
            "rate",
            "special_dish",
            "disabled",
            "course",
            "idx",
            "custom_available_from",
            "custom_available_to",
            "custom_serving_period",
        ],
        order_by="item_name asc",
    )

    # Guest screens order categories by the menu's own configuration
    # (Simphony SLU order): course serving priority, then menu row order.
    # 0 is the Int default, so it means "not configured".
    courses = {item.course for item in menu_items if item.course}
    course_priority = {
        row.name: row.custom_serving_priority or None
        for row in frappe.get_all(
            "URY Menu Course",
            filters={"name": ["in", list(courses)]},
            fields=["name", "custom_serving_priority"],
        )
    } if courses else {}

    posting_date = frappe.utils.getdate(local_dt.date() if local_dt else None)
    if not price_list:
        price_list = frappe.db.get_value(
            "Price List", {"restaurant_menu": menu, "enabled": 1}, "name"
        )
    release_field, released = (None, None)
    if price_list:
        try:
            release_field, released = item_price_release_states()
        except Exception:
            release_field, released = (None, None)

    company = (
        pos_profile.get("company")
        if pos_profile is not None and hasattr(pos_profile, "get")
        else frappe.db.get_value("Branch", branch, "company")
    )
    try:
        from ury.ury.api.ury_modifiers import get_modifier_groups_for_items

        modifier_meta = get_modifier_groups_for_items(
            [item.item for item in menu_items],
            price_list=price_list,
            branch=branch,
            company=company,
            posting_date=posting_date,
            customer=customer,
        )
    except Exception:
        modifier_meta = {}

    item_codes = [item.item for item in menu_items if item.item]
    open_item_flags = {}
    if item_codes:
        item_fields = ["name", "image"]
        has_col = getattr(getattr(frappe, "db", None), "has_column", None)
        has_open_col = not callable(has_col) or bool(has_col("Item", "custom_is_open_priced"))
        has_zero_col = not callable(has_col) or bool(has_col("Item", "custom_allow_zero_price"))
        if has_open_col:
            item_fields.append("custom_is_open_priced")
        if has_zero_col:
            item_fields.append("custom_allow_zero_price")
        try:
            for row in frappe.get_all(
                "Item",
                filters={"name": ["in", item_codes]},
                fields=item_fields,
            ):
                open_item_flags[row.name] = row
        except Exception:
            open_item_flags = {}

    menu_items_with_image = []
    for item in menu_items:
        resolved_rate = item.rate
        if price_list:
            try:
                ip = price_valid_on(
                    item.item, price_list, posting_date, release_field, released, customer=customer
                )
                if ip and ip.get("price_list_rate") is not None:
                    resolved_rate = ip.get("price_list_rate")
            except Exception:
                pass

        daypart_status = evaluate_item_daypart(
            item, active_period=active_period, local_dt=local_dt, branch=branch
        )
        menu_items_with_image.append(
            {
                "item": item.item,
                "item_name": _(item.item_name) if item.item_name else item.item_name,
                "rate": resolved_rate,
                "special_dish": item.special_dish,
                "disabled": item.disabled,
                "item_image": (
                    open_item_flags[item.item].get("image")
                    if item.item in open_item_flags
                    else frappe.db.get_value("Item", item.item, "image")
                ),
                "custom_is_open_priced": cint(
                    open_item_flags.get(item.item, {}).get("custom_is_open_priced")
                ),
                "custom_allow_zero_price": cint(
                    open_item_flags.get(item.item, {}).get("custom_allow_zero_price")
                ),
                "course": item.course,
                "course_label": _(item.course) if item.course else item.course,
                "course_priority": course_priority.get(item.course),
                "menu_order": item.idx,
                "unavailable_daypart": daypart_status["unavailable_daypart"],
                "daypart_label": daypart_status["daypart_label"],
                **(modifier_meta.get(item.item) or {
                    "is_combo_meal": False,
                    "product_bundle": None,
                    "bundle_defaults": [],
                    "has_modifiers": False,
                    "has_required_modifiers": False,
                    "prompt_on_add": False,
                    "modifier_groups": [],
                }),
            }
        )

    promos = active_item_promos(
        menu_items_with_image,
        company=company,
        price_list=price_list,
        customer=customer,
        local_dt=local_dt,
        posting_date=posting_date,
        order_type=order_type,
    )
    for row in menu_items_with_image:
        promo = promos.get(row["item"])
        if promo:
            row["promo_rate"] = promo["promo_rate"]
            row["promo_label"] = promo["promo_label"]

    tax_summary = None
    if pos_profile is not None:
        from ury.ury_pos.tax_determination import summarize_tax_for_order_type

        tax_summary = summarize_tax_for_order_type(
            pos_profile, order_type, customer=customer, posting_date=posting_date
        )

    modified = frappe.db.get_value("URY Menu", menu, "modified")

    return {
        "items": menu_items_with_image,
        "modified_time": modified,
        "name": menu,
        "active_serving_period": active_period,
        "price_list": price_list,
        "timezone": tz_name,
        "tax_summary": tax_summary,
    }

def _authorize_pos_profile_access(profile, *, expected_branch=None):
    """Enforce read permission and branch/outlet authorization on a POS Profile."""
    if callable(getattr(profile, "check_permission", None)):
        profile.check_permission("read")
    elif not frappe.has_permission("POS Profile", "read", doc=profile):
        frappe.throw(_("Not permitted to access this POS Profile."), frappe.PermissionError)

    user_roles = set(frappe.get_roles() or [])
    is_super = frappe.session.user == "Administrator" or "System Manager" in user_roles
    if is_super:
        return None

    user_branch = expected_branch if expected_branch is not None else getBranch()
    profile_branch = (profile.get("branch") or "").strip() if hasattr(profile, "get") else ""
    if profile_branch:
        if not user_branch or user_branch != profile_branch:
            frappe.throw(_("Not permitted outside your active branch."), frappe.PermissionError)
        return user_branch

    # Branchless POS Profile: non-privileged POS users must be explicitly listed in
    # applicable_for_users and match the branch's company when both are set.
    applicable_users = [
        (row.get("user") or "").strip()
        for row in (profile.get("applicable_for_users") or [])
        if (row.get("user") or "").strip()
    ]
    if not applicable_users or frappe.session.user not in applicable_users:
        frappe.throw(_("Not permitted to access this POS Profile."), frappe.PermissionError)

    profile_company = (profile.get("company") or "").strip() if hasattr(profile, "get") else ""
    if user_branch and profile_company:
        branch_company = (frappe.db.get_value("Branch", user_branch, "company") or "").strip()
        if branch_company and branch_company != profile_company:
            frappe.throw(_("Not permitted outside your active branch."), frappe.PermissionError)
    return user_branch


def _authorize_customer_for_pos_profile(customer_name, profile):
    """Load Customer and enforce Frappe read/User Permissions and POS Profile customer-group scope."""
    customer_doc = frappe.get_doc("Customer", customer_name)
    if callable(getattr(customer_doc, "check_permission", None)):
        customer_doc.check_permission("read")
    if not frappe.has_permission("Customer", "read", doc=customer_doc):
        frappe.throw(_("Not permitted to access this Customer."), frappe.PermissionError)

    user_roles = set(frappe.get_roles() or [])
    is_super = frappe.session.user == "Administrator" or "System Manager" in user_roles
    if is_super:
        return customer_doc

    allowed_groups = {
        (row.get("customer_group") or "").strip()
        for row in (profile.get("customer_groups") or [])
        if (row.get("customer_group") or "").strip()
    }
    if allowed_groups:
        cust_group = (customer_doc.get("customer_group") or "").strip()
        if cust_group and cust_group not in allowed_groups:
            group_permitted = False
            try:
                bounds = frappe.db.get_value("Customer Group", cust_group, ["lft", "rgt"], as_dict=True)
                if isinstance(bounds, dict) and bounds.get("lft") is not None:
                    ancestors = frappe.get_all(
                        "Customer Group",
                        filters={"lft": ["<=", bounds["lft"]], "rgt": [">=", bounds["rgt"]]},
                        pluck="name",
                    )
                    group_permitted = bool(set(ancestors or []) & allowed_groups)
            except Exception:
                group_permitted = False
            if not group_permitted:
                frappe.throw(_("Customer is not permitted for this POS Profile."), frappe.PermissionError)

    cust_branch = (customer_doc.get("branch") or "").strip() if hasattr(customer_doc, "get") else ""
    profile_branch = (profile.get("branch") or "").strip() if hasattr(profile, "get") else ""
    if cust_branch and profile_branch and cust_branch != profile_branch:
        frappe.throw(_("Not permitted to access a customer outside your active branch."), frappe.PermissionError)

    return customer_doc


@frappe.whitelist()
def getRestaurantMenu(pos_profile, room=None, order_type=None, customer=None):
    user_role = frappe.get_roles()

    pos_profile_name = pos_profile if isinstance(pos_profile, str) else getattr(pos_profile, "name", "")
    try:
        from ury.ury_pos import outlet_context

        if pos_profile_name and outlet_context.get_authorized_outlets(frappe.session.user):
            outlet_context.assert_outlet_access(pos_profile_name, user=frappe.session.user)
    except _permission_error_types():
        raise
    except Exception:
        pass

    pos_profile = frappe.get_doc("POS Profile", pos_profile)
    try:
        branch_name = getBranch(pos_profile=pos_profile.name)
    except TypeError:
        branch_name = getBranch()
    _authorize_pos_profile_access(pos_profile, expected_branch=branch_name)
    if customer and str(customer).strip():
        _authorize_customer_for_pos_profile(str(customer).strip(), pos_profile)

    cashier = any(
        role.role in user_role for role in pos_profile.role_allowed_for_billing
    )
    branch_name = getattr(pos_profile, "branch", None) or branch_name

    return resolve_restaurant_menu(
        branch_name,
        room,
        order_type,
        cashier,
        pos_profile=pos_profile,
        customer=customer,
    )

@frappe.whitelist()
def getMenuCourses():
    courses = frappe.get_all("URY Menu Course", filters={"disabled": 0}, fields=["name", "icon"])
    return [{"name": d.name, "label": _(d.name), "icon": d.icon} for d in courses]

def _user_branch_assignments(pos_profile=None):
    selected_branch = None
    try:
        from ury.ury_pos import outlet_context

        selected_branch = outlet_context.resolve_session_selected_branch(
            pos_profile=pos_profile, user=frappe.session.user
        )
    except _permission_error_types():
        raise
    except Exception:
        selected_branch = None

    rows = frappe.db.sql("""
        SELECT b.name AS branch, a.room
        FROM `tabURY User` AS a
        INNER JOIN `tabBranch` AS b ON a.parent = b.name
        WHERE a.user = %s AND a.parenttype = 'Branch'
    """, frappe.session.user, as_dict=True)
    if selected_branch:
        matched = [row for row in (rows or []) if row.get("branch") == selected_branch]
        if matched:
            return matched
        return [frappe._dict(branch=selected_branch, room=None)]

    if not rows:
        frappe.throw("User is not Associated with any Branch.Please refresh Page")

    branches = {row.get("branch") for row in rows}
    if len(branches) == 1:
        return rows

    from frappe.core.doctype.user_permission.user_permission import get_user_permissions

    defaults = {
        permission.get("doc")
        for permission in get_user_permissions().get("Branch", [])
        if permission.get("is_default") and permission.get("doc") in branches
    }
    if len(defaults) != 1:
        frappe.throw(coded_message("BRANCH-AMBIGUOUS", _("Set a default Branch User Permission for an assigned branch.")))
    branch = defaults.pop()
    return [row for row in rows if row.get("branch") == branch]


@frappe.whitelist()
def getBranch(pos_profile=None):
    return _user_branch_assignments(pos_profile=pos_profile)[0].get("branch")

@frappe.whitelist()
def getBranchRoom():
    branch_array = _user_branch_assignments()

    branch_name = branch_array[0].get("branch")
    room_name = branch_array[0].get("room")

    if not branch_name:
        frappe.throw("Branch information is missing for the user. Please contact your administrator.")

    if not room_name:
        frappe.throw("No room assigned to this user. Please contact your administrator.")

    return [{
        "name":room_name ,
        "branch": branch_name,
    }]

@frappe.whitelist()
def getRoom():
    branch_array = _user_branch_assignments()

    if not branch_array:
        frappe.throw("No branch or room information found for the user. Please contact your administrator.")

    room_details = [
        {
            "name": row.get("room"),
            "branch": row.get("branch")
        }
        for row in branch_array
    ]

    return room_details

@frappe.whitelist()
def getModeOfPayment():
    posDetails = getPosProfile()
    if (
        not isinstance(posDetails, dict)
        or posDetails.get("requires_outlet_selection")
        or not posDetails.get("pos_profile")
    ):
        return []
    from ury.ury_pos.fx import currency_precision, get_mode_payment_config, resolve_pos_exchange_rate
    from ury.ury_pos.offline_tenders import profile_offline_payment_modes
    posProfile = posDetails["pos_profile"]
    posProfiles = frappe.get_doc("POS Profile", posProfile)
    company = posProfiles.company
    home_currency = (posProfiles.currency or "").strip().upper()
    mode_of_payments = posProfiles.payments
    offline_by_mode = {
        row["mode_of_payment"]: row
        for row in profile_offline_payment_modes(
            posProfiles, posDetails.get("room_charge_modes"), _frappe=frappe
        )
    }
    modeOfPayments = []
    for mop in mode_of_payments:
        cfg = get_mode_payment_config(mop.mode_of_payment, company=company)
        meta = offline_by_mode.get(mop.mode_of_payment, {})
        tender_curr = cfg.get("custom_tender_currency") or ""
        fx_fields = {}
        if tender_curr and home_currency and tender_curr != home_currency:
            rate_type = cfg.get("custom_fx_rate_type") or "Buying"
            fx_fields["tender_precision"] = currency_precision(tender_curr)
            try:
                rate_info = resolve_pos_exchange_rate(
                    tender_curr,
                    home_currency,
                    company=company,
                    rate_type=rate_type,
                )
                fx_fields["exchange_rate"] = rate_info["rate"]
                fx_fields["exchange_rate_date"] = rate_info["rate_date"]
                fx_fields["fx_rate_status"] = "fresh"
            except frappe.ValidationError as exc:
                fx_fields["exchange_rate"] = None
                fx_fields["exchange_rate_date"] = None
                fx_fields["fx_rate_status"] = "stale"
                fx_fields["fx_rate_error"] = str(exc)
        modeOfPayments.append(
            {
                "mode_of_payment": mop.mode_of_payment,
                "opening_amount": float(0),
                "type": cfg.get("type") or "",
                "allow_offline": bool(meta.get("allow_offline")),
                "requires_reference": bool(meta.get("requires_reference")),
                "custom_reference_mode": cfg.get("custom_reference_mode") or "Hidden",
                "custom_reference_label": cfg.get("custom_reference_label") or "",
                "custom_reference_placeholder": cfg.get("custom_reference_placeholder") or "",
                "custom_reference_pattern": cfg.get("custom_reference_pattern") or "",
                "custom_reference_date_required": cint(cfg.get("custom_reference_date_required")),
                "custom_tender_currency": tender_curr,
                "custom_fx_rate_type": cfg.get("custom_fx_rate_type") or "Buying",
                "default_account": cfg.get("default_account"),
                "custom_fx_vault_account": cfg.get("custom_fx_vault_account"),
                **fx_fields,
            }
        )
    return modeOfPayments


@frappe.whitelist()
def get_production_units_for_branch():
	"""Fetch all production unit names for the current user's branch.

	Returns a list of production unit names that should be subscribed to for
	KOT error channels on the POS terminal.
	"""
	try:
		branch = getBranch()
	except frappe.exceptions.ValidationError:
		# Fallback if getBranch() throws (e.g., Administrator with no branch)
		return {"production_units": []}

	if not branch:
		return {"production_units": []}

	from ury.ury.api.ury_kot_display import get_production_units
	return {"production_units": [unit["name"] for unit in get_production_units(branch=branch)]}


def format_merged_table_label(primary, merged_tables=None):
    if not primary:
        return ""
    partners = [p.strip() for p in (merged_tables or "").split(",") if p.strip()]
    if not partners:
        return primary
    return " + ".join([primary] + sorted(partners))


def _backfill_split_groups(invoices):
    parent_names = [
        inv["custom_split_from"]
        for inv in invoices
        if inv.get("custom_split_from") and not inv.get("custom_split_group")
    ]
    if not parent_names:
        return

    parent_rows = frappe.get_all(
        "POS Invoice",
        filters={"name": ["in", parent_names]},
        fields=["name", "custom_split_group"],
    )
    parent_group_map = {
        row.name: row.custom_split_group
        for row in parent_rows
        if row.custom_split_group
    }
    for inv in invoices:
        if not inv.get("custom_split_group") and inv.get("custom_split_from"):
            inv["custom_split_group"] = parent_group_map.get(inv["custom_split_from"])


def _enrich_split_group_meta(invoices):
    if not invoices:
        return invoices

    _backfill_split_groups(invoices)

    groups = list(
        {inv.get("custom_split_group") for inv in invoices if inv.get("custom_split_group")}
    )
    if not groups:
        for inv in invoices:
            inv["split_index"] = 0
            inv["split_total"] = 0
            inv["split_siblings"] = []
        return invoices

    group_members = frappe.db.sql(
        """
        SELECT name, custom_split_group
        FROM `tabPOS Invoice`
        WHERE custom_split_group IN %(groups)s AND docstatus < 2
        ORDER BY creation asc
        """,
        {"groups": groups},
        as_dict=True,
    )

    group_order = {}
    for row in group_members:
        group_order.setdefault(row.custom_split_group, []).append(row.name)

    for group, names in list(group_order.items()):
        children = frappe.get_all(
            "POS Invoice",
            filters={"custom_split_from": ["in", names], "docstatus": ["<", 2]},
            fields=["name"],
            order_by="creation asc",
        )
        for child in children:
            if child.name not in names:
                names.append(child.name)

    for inv in invoices:
        group = inv.get("custom_split_group")
        if not group or group not in group_order:
            inv["split_index"] = 0
            inv["split_total"] = 0
            inv["split_siblings"] = []
            continue
        names = group_order[group]
        inv["split_total"] = len(names)
        inv["split_siblings"] = [name for name in names if name != inv["name"]]
        try:
            inv["split_index"] = names.index(inv["name"]) + 1
        except ValueError:
            inv["split_index"] = 0
            inv["split_total"] = 0
            inv["split_siblings"] = []

    return invoices


@frappe.whitelist()
def get_split_group(invoice):
    pos_invoice = frappe.get_doc("POS Invoice", invoice)
    
    if not frappe.has_permission("POS Invoice", "read", doc=pos_invoice):
        frappe.throw(frappe._("Not permitted to view this order"), frappe.PermissionError)
        
    user_branch = getBranch()
    if pos_invoice.branch and user_branch and pos_invoice.branch != user_branch:
        frappe.throw(frappe._("Not permitted to view orders outside your active branch"), frappe.PermissionError)

    from ury.ury.doctype.ury_order.ury_order import check_split_target_access

    check_split_target_access(pos_invoice, "read")
    group = pos_invoice.custom_split_group
    if not group:
        split_from = frappe.db.get_value("POS Invoice", invoice, "custom_split_from")
        if split_from:
            group = frappe.db.get_value("POS Invoice", split_from, "custom_split_group")
    if not group:
        return {"invoices": [], "current": invoice, "group": None}

    split_fields = [
        "name",
        "ury_client_order_id",
        "currency",
        "conversion_rate",
        "custom_split_from",
        "custom_split_group",
        "invoice_printed",
        "restaurant_table",
        "custom_merged_tables",
        "rounded_total",
        "grand_total",
        "customer",
        "customer_name",
        "custom_guest_label",
        "no_of_pax",
        "status",
        "docstatus",
        "posting_date",
        "posting_time",
        "order_type",
        "cashier",
        "waiter",
        "mobile_number",
        "net_total",
        "total_taxes_and_charges",
        "creation",
        "branch",
        "additional_discount_percentage",
        "discount_amount",
    ]

    invoices = frappe.get_all(
        "POS Invoice",
        filters={"custom_split_group": group, "docstatus": ["<", 2]},
        fields=split_fields,
        order_by="creation asc",
    )

    member_names = [inv["name"] for inv in invoices]
    # Legacy children can lack a group even after their original is cancelled.
    if pos_invoice.custom_split_from and pos_invoice.custom_split_from not in member_names:
        member_names.append(pos_invoice.custom_split_from)
    if member_names:
        children = frappe.get_all(
            "POS Invoice",
            filters={"custom_split_from": ["in", member_names], "docstatus": ["<", 2]},
            fields=split_fields,
            order_by="creation asc",
        )
        existing = {inv["name"] for inv in invoices}
        for child in children:
            if child.name not in existing:
                invoices.append(child)
                existing.add(child.name)

    invoices.sort(key=lambda row: row.get("creation") or row.get("name"))

    valid_invoices = []
    for inv in invoices:
        if inv.get("branch") and user_branch and inv.get("branch") != user_branch:
            continue
        if inv.name != invoice:
            try:
                check_split_target_access(frappe.get_doc("POS Invoice", inv.name), "read")
            except _permission_error_types():
                frappe.clear_last_message()
                continue
        valid_invoices.append(inv)
    visible_names = {row.name for row in valid_invoices}
    # Older groups can be named for an invoice. Only expose the group ID
    # when all members and parent links in this view are visible.
    if len(valid_invoices) != len(invoices) or any(
        row.custom_split_from and row.custom_split_from not in visible_names for row in valid_invoices
    ):
        group = None
    invoices = valid_invoices
    total = len(invoices)
    for index, inv in enumerate(invoices, start=1):
        inv["split_index"] = index
        inv["split_total"] = total
        inv["is_original"] = not inv.get("custom_split_from")
        if group is None:
            inv["custom_split_group"] = None
        if inv.get("custom_split_from") not in visible_names:
            inv["custom_split_from"] = None
        inv["split_siblings"] = [row["name"] for row in invoices if row["name"] != inv["name"]]

    return {"invoices": invoices, "current": invoice, "group": group}


@frappe.whitelist()
def getInvoiceForCashier(status, cashier, limit, limit_start):
    # U27: `cashier` was previously accepted as-is from the caller, so any
    # authenticated user in a branch could read another cashier's invoices.
    # Only an elevated role may look up someone else's invoices; everyone
    # else is scoped to their own session user regardless of what they pass.
    if cashier != frappe.session.user:
        elevated_roles = {"System Manager", "URY Manager", "URY Captain"}
        if not elevated_roles.intersection(frappe.get_roles()):
            cashier = frappe.session.user
    branch = getBranch()
    updatedlist = []
    limit = int(limit)+1
    limit_start = int(limit_start)
    if status == "Draft":
        invoices = frappe.db.sql(
            """
            SELECT 
                name, invoice_printed, custom_printing_time, grand_total, restaurant_table, custom_merged_tables,
                cashier, waiter, net_total, posting_time, 
                total_taxes_and_charges, customer, status, mobile_number, 
                posting_date, rounded_total, order_type 
            FROM `tabPOS Invoice` 
            WHERE branch = %s AND status = %s AND cashier = %s
            AND (invoice_printed = 1 OR (invoice_printed = 0 AND COALESCE(restaurant_table, '') = ''))
            ORDER BY modified desc
            LIMIT %s OFFSET %s
            """,
            (branch, status, cashier, limit,limit_start),
            as_dict=True,
        )
        updatedlist.extend(invoices)
    elif status == "Unbilled":
        
        docstatus = "Draft"
        invoices = frappe.db.sql(
            """
            SELECT 
                name, invoice_printed, custom_printing_time, grand_total, restaurant_table, custom_merged_tables,
                cashier, waiter, net_total, posting_time, 
                total_taxes_and_charges, customer, status, mobile_number, 
                posting_date, rounded_total, order_type 
            FROM `tabPOS Invoice` 
            WHERE branch = %s AND status = %s AND cashier = %s
            AND (invoice_printed = 0 AND restaurant_table IS NOT NULL)
            ORDER BY modified desc
            LIMIT %s OFFSET %s
            """,
            (branch, docstatus, cashier, limit, limit_start),
            as_dict=True,
        )
        updatedlist.extend(invoices)
    elif status == "Recently Paid":
        docstatus = "Paid"
        invoices = frappe.db.sql(
            """
            SELECT 
                name, invoice_printed, custom_printing_time, grand_total, restaurant_table, custom_merged_tables,
                cashier, waiter, net_total, posting_time, 
                total_taxes_and_charges, customer, status, mobile_number,
                posting_date, rounded_total, order_type,additional_discount_percentage,discount_amount 
            FROM `tabPOS Invoice` 
            WHERE branch = %s AND status = %s AND cashier = %s
            ORDER BY modified desc
            LIMIT %s OFFSET %s
            """,
            (branch, docstatus, cashier, limit, limit_start),
            as_dict=True,
        )
        updatedlist.extend(invoices)    
    else:
        
        invoices = frappe.db.sql(
            """
            SELECT 
                name, invoice_printed, custom_printing_time, grand_total, restaurant_table, custom_merged_tables,
                cashier, waiter, net_total, posting_time, 
                total_taxes_and_charges, customer, status, mobile_number,
                posting_date, rounded_total, order_type,additional_discount_percentage,discount_amount
            FROM `tabPOS Invoice` 
            WHERE branch = %s AND status = %s AND cashier = %s
            ORDER BY modified desc
            LIMIT %s OFFSET %s
            """,
            (branch, status, cashier, limit, limit_start),
            as_dict=True,
        )

        updatedlist.extend(invoices)
    if len(updatedlist) == limit and status != "Recently Paid":
            next = True
            updatedlist.pop()
    else:
            next = False   
    return  { "data":updatedlist,"next":next}



@frappe.whitelist()
def getPosInvoice(status, limit, limit_start):
    branch = getBranch()
    updatedlist = []
    limit = int(limit)+1
    limit_start = int(limit_start)
    extra_cols = ""
    if frappe.db.has_column("POS Invoice", "efris_irn") and frappe.db.has_column("POS Invoice", "efris_status"):
        extra_cols = ", efris_irn, efris_status"
    if status == "Draft":
        invoices = frappe.db.sql(
            f"""
            SELECT 
                name, pos_profile, currency, conversion_rate, invoice_printed, custom_printing_time, grand_total, restaurant_table, custom_merged_tables,
                cashier, waiter, net_total, posting_time, 
                total_taxes_and_charges, customer, customer_name, no_of_pax, creation, status, mobile_number,
                posting_date, rounded_total, order_type,
                custom_split_group, custom_split_from, custom_guest_label,
                custom_merged_pos_invoice, custom_merged_total,
                additional_discount_percentage, discount_amount, ury_client_order_id{extra_cols}
            FROM `tabPOS Invoice` 
            WHERE branch = %s AND status = %s 
            AND (invoice_printed = 1 OR (invoice_printed = 0 AND COALESCE(restaurant_table, '') = ''))
            ORDER BY modified desc
            LIMIT %s OFFSET %s
            """,
            (branch, status, limit,limit_start),
            as_dict=True,
        )
        updatedlist.extend(invoices)
    elif status == "Unbilled":
        
        docstatus = "Draft"
        invoices = frappe.db.sql(
            f"""
            SELECT 
                name, pos_profile, currency, conversion_rate, invoice_printed, custom_printing_time, grand_total, restaurant_table, custom_merged_tables,
                cashier, waiter, net_total, posting_time, 
                total_taxes_and_charges, customer, customer_name, no_of_pax, creation, status, mobile_number,
                posting_date, rounded_total, order_type,
                custom_split_group, custom_split_from, custom_guest_label,
                custom_merged_pos_invoice, custom_merged_total,
                additional_discount_percentage, discount_amount, ury_client_order_id{extra_cols}
            FROM `tabPOS Invoice` 
            WHERE branch = %s AND status = %s 
            AND (invoice_printed = 0 AND restaurant_table IS NOT NULL)
            ORDER BY modified desc
            LIMIT %s OFFSET %s
            """,
            (branch, docstatus, limit, limit_start),
            as_dict=True,
        )
        updatedlist.extend(invoices)
    elif status == "Recently Paid":
        docstatus = "Paid"
        invoices = frappe.db.sql(
            f"""
            SELECT 
                name, pos_profile, currency, conversion_rate, invoice_printed, custom_printing_time, grand_total, restaurant_table, custom_merged_tables,
                cashier, waiter, net_total, posting_time, 
                total_taxes_and_charges, customer, customer_name, no_of_pax, creation, status, mobile_number,
                posting_date, rounded_total, order_type, additional_discount_percentage,
                discount_amount, custom_split_group, custom_split_from, custom_guest_label,
                custom_merged_pos_invoice, custom_merged_total, ury_client_order_id{extra_cols}
            FROM `tabPOS Invoice` 
            WHERE branch = %s AND status = %s 
            ORDER BY modified desc
            LIMIT %s OFFSET %s
            """,
            (branch, docstatus, limit, limit_start),
            as_dict=True,
        )
        updatedlist.extend(invoices)    
    else:
        
        invoices = frappe.db.sql(
            f"""
            SELECT 
                name, pos_profile, currency, conversion_rate, invoice_printed, custom_printing_time, grand_total, restaurant_table, custom_merged_tables,
                cashier, waiter, net_total, posting_time, 
                total_taxes_and_charges, customer, customer_name, no_of_pax, creation, status, mobile_number,
                posting_date, rounded_total, order_type, additional_discount_percentage,
                discount_amount, custom_split_group, custom_split_from, custom_guest_label,
                custom_merged_pos_invoice, custom_merged_total, ury_client_order_id{extra_cols}
            FROM `tabPOS Invoice` 
            WHERE branch = %s AND status = %s 
            ORDER BY modified desc
            LIMIT %s OFFSET %s
            """,
            (branch, status, limit, limit_start),
            as_dict=True,
        )

        updatedlist.extend(invoices)
    if len(updatedlist) == limit and status != "Recently Paid":
            next = True
            updatedlist.pop()
    else:
            next = False
    updatedlist = _enrich_split_group_meta(updatedlist)
    return  { "data":updatedlist,"next":next}


@frappe.whitelist()
def searchPosInvoice(query,status):
    if not query:
        return {"data": [], "next": False}
    query = query.lower()
    filters = {"status": "Paid" if status == "Recently Paid" else status}
    
    try:
        branch = getBranch()
    except frappe.ValidationError:
        if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
            branch = None
        else:
            raise
            
    if branch:
        filters["branch"] = branch

    # Add additional conditions for Unbilled status
    if status == "Unbilled":
        filters.update({
            "status":"draft",
            "restaurant_table": ["not in", [None, ""]],  # Check if restaurant_table has value
            "invoice_printed": 0  # Check if invoice_printed is 0
        })
    search_fields = [
        "name",
        "pos_profile",
        "currency",
        "conversion_rate",
        "customer",
        "customer_name",
        "no_of_pax",
        "creation",
        "grand_total",
        "posting_date",
        "posting_time",
        "order_type",
        "restaurant_table",
        "custom_merged_tables",
        "status",
        "rounded_total",
        "net_total",
        "mobile_number",
        "invoice_printed",
        "custom_printing_time",
        "cashier",
        "waiter",
        "total_taxes_and_charges",
        "custom_split_group",
        "custom_split_from",
        "custom_guest_label",
        "ury_client_order_id",
        "custom_merged_pos_invoice",
        "custom_merged_total",
        "additional_discount_percentage",
        "discount_amount",
    ]
    if frappe.db.has_column("POS Invoice", "efris_irn") and frappe.db.has_column("POS Invoice", "efris_status"):
        search_fields.extend(["efris_irn", "efris_status"])
    pos_invoices = frappe.get_all(
        "POS Invoice",
        filters=filters,           
        or_filters=[
            ["name", "like", f"%{query}%"],
            ["customer", "like", f"%{query}%"],
            ["customer_name", "like", f"%{query}%"],
            ["mobile_number", "like", f"%{query}%"],
        ],
        fields=search_fields,
        limit_page_length=10 
    )
    pos_invoices = _enrich_split_group_meta(pos_invoices)
    
    return {"data": pos_invoices, "next": len(pos_invoices) == 10}
    

@frappe.whitelist()
def get_select_field_options():
    options = frappe.get_meta("POS Invoice").get_field("order_type").options
    if options:
        return [{"name": option} for option in options.split("\n")]
    else:
        return []


@frappe.whitelist()
def fav_items(customer):
    if not frappe.has_permission("Customer", "read", customer):
        frappe.throw(_("Not permitted to access this Customer"), frappe.PermissionError)

    filters = {"customer": customer}
    try:
        branch = getBranch()
        if branch:
            filters["branch"] = branch
    except frappe.exceptions.ValidationError:
        # Fallback if getBranch() throws (e.g., Administrator with no branch)
        pass

    pos_invoices = frappe.get_all(
        "POS Invoice", filters=filters, fields=["name"]
    )
    item_qty = {}

    for invoice in pos_invoices:
        pos_invoice = frappe.get_doc("POS Invoice", invoice.name)
        for item in pos_invoice.items:
            item_name = item.item_name
            qty = item.qty
            if item_name not in item_qty:
                item_qty[item_name] = 0
            item_qty[item_name] += qty

    favorite_items = [
        {"item_name": item_name, "qty": qty} for item_name, qty in item_qty.items()
    ]
    return favorite_items

@frappe.whitelist()
def getCashier(room):
    branch = getBranch()
    cashier = None
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
    if pos_opening_list:
        cashier = frappe.db.get_value(
            "POS Opening Entry",
            {"name": pos_opening_list[0].name},
            "user",)
    return cashier       
    

@frappe.whitelist()
def getPosProfile(pos_profile=None):
    authorized_outlets = []
    resolved_outlet = None
    try:
        from ury.ury_pos import outlet_context

        authorized_outlets = outlet_context.get_authorized_outlets(frappe.session.user)
        if authorized_outlets or pos_profile:
            resolved_outlet = outlet_context.resolve_active_pos_profile(
                pos_profile=pos_profile,
                user=frappe.session.user,
                allow_ambiguous=True,
            )
            if resolved_outlet is None and len(authorized_outlets) > 1:
                return {
                    "requires_outlet_selection": True,
                    "available_outlets": authorized_outlets,
                }
    except _permission_error_types():
        raise
    except Exception:
        authorized_outlets = []
        resolved_outlet = None

    if resolved_outlet:
        posProfile = resolved_outlet["pos_profile"]
        branchName = resolved_outlet["branch"]
    else:
        branchName = getBranch()
        posProfile = frappe.db.exists("POS Profile", {"branch": branchName})
    waiter = frappe.session.user
    bill_present = False
    qz_host = None
    printer = None
    cashier = None
    owner = None
    pos_profiles = frappe.get_doc("POS Profile", posProfile)
    global_defaults = frappe.get_single('Global Defaults')
    disable_rounded_total = global_defaults.disable_rounded_total
    

    if pos_profiles.branch == branchName:
        pos_profile_name = pos_profiles.name
        warehouse = pos_profiles.warehouse
        branch = pos_profiles.branch
        company = pos_profiles.company
        tableAttention = pos_profiles.table_attention_time
        get_cashier = frappe.get_doc("POS Profile", pos_profile_name)
        print_format = pos_profiles.print_format
        paid_limit=pos_profiles.paid_limit
        enable_discount = pos_profiles.custom_enable_discount
        allow_discount_change = cint(pos_profiles.get("allow_discount_change"))
        multiple_cashier = pos_profiles.custom_enable_multiple_cashier
        edit_order_type = pos_profiles.custom_edit_order_type
        enable_kot_reprint = pos_profiles.custom_enable_kot_reprint
        invoice_warning_time = pos_profiles.custom_invoice_warning_time or 0
        enable_payment_without_printing = pos_profiles.get("custom_enable_payment_without_printing") or 0
        disable_print_status_tracking = getattr(pos_profiles, "custom_disable_print_status_tracking", 0)
        if multiple_cashier:
            details = getBranchRoom()
            room = details[0].get('name') 
            branch = details[0].get('branch')

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
            if pos_opening_list:
                pos_opened_cashier = frappe.db.get_value(
                    "POS Opening Entry",
                    {"name": pos_opening_list[0].name},
                    "user",)
            else:
                pos_opened_cashier = None
            for user_details in get_cashier.applicable_for_users:
                if user_details.custom_main_cashier:
                    owner = user_details.user
                
                if frappe.session.user == owner:
                    cashier = owner
                else:
                    cashier = pos_opened_cashier    
                
        else:    
            cashier = get_cashier.applicable_for_users[0].user
            owner = get_cashier.applicable_for_users[0].user
        
        qz_print = pos_profiles.qz_print
        print_type = None

        printers = []
        for pos_profile in pos_profiles.printer_settings:
            
            if pos_profile.bill == 1:
                printers.append(pos_profile.printer)
                bill_present = True
                
        if printers:
            printer = ",".join(printers)

        if qz_print == 1:
            print_type = "qz"
            qz_host = pos_profiles.qz_host or "localhost"
            printer = pos_profiles.get("custom_qz_receipt_queue") or None

        elif bill_present == True:
            print_type = "network"

        else:
            print_type = "socket"

    payment_mode_accounts = {
        payment.mode_of_payment: frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": payment.mode_of_payment, "company": company},
            "default_account",
        )
        for payment in (pos_profiles.get("payments") or [])
    }

    room_lookup_method = (pos_profiles.get("custom_room_lookup_method") or "").strip()
    room_charge_modes, room_charge_unconfigured = _room_charge_config(
        payment_mode_accounts, company, room_lookup_method
    )
    from ury.ury_pos.offline_tenders import offline_tender_limit, profile_offline_payment_modes

    offline_payment_modes = profile_offline_payment_modes(
        pos_profiles, room_charge_modes=room_charge_modes, _frappe=frappe
    )
    till_offline_limit = offline_tender_limit(pos_profiles)
    from ury.ury_pos.approval_currency import company_currency_for, profile_approval_quote

    approval_currency = company_currency_for(company, frappe)
    approval_conversion_rate = None
    if cint(pos_profiles.get("ury_offline_capture")) and till_offline_limit > 0:
        _, quote = profile_approval_quote(pos_profiles, frappe, rate_type="for_selling")
        approval_conversion_rate = float(quote) if quote is not None else None


    try:
        from ury.ury_pos import outlet_context

        business_date_meta = outlet_context.get_business_date_and_report_meta(
            pos_profile_name, user=frappe.session.user
        )
    except Exception:
        business_date_meta = {
            "active_opening_entry": None,
            "active_business_date": None,
            "shift_report_available": False,
            "shift_z_print_format_available": False,
        }

    raw_block_open = pos_profiles.get("custom_block_clock_out_with_open_work")
    block_clock_out_with_open_work = 1 if raw_block_open in (None, "") else cint(raw_block_open)

    from ury.ury_pos.covers import covers_config

    invoice_details = {
        "pos_profile": pos_profile_name,
        "branch": branch,
        "company": company,
        "currency": pos_profiles.get("currency") or frappe.db.get_value("Company", company, "default_currency"),
        "country": getattr(pos_profiles, "country", None) or frappe.db.get_value("Company", company, "country"),
        "waiter": waiter,
        "warehouse": warehouse,
        "cashier": cashier,
        "print_format": print_format,
        "letter_head": pos_profiles.get("letter_head"),
        "qz_print": qz_print,
        "qz_host": qz_host,
        "printer": printer,
        "print_type": print_type,
        "tableAttention": tableAttention,
        "paid_limit":paid_limit,
        "disable_rounded_total":disable_rounded_total,
        "enable_discount":enable_discount,
        "allow_discount_change":allow_discount_change,
        "multiple_cashier":multiple_cashier,
        "owner":owner,
        "edit_order_type":edit_order_type,
        "enable_kot_reprint":enable_kot_reprint,
        "custom_invoice_warning_time":invoice_warning_time,
        "enable_payment_without_printing":enable_payment_without_printing,
        "custom_disable_print_status_tracking":disable_print_status_tracking,
        "floor_idle_signout_seconds": pos_profiles.get("custom_floor_idle_signout_seconds") or 0,
        "floor_signout_after_send": pos_profiles.get("custom_floor_signout_after_send") or 0,
        "custom_blind_cash_count": pos_profiles.get("custom_blind_cash_count") or 0,
        "payment_mode_accounts": payment_mode_accounts,
        "payment_mode_meta": {
            payment.mode_of_payment: {
                "type": frappe.db.get_value("Mode of Payment", payment.mode_of_payment, "type") or "",
                "overtender_rule": frappe.db.get_value(
                    "Mode of Payment", payment.mode_of_payment, "custom_overtender_rule"
                ) or "Refuse",
                "halo_max_amount": _safe_flt_value(
                    frappe.db.get_value("Mode of Payment", payment.mode_of_payment, "custom_halo_max_amount")
                ),
            }
            for payment in (pos_profiles.get("payments") or [])
        },
        "enable_cash_movements": cint(pos_profiles.get("custom_enable_cash_movements")),
        "require_over_short_reason": cint(pos_profiles.get("custom_require_over_short_reason")),
        "over_short_reason_tolerance": frappe.utils.flt(pos_profiles.get("custom_over_short_reason_tolerance")),
        "require_clock_in_to_order": cint(pos_profiles.get("custom_require_clock_in_to_order")),
        "block_clock_out_with_open_work": block_clock_out_with_open_work,
        "require_tip_declaration": cint(pos_profiles.get("custom_require_tip_declaration")),
        "requires_outlet_selection": False,
        "available_outlets_count": len(authorized_outlets) if authorized_outlets else 1,
        **business_date_meta,
        "over_short_tolerance": frappe.utils.flt(pos_profiles.get("custom_over_short_tolerance")),
        "cash_pull_threshold": frappe.utils.flt(pos_profiles.get("custom_cash_pull_threshold")),
        "paid_out_approval_threshold": (
            None
            if pos_profiles.get("custom_paid_out_approval_threshold") is None
            or (
                isinstance(pos_profiles.get("custom_paid_out_approval_threshold"), str)
                and not pos_profiles.get("custom_paid_out_approval_threshold").strip()
            )
            else frappe.utils.flt(pos_profiles.get("custom_paid_out_approval_threshold"))
        ),
        "paid_in_approval_threshold": frappe.utils.flt(pos_profiles.get("custom_paid_in_approval_threshold")),
        "paid_in_enabled": bool(pos_profiles.get("custom_paid_in_account")),
        "cash_drawer_id": (pos_profiles.get("custom_cash_drawer_id") or "").strip(),
        # the till's tenders come from configuration, never literals
        "default_payment_mode": next(
            (payment.mode_of_payment for payment in (pos_profiles.get("payments") or []) if payment.get("default")),
            None,
        ),
        "room_charge_modes": room_charge_modes,
        "room_charge_unconfigured": room_charge_unconfigured,
        # optional room lookup before a Room Charge; blank keeps free-text room
        "room_lookup_method": room_lookup_method,
        "approval_currency": approval_currency,
        "approval_conversion_rate": approval_conversion_rate,
        "offline_tender_limit": till_offline_limit,
        "offline_payment_modes": offline_payment_modes,
        "efris_print_wait_seconds": _efris_print_wait_seconds(pos_profiles),
        **_drawer_kick_profile_fields(pos_profiles),
        **covers_config(pos_profiles),
        **get_default_customer_config(pos_profiles),
        **_safe_order_type_tax_config(pos_profiles),
    }

    return invoice_details


def _room_charge_config(payment_mode_accounts, company, room_lookup_method=""):
    """Room-charge tenders and setup status from Company.custom_guest_ledger_clearing.

    The hotel bridge posts exactly the payment methods whose company account equals
    Company.custom_guest_ledger_clearing. When a site carries that field or configures a
    room lookup method without mapping the clearing account on this Company, report
    room_charge_unconfigured=True so the till informs staff instead of silently hiding
    the room prompt.
    """
    has_clearing_field = frappe.get_meta("Company").has_field("custom_guest_ledger_clearing")
    clearing = (
        frappe.db.get_value("Company", company, "custom_guest_ledger_clearing")
        if has_clearing_field
        else None
    )
    if clearing:
        return [mode for mode, account in payment_mode_accounts.items() if account == clearing], False
    return [], bool(has_clearing_field or room_lookup_method)


def _room_charge_modes(payment_mode_accounts, company):
    """The payment methods whose company account is the Company's Guest Ledger Clearing
    account. The hotel bridge posts exactly those to a guest folio, so the till asks for the
    room on them; a site without the hotel bridge has none."""
    modes, _ = _room_charge_config(payment_mode_accounts, company)
    return modes


@frappe.whitelist()
def getPosProfileFull(pos_profile):
    """Return the subset of POS Profile fields the POS frontend needs beyond
    getPosProfile()'s limited set (role-permission child tables, company/branch
    identity fields, etc). Cashier/Captain roles don't have doctype-level read
    permission on POS Profile, so the frontend can't fetch these via the raw
    `/api/resource/POS Profile/<name>` REST read - this whitelisted method
    reads server-side instead, scoped to the caller's own branch.
    """
    try:
        branch_name = getBranch(pos_profile=pos_profile)
    except TypeError:
        branch_name = getBranch()
    profile = frappe.get_doc("POS Profile", pos_profile)
    if profile.branch != branch_name:
        frappe.throw(frappe._("Not permitted to view this POS Profile"), frappe.PermissionError)

    try:
        from ury.ury_pos import outlet_context

        business_date_meta = outlet_context.get_business_date_and_report_meta(
            profile.name, user=frappe.session.user
        )
    except Exception:
        business_date_meta = {
            "active_opening_entry": None,
            "active_business_date": None,
            "shift_report_available": False,
            "shift_z_print_format_available": False,
        }

    raw_block_open = profile.get("custom_block_clock_out_with_open_work")
    block_clock_out_with_open_work = 1 if raw_block_open in (None, "") else cint(raw_block_open)
    from ury.ury_pos.offline_tenders import offline_tender_limit, profile_offline_payment_modes

    from ury.ury_pos.covers import covers_config

    return {
        "name": profile.name,
        **covers_config(profile),
        "owner": profile.owner,
        "creation": profile.creation,
        "modified": profile.modified,
        "modified_by": profile.modified_by,
        "docstatus": profile.docstatus,
        "idx": profile.idx,
        "company": profile.company,
        "customer": profile.customer,
        "country": profile.country or frappe.db.get_value("Company", profile.company, "country"),
        "disabled": profile.disabled,
        "warehouse": profile.warehouse,
        "campaign": profile.get("campaign"),
        "company_address": profile.company_address,
        "restaurant": profile.restaurant,
        "branch": profile.branch,
        "currency": profile.currency,
        "paid_limit": profile.paid_limit,
        "role_allowed_for_billing": [row.as_dict() for row in profile.role_allowed_for_billing],
        "role_restricted_for_table_order": [row.as_dict() for row in profile.role_restricted_for_table_order],
        "transfer_role_permissions": [row.as_dict() for row in profile.transfer_role_permissions],
        "role_masked_for_amounts": [row.as_dict() for row in (profile.get("role_masked_for_amounts") or [])],
        "custom_floor_stay_signed_in_roles": [
            {"role": row.role} for row in (profile.get("custom_floor_stay_signed_in_roles") or [])
        ],
        "view_all_status": profile.get("view_all_status"),
        "custom_daily_pos_close": profile.get("custom_daily_pos_close"),
        "print_format": profile.print_format,
        "letter_head": profile.get("letter_head"),
        "enable_payment_without_printing": profile.get("custom_enable_payment_without_printing") or 0,
        "custom_enable_payment_without_printing": profile.get("custom_enable_payment_without_printing") or 0,
        "ury_enable_hold_fire": cint(profile.get("ury_enable_hold_fire")),
        "default_fire_delay_minutes": cint(profile.get("default_fire_delay_minutes")) or 10,
        "fire_time_zone": frappe.utils.get_system_timezone(),
        "ury_offline_capture": 1 if profile.get("ury_offline_capture") else 0,
        "custom_offline_peer_relay_url": profile.get("custom_offline_peer_relay_url") or "",
        "custom_offline_collision_policy": profile.get("custom_offline_collision_policy") or "Auto Split Check",
        "custom_allow_fx_rate_override": 1 if profile.get("custom_allow_fx_rate_override") else 0,
        "custom_max_fx_variance_pct": frappe.utils.flt(profile.get("custom_max_fx_variance_pct")),
        "offline_tender_limit": offline_tender_limit(profile),
        "offline_payment_modes": profile_offline_payment_modes(profile, _frappe=frappe),
        # native partial-payment switch; the till lets only a named account settle short
        "allow_partial_payment": 1 if profile.get("allow_partial_payment") else 0,
        # native ERPNext open-discount switch (default 0); gates Open amount/percent keypad
        "allow_discount_change": 1 if profile.get("allow_discount_change") else 0,
        "allow_rate_change": 1 if profile.get("allow_rate_change") else 0,
        "custom_max_free_bill_prints": cint(profile.get("custom_max_free_bill_prints")) if profile.get("custom_max_free_bill_prints") is not None else 1,
        "custom_max_free_closed_copies": cint(profile.get("custom_max_free_closed_copies") or 0),
        "custom_service_charge_rate": flt(profile.get("custom_service_charge_rate") or 0),
        "custom_service_charge_account": profile.get("custom_service_charge_account") or "",
        "custom_service_charge_min_pax": cint(profile.get("custom_service_charge_min_pax") or 0),
        "custom_service_charge_order_types": profile.get("custom_service_charge_order_types") or "",
        "custom_service_charge_taxable": 1 if profile.get("custom_service_charge_taxable") else 0,
        "custom_service_charge_description": profile.get("custom_service_charge_description") or "",
        "custom_service_charge_rules": [
            {
                "order_type": row.get("order_type") or "",
                "enabled": cint(row.get("enabled") if row.get("enabled") is not None else 1),
                "charge_mode": row.get("charge_mode") or "Percentage",
                "rate": flt(row.get("rate") or 0),
                "fixed_amount": flt(row.get("fixed_amount") or 0),
                "min_amount_floor": flt(row.get("min_amount_floor") or 0),
                "min_pax": cint(row.get("min_pax") or 0),
                "calculation_basis": row.get("calculation_basis") or "Post-Discount Subtotal",
                "taxable": cint(row.get("taxable") or 0),
                "account_head": row.get("account_head") or "",
                "description": row.get("description") or "",
                "efris_item_code": row.get("efris_item_code") or "",
            }
            for row in (profile.get("custom_service_charge_rules") or [])
        ],
        "custom_tax_exempt_category": profile.get("custom_tax_exempt_category") or "",
        "custom_tips_payable_account": profile.get("custom_tips_payable_account") or "",
        # one configured void reason per line; None on sites without the field
        "custom_void_reasons": profile.get("custom_void_reasons"),
        "require_over_short_reason": cint(profile.get("custom_require_over_short_reason")),
        "over_short_reason_tolerance": frappe.utils.flt(profile.get("custom_over_short_reason_tolerance")),
        "require_clock_in_to_order": cint(profile.get("custom_require_clock_in_to_order")),
        "block_clock_out_with_open_work": block_clock_out_with_open_work,
        "require_tip_declaration": cint(profile.get("custom_require_tip_declaration")),
        **business_date_meta,
        "efris_print_wait_seconds": _efris_print_wait_seconds(profile),
        **_drawer_kick_profile_fields(profile),
        **get_default_customer_config(profile),
        **_safe_order_type_tax_config(profile),
    }


@frappe.whitelist()
def getPosInvoiceItems(invoice):
    itemDetails = []
    taxDetails = []
    orderdItems = frappe.get_doc("POS Invoice", invoice)
    
    if not frappe.has_permission("POS Invoice", "read", doc=orderdItems):
        frappe.throw(frappe._("Not permitted to view this order"), frappe.PermissionError)
        
    user_branch = getBranch()
    if orderdItems.branch and user_branch and orderdItems.branch != user_branch:
        frappe.throw(frappe._("Not permitted to view orders outside your active branch"), frappe.PermissionError)

    from ury.ury.doctype.ury_order.ury_order import check_split_target_access

    check_split_target_access(orderdItems, "read")
    posItems = orderdItems.items
    for items in posItems:
        max_disc = 0.0
        if getattr(items, "item_code", None):
            try:
                max_disc = frappe.utils.flt(frappe.get_cached_value("Item", items.item_code, "max_discount"))
            except Exception:
                max_disc = 0.0
        itemDetails.append(
            {
                "name": items.name,
                "item_code": getattr(items, "item_code", None),
                "item_name": items.item_name,
                "qty": items.qty,
                "rate": items.rate,
                "price_list_rate": getattr(items, "price_list_rate", items.rate),
                "amount": items.amount,
                "discount_percentage": frappe.utils.flt(getattr(items, "discount_percentage", 0)),
                "discount_amount": frappe.utils.flt(getattr(items, "discount_amount", 0)),
                "max_discount": max_disc,
                "is_disposable": items.is_disposable,
                "comment": items.get("comment") or "",
                "custom_course": items.get("custom_course"),
                "custom_seat_no": cint(items.get("custom_seat_no")),
                "custom_is_shareable": cint(frappe.get_cached_value("Item", items.item_code, "custom_is_shareable"))
                if items.item_code else 0,
                "reservation_line_key": items.get("reservation_line_key") or None,
                "custom_parent_line_key": items.get("custom_parent_line_key") or None,
                "custom_line_role": items.get("custom_line_role") or "Standard",
                "custom_is_modifier": cint(items.get("custom_is_modifier")),
                "custom_modifier_prefix": items.get("custom_modifier_prefix") or "",
                "custom_modifier_group": items.get("custom_modifier_group") or None,
                "custom_modifier_option": items.get("custom_modifier_option") or None,
                "custom_modifiers_json": items.get("custom_modifiers_json") or "",
                "custom_bundle_overrides_json": items.get("custom_bundle_overrides_json") or "",
                "custom_inherit_parent_routing": cint(items.get("custom_inherit_parent_routing", 1)),
                "custom_hide_unpriced_on_receipt": cint(items.get("custom_hide_unpriced_on_receipt")),
            }
        )
    taxDetail = orderdItems.taxes
    has_discount = bool(
        flt(getattr(orderdItems, "discount_amount", 0))
        or flt(getattr(orderdItems, "additional_discount_percentage", 0))
    )
    for tax in taxDetail:
        description = tax.description
        get_t = tax.get if callable(getattr(tax, "get", None)) else (lambda k, d=None: getattr(tax, k, d))
        raw_amt = flt(get_t("tax_amount"))
        after_amt = get_t("tax_amount_after_discount_amount")
        if after_amt is not None and (flt(after_amt) != 0 or has_discount or raw_amt == 0):
            rate = flt(after_amt)
        else:
            rate = raw_amt
        taxDetails.append(
            {
                "description": description,
                "rate": rate,
                "tax_amount": flt(rate),
                "tax_rate": flt(get_t("rate") or 0),
                "charge_type": get_t("charge_type") or "",
                "custom_pos_charge_kind": get_t("custom_pos_charge_kind") or "",
                "account_head": get_t("account_head") or "",
            }
        )
    paymentDetails = []
    room_number = orderdItems.get("room_number")
    room_modes = None
    pr_filters = {
        "reference_doctype": "POS Invoice",
        "reference_name": orderdItems.name,
        "docstatus": 1,
        "payment_channel": "Phone",
        "payment_request_type": "Inward",
        "status": ["in", ["Paid", "Partially Paid", "Partially Refunded"]],
    }
    if orderdItems.get("company"):
        pr_filters["company"] = orderdItems.get("company")
    paid_requests = frappe.get_all(
        "Payment Request",
        filters=pr_filters,
        fields=["name", "mode_of_payment", "payment_account", "grand_total", "outstanding_amount", "status"],
        order_by="creation asc",
    )
    if not isinstance(paid_requests, (list, tuple)):
        paid_requests = []
    live_prs = [
        pr
        for pr in paid_requests
        if (frappe.utils.flt(pr.get("grand_total")) - frappe.utils.flt(pr.get("outstanding_amount"))) > 0
    ]
    live_pr_by_name = {pr.get("name"): pr for pr in live_prs if pr.get("name")}
    live_paid_by_mode = {}
    for pr in live_prs:
        pr_mode = pr.get("mode_of_payment")
        pr_paid = frappe.utils.flt(pr.get("grand_total")) - frappe.utils.flt(pr.get("outstanding_amount"))
        if pr_mode and pr_paid > 0:
            live_paid_by_mode[pr_mode] = live_paid_by_mode.get(pr_mode, 0.0) + pr_paid

    received_rows = []
    matched_pr_names = set()
    matched_unreferenced_phone_modes = {}
    for payment in orderdItems.get("payments") or []:
        amt = frappe.utils.flt(payment.get("amount"))
        if amt <= 0 or frappe.utils.cint(payment.get("custom_tender_received")) != 1:
            continue
        mode = payment.get("mode_of_payment")
        ref = (payment.get("reference_no") or "").strip()
        is_phone = (
            payment.get("type") == "Phone"
            or (bool(mode) and frappe.db.get_value("Mode of Payment", mode, "type") == "Phone")
        )
        if is_phone:
            if ref:
                pr = live_pr_by_name.get(ref)
                if not pr:
                    continue
                # Rounded so binary float noise (5.9 - 2.0) never reaches the payment screen.
                live_amt = round(
                    frappe.utils.flt(pr.get("grand_total")) - frappe.utils.flt(pr.get("outstanding_amount")), 9
                )
                if live_amt <= 0:
                    continue
                matched_pr_names.add(ref)
                if mode:
                    live_paid_by_mode[mode] = max(0.0, live_paid_by_mode.get(mode, 0.0) - live_amt)
                received_rows.append(
                    {
                        "mode_of_payment": mode,
                        "amount": live_amt,
                        "reference_no": ref,
                        "reference_date": payment.get("reference_date"),
                        "clearance_date": payment.get("clearance_date"),
                        "type": "Phone",
                        "custom_tender_received": 1,
                    }
                )
            else:
                avail = live_paid_by_mode.get(mode, 0.0) if mode else 0.0
                if not mode or avail <= 0:
                    continue
                used = min(amt, avail)
                live_paid_by_mode[mode] = max(0.0, avail - used)
                matched_unreferenced_phone_modes[mode] = matched_unreferenced_phone_modes.get(mode, 0.0) + used
                received_rows.append(
                    {
                        "mode_of_payment": mode,
                        "amount": used,
                        "reference_no": payment.get("reference_no"),
                        "reference_date": payment.get("reference_date"),
                        "clearance_date": payment.get("clearance_date"),
                        "type": "Phone",
                        "custom_tender_received": 1,
                    }
                )
            continue

        received_rows.append(payment)
        if ref:
            matched_pr_names.add(ref)

    for pr in live_prs:
        pr_name = pr.get("name")
        if pr_name in matched_pr_names:
            continue
        paid_amt = frappe.utils.flt(pr.get("grand_total")) - frappe.utils.flt(pr.get("outstanding_amount"))
        mode = pr.get("mode_of_payment")
        if not mode or paid_amt <= 0:
            continue
        if matched_unreferenced_phone_modes.get(mode, 0.0) >= paid_amt:
            matched_unreferenced_phone_modes[mode] -= paid_amt
            continue
        received_rows.append(
            {
                "mode_of_payment": mode,
                "amount": paid_amt,
                "reference_no": pr_name,
                "type": "Phone",
                "custom_tender_received": 1,
            }
        )

    for payment in received_rows:
        amt = frappe.utils.flt(payment.get("amount"))
        if amt <= 0:
            continue
        entry = {
            "mode_of_payment": payment.get("mode_of_payment"),
            "amount": amt,
        }
        if payment.get("reference_no"):
            entry["reference_no"] = payment.get("reference_no")
        if payment.get("reference_date"):
            entry["reference_date"] = str(payment.get("reference_date"))
        if payment.get("clearance_date"):
            entry["clearance_date"] = str(payment.get("clearance_date"))
        if payment.get("custom_tender_currency"):
            entry["custom_tender_currency"] = payment.get("custom_tender_currency")
            entry["custom_tender_amount"] = frappe.utils.flt(payment.get("custom_tender_amount"))
            entry["custom_exchange_rate"] = frappe.utils.flt(payment.get("custom_exchange_rate"))
            if payment.get("custom_exchange_rate_date"):
                entry["custom_exchange_rate_date"] = str(payment.get("custom_exchange_rate_date"))
        if room_number:
            if room_modes is None:
                payment_mode_accounts = {
                    row.get("mode_of_payment"): frappe.db.get_value(
                        "Mode of Payment Account",
                        {"parent": row.get("mode_of_payment"), "company": orderdItems.company},
                        "default_account",
                    )
                    for row in received_rows
                    if row.get("mode_of_payment")
                }
                room_modes = set(_room_charge_modes(payment_mode_accounts, orderdItems.company))
            if payment.get("mode_of_payment") in room_modes:
                entry["room_number"] = room_number
        paymentDetails.append(entry)
    return itemDetails, taxDetails, paymentDetails


@frappe.whitelist()
def posOpening():
    branchName = getBranch()
    pos_opening_list = frappe.get_all(
        "POS Opening Entry",
        fields=["name", "docstatus", "status", "posting_date"],
        filters={"branch": branchName},
    )
    flag = 1
    for pos_opening in pos_opening_list:
        if pos_opening.status == "Open" and pos_opening.docstatus == 1:
            flag = 0
    if flag == 1:
        frappe.msgprint(title="Message", indicator="red", msg=("Please Open POS Entry"))
    return flag


@frappe.whitelist()
def getAggregator():
    branchName = getBranch()
    aggregatorList = frappe.get_all(
        "Aggregator Settings",
        fields=["customer"],
        filters={"parent": branchName, "parenttype": "Branch"},
    )
    return aggregatorList


@frappe.whitelist()
def getAggregatorItem(aggregator):
    branchName = getBranch()
    aggregatorItem = []
    aggregatorItemList = []
    priceList = frappe.db.get_value(
        "Aggregator Settings",
        {"customer": aggregator, "parent": branchName, "parenttype": "Branch"},
        "price_list",
    )
    aggregatorItem = frappe.get_all(
        "Item Price",
        fields=["item_code", "item_name"],
        filters={"selling": 1, "price_list": priceList},
    )
    # List each item once, at the rate sync_order bills the aggregator (price_items_for_invoice).
    names = {}
    for item in aggregatorItem:
        names.setdefault(item.item_code, item.item_name)
    release_field, released = item_price_release_states()
    today = frappe.utils.getdate()
    for item_code, item_name in names.items():
        if frappe.db.get_value("Item", item_code, "disabled"):
            continue
        price = price_valid_on(item_code, priceList, today, release_field, released, customer=aggregator)
        if not price:
            continue
        aggregatorItemList.append({
            "item": item_code,
            "item_name": item_name,
            "rate": price.price_list_rate,
            "item_image": frappe.db.get_value("Item", item_code, "image"),
        })
    return aggregatorItemList

@frappe.whitelist()
def getAggregatorMOP(aggregator):
    branchName = getBranch()
    
    modeOfPayment = frappe.db.get_value(
        "Aggregator Settings",
        {"customer": aggregator, "parent": branchName, "parenttype": "Branch"},
        "mode_of_payments",
    )
    modeOfPaymentsList = []
    modeOfPaymentsList.append(
            {"mode_of_payment": modeOfPayment, "opening_amount": float(0)}
    )
    return modeOfPaymentsList
def validate_customer_mobile(mobile_number, country=None):
    from frappe.geo.country_info import get_all
    import phonenumbers

    mobile_number = str(mobile_number or "").strip()
    if not mobile_number:
        frappe.throw(_("Mobile Number is required"))

    # Also run standard validate_phone_number to preserve existing mock expectations
    try:
        validate_phone_number(mobile_number, throw=True)
    except Exception:
        frappe.throw(_("Invalid mobile number format"))

    country_code = None
    calling_code = None
    if country:
        all_info = get_all()
        c_info = all_info.get(country) or {}
        country_code = (c_info.get("code") or "").upper() or None
        calling_code = c_info.get("isd")

    clean_mobile = mobile_number.replace(" ", "")
    if calling_code and clean_mobile in (calling_code, calling_code.lstrip("+")):
        frappe.throw(_("Mobile Number is required"))

    is_valid = False
    try:
        parsed = phonenumbers.parse(mobile_number, country_code)
        is_valid = phonenumbers.is_valid_number(parsed)
    except Exception:
        is_valid = False

    if not is_valid:
        error_msg = _("Invalid mobile number {0} for {1}").format(mobile_number, country) if country else _("Invalid mobile number format")
        frappe.throw(error_msg)


@frappe.whitelist()
def create_customer(customer_name, mobile_number=None, customer_group=None, territory=None, pos_profile=None):
    if not frappe.has_permission("Customer", "create"):
        frappe.throw(_("Not permitted to create customers"), frappe.PermissionError)

    if not customer_name:
        frappe.throw(_("Customer name is required"))
    if not mobile_number:
        frappe.throw(_("Mobile Number is required"))

    country = None
    if pos_profile:
        country = frappe.db.get_value("POS Profile", pos_profile, "country")
        if not country:
            company = frappe.db.get_value("POS Profile", pos_profile, "company")
            if company:
                country = frappe.db.get_value("Company", company, "country")

    if not country:
        try:
            branch = getBranch()
            if branch:
                pos_profile_name = frappe.db.get_value("POS Profile", {"branch": branch}, "name")
                if pos_profile_name:
                    country = frappe.db.get_value("POS Profile", pos_profile_name, "country")
                    if not country:
                        company = frappe.db.get_value("POS Profile", pos_profile_name, "company")
                        if company:
                            country = frappe.db.get_value("Company", company, "country")
        except Exception:
            pass

    if not country:
        country = frappe.db.get_single_value("System Settings", "country")

    validate_customer_mobile(mobile_number, country)

    customer_group = customer_group or frappe.db.get_single_value("Selling Settings", "customer_group")
    if not customer_group:
        frappe.throw(_("Please set Default Customer Group in Selling Settings."))

    try:
        territory = territory or frappe.db.get_single_value("Selling Settings", "territory") or nestedset.get_root_of("Territory")
        customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": customer_name,
            "mobile_number": mobile_number,
            "customer_group": customer_group,
            "territory": territory
        })
        customer.insert(ignore_permissions=False)
        frappe.db.commit()

        return {
            "status": "success",
            "message": "Customer created successfully",
            # Additive link id — distinct from display `customer_name` when
            # naming series ≠ customer_name (e.g. CUST-00042 vs "Alice").
            "name": customer.name,
            "customer_name": customer_name,
            "mobile_number": mobile_number,
            "customer_group": customer_group,
            "territory": territory
        }

    except Exception as e:
        frappe.log_error(message=frappe.get_traceback(), title="Customer Creation Failed")
        return {
            "status": "error",
            "message": str(e)
        }

def _till_invoice_rows(pos_profile, start, end, reader):
    """Select submitted, unconsolidated till invoices in the inclusive window."""
    start = frappe.utils.get_datetime(start)
    end = frappe.utils.get_datetime(end)
    rows = reader(
        "POS Invoice",
        filters={
            "docstatus": 1,
            "pos_profile": pos_profile,
            "consolidated_invoice": ["is", "not set"],
            "posting_date": ["between", [start.date(), end.date()]],
        },
        fields=[
            "name", "posting_date", "posting_time", "customer", "grand_total",
            "net_total", "total_qty", "total_taxes_and_charges", "is_return", "return_against",
        ],
        order_by="posting_date asc, posting_time asc",
        limit_page_length=0,
    )
    invoices = []
    for row in rows:
        # MariaDB returns posting_time as a timedelta, not a datetime.time.
        timestamp = datetime.combine(
            frappe.utils.get_datetime(row.posting_date).date(),
            frappe.utils.get_time(row.posting_time),
        )
        if start <= timestamp <= end:
            invoices.append(row)
    return invoices


@frappe.whitelist(methods=["GET"])
def get_till_invoices(start, end, pos_profile, user):
    """Return full invoices for the holder's open till, regardless of cashier.

    The opening, not the client-supplied start, defines the beginning of the
    inclusive period. Only the opening holder, a URY Manager or a System
    Manager may read it; invoice reads keep the caller's list permissions.
    """
    if frappe.session.user != user and not (
        {"URY Manager", "System Manager"} & set(frappe.get_roles(frappe.session.user))
    ):
        frappe.throw(coded_message("TILL-NOT-HOLDER", _("Not permitted")), frappe.PermissionError)

    openings = frappe.get_list(
        "POS Opening Entry",
        filters={"docstatus": 1, "status": "Open", "pos_profile": pos_profile, "user": user},
        fields=["period_start_date"],
        order_by="period_start_date desc",
        limit_page_length=1,
    )
    if not openings:
        frappe.throw(coded_message("TILL-NO-OPEN-SESSION", _("Not permitted")), frappe.PermissionError)

    def reader(doctype, **query):
        # Full documents are read below; keep the endpoint's narrow query.
        query["fields"] = ["name", "posting_date", "posting_time"]
        return frappe.get_list(doctype, **query)

    rows = _till_invoice_rows(pos_profile, openings[0].period_start_date, end, reader)
    return [frappe.get_doc("POS Invoice", row.name).as_dict() for row in rows]


@frappe.whitelist()
def get_open_pos_opening_entries(pos_profile):
    """Currently open (status=Open, submitted) POS Opening Entries for the
    given POS Profile. Backs the React Closing flow (pos-closing-api.ts's
    getOpenPosOpeningEntries) so it can find the requesting user's own
    session -- referenced there since the POSClosingDialog work landed, but
    never actually implemented until now (found via a live E2E test: the
    Close Shift dialog failed outright with "no attribute
    'get_open_pos_opening_entries'").

    Scoped by branch (the POS Profile must belong to the session user's
    branch) and, for non-supervisors, by user (only their own open entries
    are returned) -- matches this file's existing getBranch()/System Manager
    supervisor-bypass convention used elsewhere (e.g. searchPosInvoice,
    fav_items).
    """
    if not frappe.has_permission("POS Opening Entry", "read"):
        frappe.throw(_("Not permitted to view POS Opening Entries."), frappe.PermissionError)

    session_user = frappe.session.user
    is_supervisor = session_user == "Administrator" or "System Manager" in frappe.get_roles()

    try:
        session_branch = getBranch()
    except frappe.ValidationError:
        if is_supervisor:
            # Supervisors may not be mapped to a branch
            session_branch = None
        else:
            raise

    profile_branch = frappe.db.get_value("POS Profile", pos_profile, "branch")

    if not profile_branch:
        frappe.throw(
            _("POS Profile {0} not found.").format(pos_profile),
            frappe.DoesNotExistError,
        )

    if session_branch and profile_branch != session_branch:
        frappe.throw(
            _("You do not have permission to access opening entries for POS Profile {0}.").format(
                pos_profile
            ),
            frappe.PermissionError,
        )

    filters = {
        "pos_profile": pos_profile,
        "status": "Open",
        "docstatus": 1,
    }

    # Non-supervisors only see their own open entries
    if not is_supervisor:
        filters["user"] = session_user

    return frappe.get_all(
        "POS Opening Entry",
        filters=filters,
        fields=["name", "period_start_date", "user", "pos_profile"],
    )


@frappe.whitelist()
def validate_pos_close(pos_profile):
    enable_unclosed_pos_check = frappe.db.get_value("POS Profile",pos_profile,"custom_daily_pos_close")
    
    if enable_unclosed_pos_check:
        from ury.ury.api.ury_dashboard import _business_day_bounds

        branch = frappe.db.get_value("POS Profile", pos_profile, "branch")
        start_of_day, _end = _business_day_bounds(
            branch, default_hours=5, now=frappe.utils.now_datetime()
        )

        # Any session opened before the current business day began is
        # overdue, however many days it has been left open.
        unclosed_pos_opening = frappe.db.exists(
            "POS Opening Entry",
            {
                "period_start_date": ["<", start_of_day],
                "status": "Open",
                "pos_profile": pos_profile,
                "docstatus": 1
            }
        )
    
        if unclosed_pos_opening:
            return "Failed"
        
        return "Success"
    
    return "Success"


def _get_allowed_pos_profiles(company: str, user: str) -> list:
    """Return POS Profiles the user may open for the given company.

    Replicates ``erpnext.accounts.doctype.pos_profile.pos_profile.pos_profile_query``:
    non-disabled profiles for the company where the user is listed in
    ``applicable_for_users`` or where ``applicable_for_users`` is empty.

    Returns a list of ``{"name": ..., "label": ...}`` dicts suitable for the
    ORI POS Opening screen.
    """
    if not company or not user:
        return []

    profiles = frappe.get_all(
        "POS Profile",
        filters={"company": company, "disabled": 0},
        fields=["name"],
    )
    if not profiles:
        return []

    profile_names = [p["name"] for p in profiles]

    # Bulk-fetch applicable users for all candidate profiles.
    user_rows = frappe.get_all(
        "POS Profile User",
        filters={"parent": ["in", profile_names], "parenttype": "POS Profile"},
        fields=["parent", "user"],
    )

    profile_users = {}
    for row in user_rows:
        profile_users.setdefault(row["parent"], set()).add(row["user"])

    allowed_names = [
        name
        for name in profile_names
        if not profile_users.get(name) or user in profile_users[name]
    ]

    if not allowed_names:
        return []

    return [{"name": name, "label": name} for name in sorted(allowed_names)]


def _get_main_cashier_status(pos_profile_name: str) -> dict:
    """Return multi-cashier status for the given POS Profile.

    Returns ``{"enabled": bool, "main_cashier_configured": bool,
    "main_cashier_open": bool}``.
    """
    if not pos_profile_name:
        return {"enabled": False, "main_cashier_configured": False, "main_cashier_open": False}

    try:
        pos_profile_doc = frappe.get_doc("POS Profile", pos_profile_name)
        enabled = bool(pos_profile_doc.custom_enable_multiple_cashier)

        main_cashier = None
        for user_row in pos_profile_doc.applicable_for_users:
            if user_row.custom_main_cashier:
                main_cashier = user_row.user
                break

        main_cashier_configured = bool(main_cashier)
        main_cashier_open = False
        if main_cashier:
            today = frappe.utils.today()
            main_cashier_open = bool(
                frappe.db.exists(
                    "POS Opening Entry",
                    {
                        "user": main_cashier,
                        "pos_profile": pos_profile_name,
                        "posting_date": today,
                        "status": "Open",
                        "docstatus": 1,
                    },
                )
            )

        return {
            "enabled": enabled,
            "main_cashier_configured": main_cashier_configured,
            "main_cashier_open": main_cashier_open,
        }
    except Exception:
        return {"enabled": False, "main_cashier_configured": False, "main_cashier_open": False}


@frappe.whitelist()
def create_pos_opening_entry(pos_profile: str, company: str = None, balance_details=None) -> dict:
    """Create and submit a POS Opening Entry for the ORI native screen.

    Wraps the standard ERPNext flow but fills URY-mandatory fields
    (branch, restaurant, and company) from the selected POS Profile so ORI
    users do not need to leave the React app, and so a caller cannot submit
    an Opening Entry against a company the POS Profile isn't actually
    configured for.

    ``balance_details`` may be a JSON string (legacy Desk shape) or a list of
    ``{"mode_of_payment": ..., "opening_amount": ...}`` dicts.
    """
    if not frappe.has_permission("POS Opening Entry", "create"):
        frappe.throw(_("Not permitted to create POS Opening Entry"), frappe.PermissionError)

    if not frappe.has_permission("POS Opening Entry", "submit"):
        frappe.throw(_("Not permitted to submit POS Opening Entry"), frappe.PermissionError)

    if isinstance(balance_details, str):
        balance_details = json.loads(balance_details)

    pos_profile_doc = frappe.get_doc("POS Profile", pos_profile)

    if not pos_profile_doc.branch:
        frappe.throw(_("Selected POS Profile has no Branch."))
    if not pos_profile_doc.restaurant:
        frappe.throw(_("Selected POS Profile has no Restaurant."))
    if not pos_profile_doc.company:
        frappe.throw(_("Selected POS Profile has no Company."))

    if not frappe.has_permission("POS Profile", "read", doc=pos_profile_doc):
        frappe.throw(_("Not permitted to use this POS Profile."), frappe.PermissionError)

    # G43: prevent opening a second till on another outlet while the user holds an open shift elsewhere.
    from ury.ury_pos import outlet_context

    outlet_context.assert_no_conflicting_open_till(pos_profile, user=frappe.session.user)

    # G36: optional clock-in gate before opening a shift.
    if cint(pos_profile_doc.get("custom_require_clock_in_to_order")) == 1:
        from ury.ury_pos import timeclock

        timeclock.enforce_clock_in_for_profile(pos_profile_doc, user=frappe.session.user)

    # U24: derive company from the resolved POS Profile the same way
    # branch/restaurant already are, rather than trusting a caller-supplied
    # value that may not match the profile at all.
    company = pos_profile_doc.company

    for entry in balance_details or []:
        opening_amount = entry.get("opening_amount") if isinstance(entry, dict) else None
        if opening_amount is not None and frappe.utils.flt(opening_amount) < 0:
            frappe.throw(_("Opening amount cannot be negative."))

    opening = frappe.get_doc(
        {
            "doctype": "POS Opening Entry",
            "period_start_date": frappe.utils.get_datetime(),
            "posting_date": frappe.utils.getdate(),
            "user": frappe.session.user,
            "company": company,
            "pos_profile": pos_profile,
            "branch": pos_profile_doc.branch,
            "restaurant": pos_profile_doc.restaurant,
        }
    )
    opening.set("balance_details", balance_details)
    opening.submit()

    return opening.as_dict()


@frappe.whitelist()
def get_pos_opening_screen_data(pos_profile: str | None = None) -> dict:
    """Return the full context needed by the ORI native POS Opening screen.

    This is a read-only, permission-aware context call. It aggregates the
    current user/company, the POS Profiles the user may open, the active
    POS Profile data (including branch, restaurant and multi-cashier flags),
    payment modes seeded with an opening amount of zero, the daily-close
    pre-check status, create/submit permission flags, and any existing open
    POS Opening Entry for the current user. Also reports another cashier's
    open entry on the selected profile (holder name and start, never the
    email), with its ID only when readable, and whether this user may
    continue on it.

    The method does not create or mutate any document; submit-time
    validations (payment accounts, duplicate entries, multi-cashier rules)
    remain on the server.
    """
    user = frappe.session.user

    # Resolve the cashier's branch-assigned POS Profile first so multi-company
    # tills (e.g. a UGX branch on a USD-default site, or a cashier without a
    # User Default Company) bind to their own profile's company and currency.
    branch_profile_data = None
    try:
        branch_profile_data = getPosProfile()
    except Exception:
        branch_profile_data = None

    branch_company = (
        branch_profile_data.get("company")
        if isinstance(branch_profile_data, dict)
        else None
    )

    # Company resolution: assigned branch POS Profile company first, then user
    # default, then global default (Frappe stores Global Defaults under "company").
    company = branch_company
    if not company:
        company = frappe.defaults.get_user_default("Company")
    if not company:
        company = frappe.db.get_default("Company") or frappe.db.get_default("company")

    allowed_profiles = _get_allowed_pos_profiles(company, user)

    # Resolve the active POS Profile. Prefer the existing URY helper; fall
    # back to the first allowed profile if the helper cannot resolve one.
    pos_profile_data = None
    pos_profile_name = None
    if pos_profile:
        if pos_profile not in {profile["name"] for profile in allowed_profiles}:
            frappe.throw(
                coded_message("OPENING-PROFILE-NOT-ALLOWED", _("Not permitted to use this POS Profile.")),
                frappe.PermissionError,
            )
        pos_profile_name = pos_profile
        if (
            isinstance(branch_profile_data, dict)
            and branch_profile_data.get("pos_profile") == pos_profile
        ):
            pos_profile_data = branch_profile_data
    elif isinstance(branch_profile_data, dict):
        pos_profile_data = branch_profile_data
        pos_profile_name = branch_profile_data.get("pos_profile")

    if not pos_profile_name and allowed_profiles:
        pos_profile_name = allowed_profiles[0]["name"]

    pos_profile_doc = None
    if pos_profile_name:
        try:
            pos_profile_doc = frappe.get_doc("POS Profile", pos_profile_name)
        except Exception:
            pos_profile_doc = None

    if pos_profile_name and not pos_profile_data and pos_profile_doc is not None:
        pos_profile_data = {
            "pos_profile": pos_profile_doc.name,
            "branch": pos_profile_doc.branch,
            "company": pos_profile_doc.company,
            "currency": (
                pos_profile_doc.get("currency")
                if callable(getattr(pos_profile_doc, "get", None))
                else getattr(pos_profile_doc, "currency", None)
            ),
            "restaurant": pos_profile_doc.restaurant,
            "warehouse": pos_profile_doc.warehouse,
            "cashier": user,
            "multiple_cashier": pos_profile_doc.custom_enable_multiple_cashier,
            "custom_daily_pos_close": pos_profile_doc.custom_daily_pos_close,
        }

    doc_company = getattr(pos_profile_doc, "company", None) if pos_profile_doc is not None else None
    if not company and isinstance(doc_company, str) and doc_company:
        company = doc_company
        allowed_profiles = _get_allowed_pos_profiles(company, user)

    # Payment modes with opening_amount default 0.
    payment_modes = []
    if pos_profile_doc is not None:
        try:
            payment_modes = [
                {"mode_of_payment": mop.mode_of_payment, "opening_amount": 0.0}
                for mop in pos_profile_doc.payments
            ]
        except Exception:
            payment_modes = []

    # Daily close pre-check for the selected POS Profile.
    daily_close_pending = False
    if pos_profile_name:
        try:
            daily_close_pending = validate_pos_close(pos_profile_name) == "Failed"
        except Exception:
            daily_close_pending = False

    # Multi-cashier status for the selected POS Profile.
    multi_cashier = _get_main_cashier_status(pos_profile_name)

    # DocType permission flags for POS Opening Entry.
    can_create = bool(frappe.has_permission("POS Opening Entry", "create"))
    can_submit = bool(frappe.has_permission("POS Opening Entry", "submit"))

    # Existing open entries for the current user (user-wide check).
    open_entries = frappe.get_all(
        "POS Opening Entry",
        filters={
            "user": user,
            "docstatus": 1,
            "status": "Open",
        },
        fields=[
            "name",
            "company",
            "pos_profile",
            "period_start_date",
            "branch",
            "status",
        ],
        order_by="period_start_date desc",
    )

    occupied_entry = _occupied_entry(pos_profile_name, allowed_profiles, multi_cashier, user)

    # Currency information for display: prefer the selected POS Profile's own
    # currency, falling back to the resolved Company's default currency.
    company_currency = None
    currency_symbol = None
    profile_currency = None
    if pos_profile_doc is not None:
        candidate = (
            pos_profile_doc.get("currency")
            if callable(getattr(pos_profile_doc, "get", None))
            else getattr(pos_profile_doc, "currency", None)
        )
        if isinstance(candidate, str) and candidate:
            profile_currency = candidate
    if not profile_currency and isinstance(pos_profile_data, dict):
        candidate = pos_profile_data.get("currency")
        if isinstance(candidate, str) and candidate:
            profile_currency = candidate

    if profile_currency:
        company_currency = profile_currency
        try:
            currency_symbol = frappe.db.get_value(
                "Currency", company_currency, "symbol"
            )
        except Exception:
            currency_symbol = None
    elif company:
        try:
            company_doc = frappe.get_doc("Company", company)
            company_currency = company_doc.default_currency
            currency_symbol = frappe.db.get_value(
                "Currency", company_currency, "symbol"
            )
        except Exception:
            company_currency = None
            currency_symbol = None

    user_full_name = None
    try:
        user_full_name = frappe.db.get_value("User", user, "full_name")
    except Exception:
        user_full_name = None

    return {
        "user": user,
        "user_full_name": user_full_name,
        "company": company,
        "company_currency": company_currency,
        "currency_symbol": currency_symbol,
        "allowed_profiles": allowed_profiles,
        "selected_profile": pos_profile_name,
        "branch": pos_profile_data.get("branch") if pos_profile_data else None,
        "restaurant": pos_profile_data.get("restaurant") if pos_profile_data else None,
        "payment_modes": payment_modes,
        "daily_close_pending": daily_close_pending,
        "multi_cashier": multi_cashier,
        "permissions": {"create": can_create, "submit": can_submit},
        "open_entries": open_entries,
        "occupied_entry": occupied_entry,
    }


def _occupied_entry(pos_profile_name, allowed_profiles, multi_cashier, user):
    """Another cashier's open entry on a single-cashier profile, or None."""
    if multi_cashier["enabled"] or pos_profile_name not in {profile["name"] for profile in allowed_profiles}:
        return None
    other_entries = frappe.get_all(
        "POS Opening Entry",
        filters={
            "pos_profile": pos_profile_name,
            "user": ["!=", user],
            "docstatus": 1,
            "status": "Open",
        },
        fields=["name", "user", "period_start_date"],
        order_by="period_start_date desc",
        limit=1,
    )
    if not other_entries:
        return None
    entry = other_entries[0]
    occupied_entry = {
        "user_full_name": frappe.db.get_value("User", entry["user"], "full_name") or None,
        "period_start_date": entry["period_start_date"],
    }
    entry_doc = frappe.get_doc("POS Opening Entry", entry["name"])
    if not frappe.has_permission("POS Opening Entry", "read", doc=entry_doc):
        return occupied_entry
    occupied_entry["name"] = entry["name"]
    # An operator assigned to the profile, with a billing role when the
    # profile restricts billing, may continue on today's open till.
    profile_doc = frappe.get_doc("POS Profile", pos_profile_name)
    assigned = any(row.user == user for row in profile_doc.applicable_for_users or [])
    billing_roles = {row.role for row in profile_doc.role_allowed_for_billing or []}
    if assigned and (not billing_roles or billing_roles.intersection(frappe.get_roles(user))):
        from ury.ury.api.ury_dashboard import _business_day_bounds

        start, end = _business_day_bounds(
            profile_doc.branch, default_hours=5, now=frappe.utils.now_datetime()
        )
        opened_at = frappe.utils.get_datetime(entry["period_start_date"])
        if start <= opened_at < end:
            occupied_entry["joinable"] = True
    return occupied_entry


def _validate_checklist_branch(pos_profile):
    """Ensure the session user's branch matches the given POS Profile's branch."""
    session_branch = getBranch()
    profile_branch = frappe.db.get_value("POS Profile", pos_profile, "branch")

    if not profile_branch:
        frappe.throw(
            _("POS Profile {0} not found.").format(pos_profile),
            frappe.DoesNotExistError,
        )

    if profile_branch != session_branch:
        frappe.throw(
            _("You do not have permission to access the checklist for POS Profile {0}.").format(
                pos_profile
            ),
            frappe.PermissionError,
        )


# Dependent Checklist Option -> the checklist-type gates that collect it.
# Opening runs RM -> Cashier -> Order Taker; Closing runs the reverse. The
# phase-explicit options let each role have distinct opening and closing
# goals; the legacy names keep their original phase binding so existing
# configurations do not break.
OPTIONS_BY_CHECKLIST_TYPE = {
    "Opening": [
        "POS Opening Entry",
        "Order Taking",  # legacy: binds order-taker goals to the opening gate
        "RM Opening Checklist",
        "Order Taker Opening Checklist",
    ],
    "Closing": [
        "POS Closing Entry",
        "RM Checklist",  # legacy: binds RM goals to the closing gate
        "RM Closing Checklist",
        "Order Taker Closing Checklist",
    ],
}

# Canonical role order for the checklist sequence. Names are matched by
# substring (case-insensitive) so custom role names ("Restaurant Manager",
# "URY Manager", ...) classify without hardcoding exact names.
_ROLE_RANK_TOKENS = (
    ("manager", 0),
    ("cashier", 1),
    ("captain", 2),
    ("order", 2),
    ("waiter", 2),
)

ROLE_RANK_LABELS = {0: "Restaurant Manager", 1: "Cashier", 2: "Order Taker"}


def _role_rank(role):
    name = (role or "").lower()
    for token, rank in _ROLE_RANK_TOKENS:
        if token in name:
            return rank
    return None


def _role_label(role):
    rank = _role_rank(role)
    return ROLE_RANK_LABELS.get(rank, role)


def _phase_dependent_rows(pos_profile, checklist_type):
    """Dependent Checklist rows of this POS Profile that belong to the given
    checklist phase (opening or closing)."""
    mapped_options = OPTIONS_BY_CHECKLIST_TYPE.get(checklist_type) or []
    if not mapped_options:
        return []
    rows = frappe.get_all(
        "Dependent Checklist",
        fields=["quality_checklist", "select_2", "role"],
        filters={"parent": pos_profile, "parenttype": "POS Profile"},
    )
    return [row for row in rows if row.select_2 in mapped_options]


def _goal_submitted(goal, branch, period_date, owner=None):
    """True when the goal's checklist has been SUBMITTED for this
    branch/business day: a review exists whose objectives all carry an
    explicit result. PASS and FAIL are both valid responses -- a failed
    objective records the failure but never blocks submission or the role
    hierarchy."""
    filters = {"goal": goal, "branch": branch, "date": period_date}
    if owner:
        filters["owner"] = owner
    return bool(
        frappe.db.exists(
            "Quality Review",
            {**filters, "status": ["in", ["Passed", "Failed"]]},
        )
    )


def _goal_submitted_since(goal, branch, since):
    """True when a submitted checklist (any PASS/FAIL mix) exists created at
    or after `since` -- the closing flow anchors on the shift period start."""
    return bool(
        frappe.get_all(
            "Quality Review",
            filters={
                "goal": goal,
                "branch": branch,
                "creation": [">=", since],
                "status": ["in", ["Passed", "Failed"]],
            },
            pluck="name",
            limit=1,
        )
    )


def _checklist_blocker(rows, checklist_type, branch, period_date, user_roles):
    """Earliest role in the phase sequence whose checklists are not all
    SUBMITTED (a submitted checklist may contain FAIL objectives -- that is
    a valid response, not a blocker).

    Opening runs ascending rank (RM -> Cashier -> Order Taker); Closing runs
    descending (Order Taker -> Cashier -> RM). A user acts at their lowest
    configured rank and is blocked by the roles that must finish before that
    step. Returns {"role", "role_label", "goals"} or None.
    """
    if not rows:
        return None
    ranked = {}
    for row in rows:
        rank = _role_rank(row.role)
        if rank is None:
            continue
        ranked.setdefault(rank, {}).setdefault(row.role, [])
        if row.quality_checklist not in ranked[rank][row.role]:
            ranked[rank][row.role].append(row.quality_checklist)

    position = _user_position_rank(rows, user_roles)
    if position is None:
        return None
    if checklist_type == "Opening":
        # RM first: lower ranks must finish before the user's step.
        predecessors = sorted(rank for rank in ranked if rank < position)
    else:
        # Closing runs in reverse (Order Taker first): higher ranks must
        # finish before the user's step.
        predecessors = sorted(
            (rank for rank in ranked if rank > position), reverse=True
        )

    for rank in predecessors:
        for role, goals in ranked[rank].items():
            unsubmitted = [
                goal for goal in goals if not _goal_submitted(goal, branch, period_date)
            ]
            if unsubmitted:
                return {
                    "role": role,
                    "role_label": _role_label(role),
                    "rank": rank,
                    "goals": unsubmitted,
                }
    return None


def _checklist_period_date(branch):
    """Date the grillax validations compare Quality Reviews against: the open
    shift's posting date when one exists, else today."""
    open_shift = frappe.get_all(
        "POS Opening Entry",
        fields=["posting_date"],
        filters={"branch": branch, "docstatus": 1, "status": "Open"},
        limit=1,
    )
    if open_shift:
        return open_shift[0].posting_date
    return frappe.utils.getdate()


def _existing_goal_review(goal, branch, period_date):
    """The session user's live Quality Review for this goal/period, if any.

    No document-status filter: ERPNext derives the review's doc-level status
    from its rows, so a review recording a failure has status "Failed" -- it
    is still the user's live review and must be found for updates/resume."""
    name = frappe.db.exists(
        "Quality Review",
        {
            "goal": goal,
            "branch": branch,
            "date": period_date,
            "owner": frappe.session.user,
        },
    )
    return frappe.get_doc("Quality Review", name) if name else None


def _goal_objectives(goal):
    return frappe.get_doc("Quality Goal", goal).objectives or []


def _user_position_rank(rows, user_roles):
    """The user's step in the phase sequence: their lowest rank among the
    roles actually configured on this POS Profile. None when the user has no
    ranked role here."""
    configured_ranks = {
        _role_rank(row.role) for row in rows if row.role in set(user_roles)
    }
    configured_ranks.discard(None)
    return min(configured_ranks) if configured_ranks else None


def _own_position_goals(rows, user_roles):
    """Goals the user must complete themselves: rows whose role is one of
    theirs at their sequence position."""
    if not rows:
        return []
    position = _user_position_rank(rows, user_roles)
    if position is None:
        return []
    return [
        row.quality_checklist
        for row in rows
        if row.role in set(user_roles) and _role_rank(row.role) == position
    ]


def _throw_blocked(blocked_by, checklist_type):
    phase = _("Opening") if checklist_type == "Opening" else _("Closing")
    frappe.throw(
        _(
            "{0} {1} Checklist is not completed yet. Please ask the {2} to complete it before continuing."
        ).format(blocked_by["role_label"], phase, blocked_by["role_label"]),
        frappe.PermissionError,
    )


def _goal_review_pending(goal, branch, period_date):
    """A goal is pending for THIS user until they have SUBMITTED its
    checklist. PASS and FAIL both count as submitted; only objectives with
    no explicit result keep the goal pending."""
    return not _goal_submitted(goal, branch, period_date, owner=frappe.session.user)


def _item_status(state):
    """Resolve a submitted item's explicit status. New frontends send
    status="Passed"/"Failed"; the legacy is_checked contract maps
    True -> Passed and False/omitted -> Open."""
    status = state.get("status")
    if status in ("Passed", "Failed"):
        return status
    return "Passed" if state.get("is_checked") else "Open"


def _require_remarks_for_failures(item_states):
    for label, state in item_states.items():
        if _item_status(state) == "Failed" and not (state.get("remarks") or "").strip():
            frappe.throw(
                _("Remarks are required for failed checklist items: {0}").format(label),
                frappe.ValidationError,
            )


def _review_row(objective, state):
    return {
        "objective": objective.objective,
        "target": objective.target,
        "uom": objective.uom,
        "status": _item_status(state),
        "review": state.get("remarks") or "",
    }


def _create_goal_review(goal, branch, period_date, item_states):
    """Create the Quality Review that satisfies the grillax checklist
    validations (POS Opening/Closing hooks) for one Dependent Checklist goal.
    Each objective's status mirrors the checkbox the user actually ticked in
    the POS checklist UI."""
    review = frappe.new_doc("Quality Review")
    review.goal = goal
    review.date = period_date
    review.branch = branch
    review.employee = frappe.session.user
    remarks = next(
        (state.get("remarks") for state in item_states.values() if state.get("remarks")),
        None,
    )
    if remarks:
        review.additional_information = remarks

    for objective in _goal_objectives(goal):
        review.append("reviews", _review_row(objective, item_states.get(objective.objective, {})))

    review.insert()
    return review.name


def _update_goal_review(review, item_states):
    """Fold a resubmitted checklist into the existing review: update statuses
    for objectives already on it, append rows for objectives it doesn't have."""
    seen = {row.objective for row in review.reviews or []}
    for row in review.reviews or []:
        state = item_states.get(row.objective)
        if state is None:
            continue
        row.status = _item_status(state)
        if state.get("remarks"):
            row.review = state["remarks"]

    for objective in _goal_objectives(review.goal):
        if objective.objective in seen:
            continue
        review.append(
            "reviews",
            _review_row(objective, item_states.get(objective.objective, {})),
        )

    review.save()
    return review.name


@frappe.whitelist()
def get_checklist(pos_profile, checklist_type):
    _validate_checklist_branch(pos_profile)

    configured_items = frappe.get_all(
        "URY Checklist Item",
        fields=["item_label", "applies_to", "is_mandatory"],
        filters={"parent": pos_profile, "applies_to": ["in", [checklist_type, "Both"]]},
        parent_doctype="POS Profile",
    )

    # Role-based sequence (grillax port): Opening runs RM -> Cashier ->
    # Order Taker, Closing the reverse. The gate stays closed -- with a
    # specific blocker message -- until every role before the user's step has
    # SUBMITTED its checklist for this branch/business day (any PASS/FAIL
    # mix; submission, not passing, is what unlocks the next role).
    branch = getBranch()
    rows = _phase_dependent_rows(pos_profile, checklist_type)
    user_roles = frappe.get_roles()
    period_date = _checklist_period_date(branch) if rows else None
    blocked_by = (
        _checklist_blocker(rows, checklist_type, branch, period_date, user_roles)
        if rows
        else None
    )
    if blocked_by:
        return {
            "items": [],
            "log_name": None,
            "log_status": None,
            "blocked_by": blocked_by,
        }
    goals = _own_position_goals(rows, user_roles)
    goal_items = []
    pending_goals = []
    for goal in goals:
        if _goal_review_pending(goal, branch, period_date):
            pending_goals.append(goal)
        review = _existing_goal_review(goal, branch, period_date)
        states = (
            {row.objective: row for row in review.reviews or []} if review else {}
        )
        objectives = [o.objective for o in _goal_objectives(goal)]
        if not objectives:
            # Goal without configured objectives: the goal name is the item.
            if not review:
                goal_items.append(
                    {
                        "item_label": goal,
                        "is_mandatory": 1,
                        "applies_to": checklist_type,
                        "goal": goal,
                        "status": None,
                        "remarks": "",
                    }
                )
            continue
        # Only objectives with NO explicit result resurface. FAIL is a valid
        # final response: it is recorded on the review and must not keep the
        # item (or the user) in the gate.
        remaining = [
            o for o in objectives if states.get(o) is None or states[o].status == "Open"
        ]
        if remaining:
            goal_items += [
                {
                    "item_label": o,
                    "is_mandatory": 1,
                    "applies_to": checklist_type,
                    "goal": goal,
                    "status": (
                        states[o].status
                        if states.get(o) and states[o].status in ("Passed", "Failed")
                        else None
                    ),
                    "remarks": (states[o].review or "") if states.get(o) else "",
                }
                for o in remaining
            ]

    # Nothing to collect (no legacy items and no pending goals for this
    # user's role) -> the gate is complete for them.
    if not configured_items and not goal_items:
        return {
            "items": [],
            "log_name": None,
            "log_status": "Complete",
            "blocked_by": None,
        }

    existing_log = frappe.get_all(
        "URY POS Checklist Log",
        fields=["name", "status"],
        filters={
            "pos_profile": pos_profile,
            "checklist_type": checklist_type,
            "shift_date": frappe.utils.getdate(),
        },
        limit=1,
    )

    # Prefill legacy items with the user's previously saved result so
    # reopening an in-progress log restores the last PASS/FAIL selection.
    if existing_log:
        log_rows = frappe.get_all(
            "URY Checklist Log Item",
            fields=["item_label", "is_checked", "status", "remarks"],
            filters={"parent": existing_log[0].name},
        )
        log_states = {row.item_label: row for row in log_rows}
        for item in configured_items:
            saved = log_states.get(item.item_label)
            if not saved:
                continue
            saved_status = saved.status or ("Passed" if saved.is_checked else None)
            item["status"] = (
                saved_status if saved_status in ("Passed", "Failed") else None
            )
            item["remarks"] = saved.remarks or ""

    items = list(configured_items) + goal_items

    log_name = None
    log_status = None
    if existing_log:
        log_name = existing_log[0].name
        log_status = existing_log[0].status
    if not items:
        log_status = "Complete"
    elif pending_goals and log_status == "Complete":
        # Legacy log is complete but role-based goals are still pending --
        # keep the gate open, otherwise the grillax validations block
        # opening/closing server-side with no way to clear them from the UI.
        log_status = None

    return {
        "items": items,
        "log_name": log_name,
        "log_status": log_status,
        "blocked_by": None,
    }


@frappe.whitelist()
def submit_checklist(pos_profile, checklist_type, items, pos_opening_entry=None):
    _validate_checklist_branch(pos_profile)

    configured_items = frappe.get_all(
        "URY Checklist Item",
        fields=["item_label", "is_mandatory"],
        filters={"parent": pos_profile, "applies_to": ["in", [checklist_type, "Both"]]},
        parent_doctype="POS Profile",
    )

    # Same role-sequence gate as get_checklist, enforced server-side so the
    # order cannot be bypassed by calling the API directly.
    branch = getBranch()
    rows = _phase_dependent_rows(pos_profile, checklist_type)
    user_roles = frappe.get_roles()
    period_date = _checklist_period_date(branch) if rows else None
    blocked_by = (
        _checklist_blocker(rows, checklist_type, branch, period_date, user_roles)
        if rows
        else None
    )
    if blocked_by:
        _throw_blocked(blocked_by, checklist_type)

    goals = _own_position_goals(rows, user_roles)

    if not configured_items and not goals:
        return {
            "status": "Complete",
            "name": None,
        }

    items = json.loads(items)

    # A FAIL without a reason is not submittable -- enforced server-side in
    # addition to the frontend gate.
    _require_remarks_for_failures(
        {item.get("item_label"): item for item in items if item.get("item_label")}
    )

    # Persist role-based checklist completions as Quality Reviews (grillax
    # model) so the POS Opening/Closing validations and the desk flows all
    # see the same completion state. Items carry the goal they belong to
    # (falling back to matching the objective text against the role-matched
    # goals); each objective's review row mirrors the user's result, and a
    # resubmission updates the existing review instead of duplicating it.
    objective_goal = {}
    for goal in goals:
        for objective in _goal_objectives(goal):
            objective_goal[objective.objective] = goal

    items_by_goal = {}
    for item in items:
        label = item.get("item_label")
        if not label:
            continue
        goal = (
            item.get("goal")
            if item.get("goal") in goals
            else objective_goal.get(label)
        )
        if goal:
            items_by_goal.setdefault(goal, {})[label] = item

    for goal, item_states in items_by_goal.items():
        review = _existing_goal_review(goal, branch, period_date)
        if review:
            _update_goal_review(review, item_states)
        else:
            _create_goal_review(goal, branch, period_date, item_states)

    existing_log = frappe.get_all(
        "URY POS Checklist Log",
        fields=["name", "status"],
        filters={
            "pos_profile": pos_profile,
            "checklist_type": checklist_type,
            "shift_date": frappe.utils.getdate(),
        },
        limit=1,
    )

    def _goal_submission_status():
        """Incomplete only while some objective still has no explicit result.
        A submitted checklist -- any PASS/FAIL mix -- is Complete and closes
        the gate; the role hierarchy likewise only requires submission."""
        if any(
            not _goal_submitted(goal, branch, period_date, owner=frappe.session.user)
            for goal in goals
        ):
            return "In Progress"
        return None

    if not configured_items:
        # No legacy items: completion is driven purely by Quality Reviews.
        if existing_log:
            return {
                "status": existing_log[0].status,
                "name": existing_log[0].name,
            }
        return {
            "status": _goal_submission_status() or "Complete",
            "name": None,
        }

    mandatory_by_label = {row.item_label: row.is_mandatory for row in configured_items}

    if existing_log:
        log_doc = frappe.get_doc("URY POS Checklist Log", existing_log[0].name)
    else:
        log_doc = frappe.new_doc("URY POS Checklist Log")
        log_doc.pos_profile = pos_profile
        log_doc.branch = branch
        log_doc.checklist_type = checklist_type
        log_doc.shift_date = frappe.utils.getdate()

    if pos_opening_entry:
        log_doc.pos_opening_entry = pos_opening_entry

    log_doc.set("items", [])
    for item in items:
        status = _item_status(item)
        log_doc.append(
            "items",
            {
                "item_label": item.get("item_label"),
                # Dependent Checklist goals are always mandatory; legacy
                # items keep their own configured flag.
                "is_mandatory": 1
                if item.get("item_label") in goals
                else mandatory_by_label.get(item.get("item_label"), 0),
                # is_checked is kept in sync with the explicit status so
                # pre-change consumers (and pre-change rows, read back via
                # the is_checked fallback below) keep working.
                "is_checked": 1 if status == "Passed" else 0,
                "status": status,
                "remarks": item.get("remarks"),
            },
        )

    def _row_answered(row):
        # Rows written before the status field existed have status=None and
        # fall back to is_checked.
        saved = row.status or ("Passed" if row.is_checked else None)
        return saved in ("Passed", "Failed")

    all_mandatory_answered = all(
        _row_answered(row) for row in log_doc.items if row.is_mandatory
    )

    if all_mandatory_answered:
        # PASS and FAIL are both valid responses: once every mandatory item
        # has a result the checklist is Complete (submitted). Individual
        # failures stay recorded on the rows for reporting.
        log_doc.status = "Complete"
        log_doc.completed_by = frappe.session.user
        log_doc.completed_at = frappe.utils.now()
    else:
        log_doc.status = "In Progress"

    log_doc.save()

    # Role-based goal states take precedence: pending/failed goals keep the
    # submission below Complete regardless of the legacy log status.
    goal_status = _goal_submission_status()
    if goal_status:
        return {
            "status": goal_status,
            "name": log_doc.name,
        }

    return {
        "status": log_doc.status,
        "name": log_doc.name,
    }


def _merge_sources(secondary_invoice):
    """One check name, or a JSON list of check names, in the order given."""
    if isinstance(secondary_invoice, str) and secondary_invoice.strip().startswith("["):
        secondary_invoice = json.loads(secondary_invoice)
    names = [secondary_invoice] if isinstance(secondary_invoice, str) else list(secondary_invoice or [])
    return list(dict.fromkeys(name for name in names if name))


@frappe.whitelist()

def merge_bills(primary_invoice, secondary_invoice, approval_token=None, reason=None, reason_note=None):
    """Add Check (Oracle Simphony Add/Transfer Check): move every line of the selected open
    check(s) onto the primary check, which stays the guest's one POS Invoice.

    `secondary_invoice` is one check name or a JSON list of names. The checks must be drafts of
    one branch and one order type that the caller may write. Governed by `merge_checks` privilege
    / Manager PIN in place with deterministic sorted `FOR UPDATE` locking.
    """
    from ury.ury_pos.privileged_auth import (
        DEFAULT_MANAGER_ROLES,
        _is_valid_str,
        _table_exists,
        assert_not_efris_fiscalised,
        assert_not_offline_replay,
        authorize_or_perform,
        is_company_policy_active,
    )

    assert_not_offline_replay()

    try:

        secondaries = _merge_sources(secondary_invoice)

        if not secondaries or primary_invoice in secondaries:
            frappe.throw("Cannot merge an invoice with itself.")

        # Lock all participating checks in deterministic sorted order to prevent deadlocks / races.
        locked_docs = {}
        for name in sorted([primary_invoice, *secondaries]):
            locked_docs[name] = frappe.get_doc("POS Invoice", name, for_update=True)
        primary_doc = locked_docs[primary_invoice]
        secondary_docs = [locked_docs[name] for name in secondaries]

        # Authorization: caller must have write permission on every invoice
        if not frappe.has_permission("POS Invoice", "write", doc=primary_doc):
            frappe.throw(
                "You do not have permission to merge this bill.",
                frappe.PermissionError,
            )

        assert_not_efris_fiscalised(primary_doc)

        for secondary_doc in secondary_docs:

            if not frappe.has_permission("POS Invoice", "write", doc=secondary_doc):
                frappe.throw(
                    "You do not have permission to merge the selected bill.",
                    frappe.PermissionError,
                )

            assert_not_efris_fiscalised(secondary_doc)

            # Prevent cross-branch merging
            if primary_doc.branch != secondary_doc.branch:
                frappe.throw(
                    "Cannot merge bills from different branches.",
                    frappe.PermissionError,
                )

            # Validation
            if (primary_doc.docstatus != 0 or secondary_doc.docstatus != 0):
                frappe.throw("Both invoices must be in Draft state to merge.")

            # Bills linked before Add Check consolidated lines settle as their pair.
            if primary_doc.custom_merged_pos_invoice:
                frappe.throw("This bill already includes a merged bill.")

            if secondary_doc.custom_merged_pos_invoice:
                frappe.throw("The selected bill already includes another bill.")

            if not secondary_doc.items:
                frappe.throw("The selected bill has no items to merge.")

            if primary_doc.order_type != secondary_doc.order_type:
                frappe.throw(_("Only bills of the same order type can be merged."))

        pos_profile_name = getattr(primary_doc, "pos_profile", None)
        if _is_valid_str(pos_profile_name):
            raw_clock_in_req = frappe.db.get_value(
                "POS Profile", pos_profile_name, "custom_require_clock_in_to_order"
            )
            if isinstance(raw_clock_in_req, (int, float, str)) and cint(raw_clock_in_req) == 1:
                from ury.ury_pos import timeclock

                timeclock.enforce_clock_in_for_profile_name(pos_profile_name, user=frappe.session.user)
        raw_company = getattr(primary_doc, "company", None)
        company = raw_company if _is_valid_str(raw_company) else None
        policy_active = is_company_policy_active(company)

        # Determine legacy fallback when no POS Privilege Rule rows exist yet:
        # keep same-operator / elevated / billing merges usable, while cross-waiter
        # merges by an ordinary floor captain require a manager PIN.
        user = getattr(getattr(frappe, "session", None), "user", None)
        other_waiters = [
            getattr(d, "waiter", None)
            for d in [primary_doc, *secondary_docs]
            if getattr(d, "waiter", None) and getattr(d, "waiter", None) != user
        ]
        user_roles = set(frappe.get_roles() or [])
        elevated_or_billing = bool(user_roles & (DEFAULT_MANAGER_ROLES | {"System Manager", "URY Cashier"}))
        if _is_valid_str(pos_profile_name) and _table_exists("POS Profile"):
            try:
                profile_doc = frappe.get_doc("POS Profile", pos_profile_name)
                transfer_roles = {r.role for r in (getattr(profile_doc, "transfer_role_permissions", None) or [])}
                billing_roles = {r.role for r in (getattr(profile_doc, "role_allowed_for_billing", None) or [])}
                if user_roles & (transfer_roles | billing_roles):
                    elevated_or_billing = True
            except Exception:
                pass
        legacy_allowed = (not other_waiters) or elevated_or_billing or user == "Administrator"

        if policy_active or approval_token or not legacy_allowed or _table_exists("POS Authorisation Log"):
            authorize_or_perform(
                privilege="merge_checks",
                pos_profile=pos_profile_name if _is_valid_str(pos_profile_name) else None,
                company=company,
                skip_profile_company_lookup=True,
                reference_doctype="POS Invoice",
                reference_name=primary_doc.name,
                claim={
                    "action": "merge_bills",
                    "primary_invoice": primary_doc.name,
                    "secondary_invoices": sorted(secondaries),
                },
                token=approval_token,
                reason=reason,
                reason_note=reason_note,
                require_reason=None if policy_active else False,
                allow_fallback_predicate=legacy_allowed,
            )

        from ury.ury.doctype.ury_order.ury_order import consolidate_checks

        consolidate_checks(primary_doc, secondary_docs)

        frappe.db.commit()


        return {
            "status": "success",
            "message": "Bills merged successfully",
            "name": primary_doc.name,
        }


    except _permission_error_types():

        frappe.db.rollback()

        raise


    except frappe.ValidationError as e:

        frappe.db.rollback()

        msg = str(e)
        if any(
            code in msg
            for code in (
                "POS-AUTH-",
                "POS-REASON-",
                "POS-OFFLINE-",
                "POS-TIMECARD-",
                "POS-OUTLET-",
                "EFRIS_FISCALISED",
            )
        ):
            raise

        frappe.log_error(
            title="Bill Merge Error",
            message=frappe.get_traceback(),
        )

        return {
            "status": "error",
            "message": msg,
        }


    except Exception as e:

        frappe.db.rollback()

        frappe.log_error(
            title="Bill Merge Error",
            message=frappe.get_traceback(),
        )

        return {
            "status": "error",
            "message": str(e),
        }


@frappe.whitelist()
def ensure_payment_mode_accounts(modes, company):
    """Ensure every Mode of Payment in `modes` has a default account for `company`.

    Called from the frontend POS Profile form right before save, so a payment
    mode with no company-scoped default account (e.g. Cheque, Credit Card,
    Zomato, Swiggy, Direct -- anything the dev-seed's Cash/Card/UPI wiring
    never covers) doesn't crash ERPNext's standard POS Profile validation
    ("Please set default Cash or Bank account in Mode of Payments ...").
    """
    from ury.ury.dev_seed.profiles import _ensure_mode_of_payment

    # Mode of Payment / Account records are accounts-configuration data, so
    # require the same permission ERPNext's own Mode of Payment desk form
    # requires (Accounts Manager / URY Manager per this app's DocPerm
    # fixtures -- System Manager is NOT granted create on either doctype
    # here, live-verified) -- not just any authenticated session.
    if not frappe.has_permission("Mode of Payment", "create") or not frappe.has_permission("Account", "create"):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    if isinstance(modes, str):
        modes = frappe.parse_json(modes)
    if not modes or not company:
        return []

    ensured = []
    for mode in modes:
        if not mode:
            continue
        _ensure_mode_of_payment(mode, company)
        ensured.append(mode)
    return ensured


@frappe.whitelist(allow_guest=False)
def is_printer_watch_installed() -> dict:
    try:
        installed = "ury_printer_watch" in frappe.get_installed_apps()
    except Exception:
        installed = False
    return {"installed": bool(installed)}


_CLOSED_STATUSES = ('Paid', 'Partly Paid', 'Unpaid', 'Overdue', 'Consolidated', 'Return', 'Credit Note Issued')
_COPY_LOG = 'URY Bill Reprint Log'


def _closed_branch(requested=None):
    if not {'URY Cashier', 'URY Manager'}.intersection(frappe.get_roles()) or not frappe.has_permission('POS Invoice', 'read'):
        frappe.throw('Not permitted to access closed checks.', frappe.PermissionError)
    branch = getBranch()
    if not branch or (requested and requested != branch):
        frappe.throw('Not permitted outside your assigned branch.', frappe.PermissionError)
    return branch


def _user_assigned_to_till(user: str | None, pos_profile: str | None) -> bool:
    if not user or not pos_profile:
        return False
    return bool(
        frappe.db.exists(
            'POS Profile User',
            {'parent': pos_profile, 'parenttype': 'POS Profile', 'user': user},
        )
        or frappe.db.exists('POS Profile User', {'parent': pos_profile, 'user': user})
    )


def _closed_invoice(name, branch, *, for_update=False):
    from ury.ury.doctype.ury_order.ury_order import _enforce_order_access

    doc = frappe.get_doc('POS Invoice', name, for_update=for_update)
    if doc.branch != branch or not frappe.has_permission('POS Invoice', 'read', doc=doc):
        frappe.throw('Not permitted to read this check.', frappe.PermissionError)
    try:
        _enforce_order_access(doc, require_modify=False)
    except frappe.PermissionError:
        user = getattr(getattr(frappe, 'session', None), 'user', None) or ''
        roles = set(frappe.get_roles() or [])
        same_till_cashier = bool(
            roles.intersection({'URY Cashier', 'URY Manager'})
            and user
            and (
                doc.get('cashier') == user
                or doc.get('owner') == user
                or _user_assigned_to_till(user, doc.get('pos_profile'))
            )
        )
        if not same_till_cashier:
            raise
    if doc.docstatus != 1 or doc.status not in _CLOSED_STATUSES:
        frappe.throw('Only submitted closed checks can be viewed here.')
    return doc


@frappe.whitelist()
def search_settled_bills(branch: str | None = None, pos_profile: str | None = None,
                         date_from: str | None = None, date_to: str | None = None,
                         bill_no: str | None = None, table: str | None = None,
                         start: int = 0, page_length: int = 20):
    branch = _closed_branch(branch)
    filters = {'branch': branch, 'docstatus': 1, 'status': ['in', _CLOSED_STATUSES]}
    if date_from and date_to:
        if date.fromisoformat(date_from) > date.fromisoformat(date_to):
            frappe.throw('Start date must precede end date.')
        filters['posting_date'] = ['between', [date_from, date_to]]
    elif date_from or date_to:
        filters['posting_date'] = ['>=' if date_from else '<=', date.fromisoformat(date_from or date_to)]
    if bill_no:
        filters['name'] = ['like', f'%{bill_no}%']
    if table:
        filters['restaurant_table'] = table
    if pos_profile:
        filters['pos_profile'] = pos_profile
    names = frappe.get_list('POS Invoice', filters=filters, fields=['name'], start=max(0, cint(start)),
                            page_length=max(1, min(50, cint(page_length))), order_by='posting_date desc, posting_time desc, name desc')
    rows = []
    for row in names:
        try:
            doc = _closed_invoice(row.name, branch)
        except frappe.PermissionError:
            continue
        data = {key: doc.get(key) for key in ('name', 'posting_date', 'posting_time', 'branch', 'restaurant_table',
                'pos_profile', 'customer', 'cashier', 'waiter', 'currency', 'grand_total', 'net_total', 'rounded_total',
                'discount_amount', 'additional_discount_percentage', 'paid_amount', 'change_amount', 'room_number',
                'is_return', 'return_against', 'custom_reopened_from', 'efris_irn', 'efris_status',
                'total_taxes_and_charges', 'outstanding_amount', 'status', 'consolidated_invoice')}
        for child, fields in [('items', ('name', 'item_code', 'item_name', 'qty', 'rate', 'amount')), ('taxes', ('description', 'tax_amount')),
                              ('payments', ('mode_of_payment', 'amount', 'reference_no', 'reference_date',
                                            'custom_tender_currency', 'custom_tender_amount',
                                            'custom_exchange_rate', 'custom_exchange_rate_date'))]:
            data[child] = [{key: entry.get(key) for key in fields} for entry in doc.get(child) or []]
        rows.append(data)
    return rows


def _resolve_invoice_letter_head(doc) -> str | None:
    if doc is None:
        return None
    lh = doc.get('letter_head') if hasattr(doc, 'get') else getattr(doc, 'letter_head', None)
    if isinstance(lh, str) and lh.strip():
        return lh.strip()
    pos_profile = doc.get('pos_profile') if hasattr(doc, 'get') else getattr(doc, 'pos_profile', None)
    if isinstance(pos_profile, str) and pos_profile.strip():
        prof_lh = frappe.db.get_value('POS Profile', pos_profile.strip(), 'letter_head')
        if isinstance(prof_lh, str) and prof_lh.strip():
            return prof_lh.strip()
    return None


def _copy_print_url(invoice: str, copy_number: int, letter_head: str | None = None) -> str:
    clean_lh = letter_head.strip() if isinstance(letter_head, str) and letter_head.strip() else None
    lh_params = (
        f'&no_letterhead=0&letterhead={quote(clean_lh, safe="")}'
        if clean_lh
        else '&no_letterhead=1'
    )
    return (
        f'/printview?doctype=POS%20Invoice&name={quote(invoice, safe="")}'
        f'&format=URY%20Bill%20Copy{lh_params}&trigger_print=1&copy_number={copy_number}'
    )


def _render_copy_html(doc, invoice: str, copy_number: int) -> tuple[str, str]:
    from ury.ury.class_overrides.pos_invoice import _COPY_RENDER_MARKER

    doc.copy_timestamp = frappe.utils.now()
    doc.copy_number = copy_number
    doc._ury_copy_render = _COPY_RENDER_MARKER
    letter_head = _resolve_invoice_letter_head(doc)
    kwargs = {'print_format': 'URY Bill Copy', 'doc': doc, 'no_letterhead': 0 if letter_head else 1}
    if letter_head:
        kwargs['letterhead'] = letter_head
    rendered = frappe.get_print('POS Invoice', invoice, **kwargs)
    html = rendered.decode('utf-8') if isinstance(rendered, bytes) else (rendered or '')
    return html, doc.copy_timestamp


def _copy_result(job, *, doc=None, delivery=None):
    result = {key: job.get(key) for key in ('status', 'failure_reason', 'print_job_id')}
    if job.get('status') in ('QZ_DISPATCHING', 'BROWSER_DISPATCHING', 'DISPATCHING'):
        result['failure_reason'] = 'Copy dispatch has started. Check the printer and original till before printing again.'
    copy_number = cint(job.get('copy_number'))
    if copy_number:
        result['copy_number'] = copy_number
    if job.get('status') == 'RESERVED' and doc is not None and copy_number:
        result['delivery'] = delivery or (
            'qz' if cint(frappe.db.get_value('POS Profile', doc.pos_profile, 'qz_print')) else 'browser'
        )
        invoice = job.get('invoice') or doc.name
        if result['delivery'] == 'qz':
            result['html'], result['copy_timestamp'] = _render_copy_html(doc, invoice, copy_number)
        result['print_url'] = _copy_print_url(invoice, copy_number, letter_head=_resolve_invoice_letter_head(doc))
    return result


def _copy_race_errno(exc: BaseException) -> int | None:
    """Extract MariaDB/MySQL numeric errno across Frappe wrappers, nested args, and __cause__/__context__."""
    queue = [exc]
    seen: set[int] = set()
    while queue:
        cur = queue.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        errno = getattr(cur, 'errno', None)
        if isinstance(errno, int) and not isinstance(errno, bool):
            return errno
        if isinstance(cur, BaseException):
            args = getattr(cur, 'args', None) or ()
            if args and isinstance(args[0], int) and not isinstance(args[0], bool):
                return args[0]
            for arg in args:
                if isinstance(arg, (BaseException, tuple, list)):
                    queue.append(arg)
            if cur.__cause__ is not None:
                queue.append(cur.__cause__)
            if cur.__context__ is not None:
                queue.append(cur.__context__)
        elif isinstance(cur, (tuple, list)) and cur:
            if isinstance(cur[0], int) and not isinstance(cur[0], bool):
                return cur[0]
    return None


def _open_isolated_copy_db():
    """Open a short-lived independent DB connection for URY Bill Reprint Log isolation."""
    try:
        from frappe.database import get_db
        from frappe.database.database import Database
    except Exception:
        return None
    current_db = getattr(frappe, 'db', None)
    if not isinstance(current_db, Database):
        return None
    if getattr(current_db, '_conn', None) is None:
        return None
    conf = getattr(getattr(frappe, 'local', None), 'conf', None)
    db_name = getattr(conf, 'db_name', None) if conf else None
    if not db_name:
        return None
    isolated = get_db(
        socket=getattr(conf, 'db_socket', None),
        host=getattr(conf, 'db_host', None),
        port=getattr(conf, 'db_port', None),
        user=getattr(conf, 'db_user', None) or db_name,
        password=getattr(conf, 'db_password', None),
        cur_db_name=db_name,
    )
    isolated.connect()
    return isolated


class _IsolatedCopyLogDB:
    """Run URY Bill Reprint Log claiming/updates in an isolated short DB transaction.

    Prevents MariaDB 11.8 innodb_snapshot_isolation (errno 1020) from leaking or
    invalidating a caller's uncommitted transaction when concurrent bill copies race.
    """

    def __init__(self, invoice: str | None = None):
        self.invoice = invoice
        self.isolated = None
        self._prev_db = None
        self._swapped_local = False
        self._lock_name = None

    def __enter__(self):
        from hashlib import sha256

        isolated = _open_isolated_copy_db()
        if isolated is None:
            return None
        self.isolated = isolated
        try:
            current_db = getattr(frappe, 'db', None)
            if type(current_db).__name__ == 'LocalProxy':
                self._prev_db = frappe.local.db
                frappe.local.db = isolated
                self._swapped_local = True
            else:
                self._prev_db = current_db
                frappe.db = isolated
                self._swapped_local = False

            if self.invoice:
                db_name = getattr(isolated, 'cur_db_name', None) or ''
                digest = sha256(f'{db_name}:{self.invoice}'.encode()).hexdigest()[:48]
                lock_name = f'ury_copy_{digest}'
                lock_rows = isolated.sql('SELECT GET_LOCK(%s, 10)', (lock_name,))
                acquired = bool(lock_rows and lock_rows[0] and cint(lock_rows[0][0]) == 1)
                if not acquired:
                    timeout_exc = getattr(frappe, 'QueryTimeoutError', TimeoutError)
                    raise timeout_exc('Timed out waiting for bill copy numbering lock.')
                self._lock_name = lock_name
                # Start the InnoDB ReadView strictly after acquiring the advisory lock.
                isolated.rollback()
            return isolated
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.isolated is None:
            return False
        try:
            if exc_type is not None:
                try:
                    self.isolated.rollback()
                except Exception:
                    pass
            if self._lock_name:
                try:
                    self.isolated.sql('SELECT RELEASE_LOCK(%s)', (self._lock_name,))
                except Exception:
                    pass
        finally:
            if self._swapped_local:
                frappe.local.db = self._prev_db
            else:
                frappe.db = self._prev_db
            try:
                self.isolated.close()
            except Exception:
                pass
            self.isolated = None
            self._lock_name = None
        return False


def _is_retryable_copy_race_error(exc: Exception) -> bool:
    retryable = tuple(
        cls for cls in (
            getattr(frappe, 'DuplicateEntryError', None),
            getattr(frappe, 'QueryDeadlockError', None),
            getattr(frappe, 'QueryTimeoutError', None),
        )
        if isinstance(cls, type) and issubclass(cls, BaseException)
    )
    if retryable and isinstance(exc, retryable):
        return True
    errno = _copy_race_errno(exc)
    return errno in (1020, 1062, 1205, 1213)


_COPY_LOG_LOOKUP_FIELDS = [
    'name', 'print_job_id', 'invoice', 'status', 'failure_reason', 'attempt', 'copy_number', 'printer', 'job_owner',
    'dispatch_token_hash', 'dispatch_until', 'fiscal_event',
]
_DISABLED_REPRINT_OWNER_REASON = 'Requester account is disabled; reprint from Settled Bills.'


def _find_previous_copy_log(invoice: str, key: str, owner: str, idempotency_key: str, is_auto_fiscal: bool):
    from hashlib import sha256

    previous = frappe.db.get_value(
        _COPY_LOG,
        {'request_key': key},
        _COPY_LOG_LOOKUP_FIELDS,
        as_dict=True,
        order_by='attempt desc',
        for_update=True,
    )
    if previous:
        return previous

    # Accept pre-upgrade [owner, invoice, idempotency_key] hashes so reprints queued
    # before the user-independent key format never print a duplicate COPY #2.
    legacy_owners = []
    if owner:
        legacy_owners.append(owner)
    if is_auto_fiscal:
        pending_owner = frappe.db.get_value(_COPY_LOG, f'PENDING-EFRIS-{invoice}', 'job_owner')
        if isinstance(pending_owner, str) and pending_owner and pending_owner not in legacy_owners:
            legacy_owners.append(pending_owner)
        existing_rows = frappe.get_all(
            _COPY_LOG,
            filters={'invoice': invoice, 'attempt': ['>', 0]},
            fields=['job_owner'],
        ) or []
        for existing in existing_rows:
            existing_owner = (
                existing.get('job_owner') if isinstance(existing, dict) else getattr(existing, 'job_owner', None)
            )
            if isinstance(existing_owner, str) and existing_owner and existing_owner not in legacy_owners:
                legacy_owners.append(existing_owner)

    for legacy_owner in legacy_owners:
        legacy_key = sha256(json.dumps([legacy_owner, invoice, idempotency_key]).encode()).hexdigest()
        if legacy_key == key:
            continue
        previous = frappe.db.get_value(
            _COPY_LOG,
            {'request_key': legacy_key},
            _COPY_LOG_LOOKUP_FIELDS,
            as_dict=True,
            order_by='attempt desc',
            for_update=True,
        )
        if previous:
            return previous
    return None


def _mark_pending_fiscal_reprint_dispatched(invoice: str):
    pending_id = f'PENDING-EFRIS-{invoice}'
    if frappe.db.exists(_COPY_LOG, pending_id):
        frappe.db.set_value(_COPY_LOG, pending_id, {'status': 'DISPATCHED'})


def _copy_request_key(invoice, idempotency_key):
    from hashlib import sha256

    if not idempotency_key or len(idempotency_key) > 128:
        frappe.throw('A copy request key is required (maximum 128 characters).')
    return sha256(json.dumps([invoice, idempotency_key]).encode()).hexdigest()


def _fiscal_statuses(doc):
    """Read the authoritative fiscal provider for the invoice and its linked bill."""
    try:
        from uganda_compliance.efris.efris_queue import get_efris_status
    except ImportError:
        return []
    primary = get_efris_status('POS Invoice', doc.name) or {}
    events = [primary]
    partner = getattr(doc, 'custom_merged_pos_invoice', None)
    if partner:
        linked = get_efris_status('POS Invoice', partner) or {}
        if linked.get('efris_status') or linked.get('fdn'):
            events.append(linked)
    return events


def _issued_fiscal_event(events):
    from hashlib import sha256

    fdns = []
    for event in events:
        fdn = str(event.get('fdn') or '').strip()
        status = str(event.get('efris_status') or '').strip().lower()
        if not fdn or status in ('pending', 'failed', 'cancelled', 'rejected'):
            return None
        fdns.append(fdn)
    return sha256(json.dumps(fdns).encode()).hexdigest() if fdns else None


def _require_fiscal_event(doc):
    pending = frappe.db.get_value(
        _COPY_LOG, {'name': f'PENDING-EFRIS-{doc.name}', 'invoice': doc.name,
                    'job_owner': frappe.session.user, 'status': 'AWAITING_FDN'},
        'name', for_update=True,
    )
    event = _issued_fiscal_event(_fiscal_statuses(doc)) if pending else None
    if not event:
        frappe.throw('Automatic fiscal copy is unavailable. Request a manual copy from Settled Bills.', frappe.PermissionError)
    return event


def _lease_hash(token):
    from hashlib import sha256
    return sha256(token.encode()).hexdigest()


def _new_copy_lease():
    from secrets import token_urlsafe
    from frappe.utils import now_datetime

    token = token_urlsafe(32)
    return token, {'dispatch_token_hash': _lease_hash(token), 'dispatch_until': now_datetime() + timedelta(minutes=5)}


def _consume_browser_render_grant(invoice, copy_number, grant):
    from frappe.utils import get_datetime, now_datetime

    if not isinstance(grant, str) or len(grant) != 43 or not copy_number:
        frappe.throw('Open the numbered copy from its original till.', frappe.PermissionError)
    branch = _closed_branch()
    # GET printview requests normally roll back. Consume the one-use grant in a
    # short transaction using the same parent/attempt lock order as confirmation.
    with _IsolatedCopyLogDB(invoice) as claim_db:
        if claim_db is None:
            frappe.throw("Browser copy authorization is temporarily unavailable.")
        doc = _closed_invoice(invoice, branch, for_update=True)
        record = frappe.db.get_value(
            _COPY_LOG, {'invoice': invoice, 'copy_number': copy_number, 'job_owner': frappe.session.user,
                        'status': 'BROWSER_DISPATCHING', 'render_grant_hash': _lease_hash(grant)},
            ['name', 'dispatch_until', 'fiscal_event'], as_dict=True, for_update=True,
        )
        if not record or not record.dispatch_until or get_datetime(record.dispatch_until) <= now_datetime():
            frappe.throw('This browser copy has expired or was already used.', frappe.PermissionError)
        if record.fiscal_event and record.fiscal_event != _require_fiscal_event(doc):
            frappe.throw('The reserved fiscal receipt has changed.', frappe.PermissionError)
        frappe.db.set_value(_COPY_LOG, record.name, {'render_grant_hash': None, 'dispatch_until': None})
        claim_db.commit()
    return copy_number


def _finish_copy_attempt(invoice, idempotency_key, dispatch_token, *, action):
    from secrets import compare_digest, token_urlsafe
    from frappe.utils import get_datetime, now_datetime
    from ury.ury_pos.privileged_auth import assert_not_offline_replay

    assert_not_offline_replay()
    key = _copy_request_key(invoice, idempotency_key)
    branch = _closed_branch()
    # The request may already have a MariaDB snapshot. Start a short serialized
    # transaction, then lock the parent before the attempt and fiscal queue rows.
    with _IsolatedCopyLogDB(invoice) as claim_db:
        doc = _closed_invoice(invoice, branch, for_update=True)
        record = frappe.db.get_value(
            _COPY_LOG, {'invoice': invoice, 'request_key': key, 'job_owner': frappe.session.user},
            _COPY_LOG_LOOKUP_FIELDS, as_dict=True, order_by='attempt desc', for_update=True,
        )
        if not record or record.printer or record.status not in ('RESERVED', 'QZ_DISPATCHING', 'BROWSER_DISPATCHING', 'DISPATCHING', 'COMPLETED'):
            frappe.throw('No client copy reservation for this request.', frappe.PermissionError)
        if not dispatch_token or not compare_digest(record.dispatch_token_hash or '', _lease_hash(dispatch_token)):
            frappe.throw('This print attempt no longer owns the copy.', frappe.PermissionError)
        result = {'copy_number': record.copy_number, 'print_job_id': record.name}
        if action in ('qz', 'browser'):
            # A cashier may explicitly switch a possibly spooled QZ copy to browser
            # once. Each transport claims the physical dispatch before it starts.
            fallback = record.status == 'QZ_DISPATCHING' and action == 'browser'
            valid_lease = (record.status == 'RESERVED' and record.dispatch_until
                           and get_datetime(record.dispatch_until) > now_datetime())
            if not (fallback or valid_lease):
                frappe.throw('Copy dispatch expired or already started. Check the original till.', frappe.PermissionError)
            if record.fiscal_event and _require_fiscal_event(doc) != record.fiscal_event:
                frappe.throw('The reserved fiscal receipt has changed.', frappe.PermissionError)
            update = {'status': 'QZ_DISPATCHING', 'dispatch_until': None}
            if action == 'browser':
                grant = token_urlsafe(32)
                update.update(status='BROWSER_DISPATCHING', render_grant_hash=_lease_hash(grant),
                              dispatch_until=now_datetime() + timedelta(minutes=5))
                result['render_grant'] = grant
            frappe.db.set_value(_COPY_LOG, record.name, update)
            record.status = 'DISPATCHING'
        elif action == 'release':
            if record.status != 'RESERVED':
                frappe.throw('Dispatch already started. Check the original till.', frappe.PermissionError)
            frappe.db.set_value(_COPY_LOG, record.name, {'dispatch_token_hash': None, 'dispatch_until': None})
        elif record.status != 'COMPLETED':
            if idempotency_key == f'efris-auto-{invoice}':
                if _require_fiscal_event(doc) != record.fiscal_event:
                    frappe.throw('Fiscal receipt changed before delivery confirmation.', frappe.PermissionError)
                _mark_pending_fiscal_reprint_dispatched(invoice)
            record.status = 'COMPLETED'
            frappe.db.set_value(_COPY_LOG, record.name, {
                'status': record.status, 'dispatch_until': None, 'render_grant_hash': None,
            })
        if claim_db is not None:
            claim_db.commit()
        return dict(result, status=record.status)


@frappe.whitelist(methods=['POST'])
def begin_bill_copy_dispatch(invoice: str, idempotency_key: str, dispatch_token: str, transport: str = 'browser'):
    if transport not in ('qz', 'browser'):
        frappe.throw('Select a client print transport.')
    return _finish_copy_attempt(invoice, idempotency_key, dispatch_token, action=transport)


@frappe.whitelist(methods=['POST'])
def confirm_bill_copy(invoice: str, idempotency_key: str, dispatch_token: str):
    return _finish_copy_attempt(invoice, idempotency_key, dispatch_token, action='confirm')


@frappe.whitelist(methods=['POST'])
def release_bill_copy(invoice: str, idempotency_key: str, dispatch_token: str):
    return _finish_copy_attempt(invoice, idempotency_key, dispatch_token, action='release')


@frappe.whitelist(methods=['POST'])
def print_bill_copy(
    invoice: str,
    idempotency_key: str,
    printer: str | None = None,
    delivery: str | None = None,
    approval_token: str | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
):
    from ury.ury.api.ury_print import _bill_printers
    from ury.ury.class_overrides.pos_invoice import _COPY_RENDER_MARKER
    from ury.ury.printing.file_store import save_job
    from ury.ury.printing.service import submit_and_monitor_print_job
    from ury.ury_pos.privileged_auth import (
        DEFAULT_MANAGER_ROLES,
        _doc_get,
        _is_valid_str,
        assert_not_offline_replay,
        authorize_or_perform,
        is_company_policy_active,
    )

    assert_not_offline_replay()

    # Idempotent per invoice and copy key, whoever dispatches (cashier till or scheduler).
    key = _copy_request_key(invoice, idempotency_key)
    branch = _closed_branch()
    owner = frappe.session.user
    requested_delivery = (delivery or '').strip().lower() or None
    is_auto_fiscal = idempotency_key == f'efris-auto-{invoice}'

    doc = _closed_invoice(invoice, branch)
    transport = None
    record = None
    replayed = None
    dispatch_token = None
    number = 0
    job_id = ''
    chosen_printer = printer
    caller_has_writes = getattr(frappe.db, 'transaction_writes', None) != 0
    with _IsolatedCopyLogDB(invoice) as claim_db:
        for race_attempt in range(5):
            savepoint = None
            if claim_db is None:
                savepoint = f'ury_print_bill_copy_{race_attempt}'
                frappe.db.savepoint(savepoint)
            try:
                previous = _find_previous_copy_log(invoice, key, owner, idempotency_key, is_auto_fiscal)
                if previous and previous.status not in ('RESERVED', 'FAILED', 'CANCELED'):
                    # A replay reports the original outcome without granting another dispatch.
                    return _copy_result(previous)

                fiscal_event = _require_fiscal_event(doc) if is_auto_fiscal else None
                if previous and previous.status == 'RESERVED':
                    from frappe.utils import get_datetime, now_datetime

                    if previous.job_owner != owner:
                        frappe.throw('The client copy is reserved by another cashier.', frappe.PermissionError)
                    active = previous.dispatch_until and get_datetime(previous.dispatch_until) > now_datetime()
                    if requested_delivery == 'network' or active:
                        return {'status': 'IN_FLIGHT', 'copy_number': previous.copy_number,
                                'failure_reason': 'Copy already reserved for client printing. Check the original till.'}
                    if previous.fiscal_event and fiscal_event != previous.fiscal_event:
                        frappe.throw('The reserved fiscal receipt has changed.', frappe.PermissionError)
                    dispatch_token, lease = _new_copy_lease()
                    frappe.db.set_value(_COPY_LOG, previous.name, dict(lease, fiscal_event=fiscal_event))
                    if claim_db is not None:
                        claim_db.commit()
                    replayed = previous
                    break

                printers = _bill_printers(doc.pos_profile, doc.restaurant_table)
                if requested_delivery in ('browser', 'qz'):
                    transport = requested_delivery
                    chosen_printer = None
                elif requested_delivery is None and not printer and not printers:
                    transport = 'qz' if cint(frappe.db.get_value('POS Profile', doc.pos_profile, 'qz_print')) else 'browser'
                    chosen_printer = None
                else:
                    transport = 'network'
                    if not printers or (printer and printer not in printers):
                        frappe.throw('Select a configured bill printer for this outlet.')
                    chosen_printer = printer or printers[0]

                attempt = previous.attempt + 1 if previous else 1
                if previous:
                    number = previous.copy_number
                    prior_copies = max(0, cint(number) - 1)
                else:
                    # Locking read prevents MariaDB 11.8 innodb_snapshot_isolation stale-count races.
                    latest_copy = frappe.db.get_value(
                        _COPY_LOG,
                        {'invoice': invoice, 'attempt': 1},
                        'copy_number',
                        order_by='copy_number desc',
                        for_update=True,
                    )
                    prior_copies = cint(latest_copy)
                    number = prior_copies + 1

                # Enforce free closed-copy cap (`custom_max_free_closed_copies`, default 0 on configured profiles)
                # and `reprint_bill` privilege / Manager PIN + reason on every manual copy beyond the free cap.
                # The verified pending fiscal event authorizes one automatic copy.
                if not previous and not fiscal_event:
                    raw_profile = _doc_get(doc, 'pos_profile')
                    raw_max_free = (
                        frappe.db.get_value('POS Profile', raw_profile, 'custom_max_free_closed_copies')
                        if _is_valid_str(raw_profile)
                        else None
                    )
                    raw_company = _doc_get(doc, 'company')
                    company = raw_company if _is_valid_str(raw_company) else None
                    policy_active = is_company_policy_active(company)

                    if raw_max_free is not None or policy_active or approval_token:
                        max_free = max(0, cint(raw_max_free)) if raw_max_free is not None else 0
                        if prior_copies >= max_free:
                            authorize_or_perform(
                                privilege='reprint_bill',
                                pos_profile=doc.pos_profile,
                                company=company,
                                skip_profile_company_lookup=True,
                                reference_doctype='POS Invoice',
                                reference_name=invoice,
                                claim={
                                    'action': 'print_bill_copy',
                                    'invoice': invoice,
                                    'copy_number': number,
                                },
                                token=approval_token,
                                reason=reason,
                                reason_note=reason_note,
                                require_reason=True,
                                fallback_roles=DEFAULT_MANAGER_ROLES,
                            )

                job_id = f'COPY-{key}-{attempt}'
                copy_slot = f'{invoice}:{number}:{attempt}'

                record = {
                    'doctype': _COPY_LOG,
                    'name': job_id,
                    'print_job_id': job_id,
                    'request_key': key,
                    'copy_slot': copy_slot,
                    'invoice': invoice,
                    'printer': None if transport in ('browser', 'qz') else chosen_printer,
                    'job_owner': owner,
                    'attempt': attempt,
                    'copy_number': number,
                    'retry_of': previous.name if previous else None,
                    'status': 'RESERVED' if transport in ('browser', 'qz') else 'SUBMITTING',
                }
                record['fiscal_event'] = fiscal_event
                if transport in ('browser', 'qz'):
                    dispatch_token, lease = _new_copy_lease()
                    record.update(lease)
                frappe.get_doc(record).insert(ignore_permissions=True)
                if claim_db is not None:
                    claim_db.commit()
                break
            except Exception as exc:
                if race_attempt < 4 and _is_retryable_copy_race_error(exc):
                    if claim_db is not None:
                        claim_db.rollback()
                    elif not caller_has_writes and _copy_race_errno(exc) == 1020:
                        # MariaDB 11.8 innodb_snapshot_isolation pins ReadView across savepoints.
                        # Resetting the transaction is only safe when the caller had zero prior writes.
                        frappe.db.rollback()
                    else:
                        try:
                            frappe.db.rollback(save_point=savepoint)
                        except Exception as rb_exc:
                            if not caller_has_writes and _copy_race_errno(rb_exc) == 1305:
                                frappe.db.rollback()
                            else:
                                raise exc from rb_exc
                    continue
                raise

    if dispatch_token:
        try:
            result = _copy_result(replayed or record, doc=doc, delivery=requested_delivery or transport)
        except Exception:
            # No printable payload left the server. Return the attempt for immediate retry.
            _finish_copy_attempt(invoice, idempotency_key, dispatch_token, action='release')
            raise
        result['dispatch_token'] = dispatch_token
        return result
    doc.copy_timestamp = frappe.utils.now()
    doc.copy_number = number
    doc._ury_copy_render = _COPY_RENDER_MARKER
    meta = {'print_job_id': job_id, 'invoice': invoice, 'job_owner': owner, 'retry_of': record['retry_of'],
            'job_type': 'BILL_COPY', 'reference_doctype': 'POS Invoice', 'reference_name': invoice,
            'created_at': doc.copy_timestamp, 'printer': chosen_printer, 'status': 'SUBMITTING'}
    if not save_job(job_id, meta):
        result = {'status': 'Failure', 'message': 'Could not create the print job.'}
    else:
        try:
            copy_lh = _resolve_invoice_letter_head(doc)
            submit_kwargs = dict(
                doctype='POS Invoice',
                name=invoice,
                printer_setting=chosen_printer,
                print_format='URY Bill Copy',
                doc=doc,
                job_type='BILL_COPY',
                extra_metadata={k: v for k, v in meta.items() if k != 'status'},
            )
            if copy_lh:
                submit_kwargs['no_letterhead'] = 0
                submit_kwargs['letterhead'] = copy_lh
            result = submit_and_monitor_print_job(**submit_kwargs)
        except Exception:
            frappe.log_error(title='Bill copy dispatch failed')
            result = {'status': 'Unknown', 'message': 'Copy outcome unknown. Check the printer.'}
    status = {'Success': 'SUBMITTED', 'Failure': 'FAILED'}.get(result['status'], 'UNKNOWN')
    update = {'status': status, 'failure_reason': result.get('message')}
    if status != 'SUBMITTED':
        save_job(job_id, {**meta, **update})
    # A fast polling worker can already have recorded the terminal outcome.
    final_record = {**record, **update}
    with _IsolatedCopyLogDB() as update_db:
        current = frappe.db.get_value(
            _COPY_LOG,
            job_id,
            ['status', 'print_job_id', 'failure_reason', 'copy_number', 'invoice', 'printer'],
            as_dict=True,
            for_update=True,
        )
        if current and current.status != 'SUBMITTING':
            if is_auto_fiscal and current.status in ('SUBMITTED', 'COMPLETED'):
                _mark_pending_fiscal_reprint_dispatched(invoice)
            if update_db is not None:
                update_db.commit()
            final_record = current
        else:
            frappe.db.set_value(_COPY_LOG, job_id, update)
            if is_auto_fiscal and status in ('SUBMITTED', 'COMPLETED'):
                _mark_pending_fiscal_reprint_dispatched(invoice)
            if update_db is not None:
                update_db.commit()
    return _copy_result(final_record, doc=doc, delivery='network')


def _record_settlement_fiscal_intent(doc):
    """Called in the submission transaction, once for the primary receipt."""
    from ury.ury_pos.tax_determination import _is_company_efris_enabled

    events = _fiscal_statuses(doc)
    if _issued_fiscal_event(events):
        # Offline replay still owes the first fiscal receipt at this transition.
        if not getattr(doc, 'ury_offline_captured_at', None):
            return
    elif not any(event.get('efris_status') or event.get('fdn') for event in events):
        if not _is_company_efris_enabled(getattr(doc, 'company', None)):
            return

    job_id = f'PENDING-EFRIS-{doc.name}'
    frappe.get_doc({
        'doctype': _COPY_LOG, 'print_job_id': job_id, 'name': job_id,
        'invoice': doc.name, 'job_owner': frappe.session.user,
        'request_key': f'efris-auto:{doc.name}', 'copy_slot': f'{doc.name}:0:0',
        'attempt': 0, 'copy_number': 0, 'status': 'PENDING_RECEIPT',
    }).insert(ignore_permissions=True)


def _settlement_fiscal_job(invoice):
    _closed_invoice(invoice, _closed_branch(), for_update=True)
    return frappe.db.get_value(
        _COPY_LOG, {'name': f'PENDING-EFRIS-{invoice}', 'invoice': invoice, 'job_owner': frappe.session.user},
        ['name', 'status'], as_dict=True, for_update=True,
    )


@frappe.whitelist(methods=['POST'])
def close_unused_fiscal_copy(invoice: str):
    """The primary receipt includes the FDN. Retire its unused copy authority."""
    job = _settlement_fiscal_job(invoice)
    if job and job.status == 'PENDING_RECEIPT':
        frappe.db.set_value(_COPY_LOG, job.name, 'status', 'NOT_REQUIRED')


@frappe.whitelist(methods=['POST'])
def queue_fiscal_reprint(invoice: str, delivery: str | None = None):
    job = _settlement_fiscal_job(invoice)
    if not job:
        frappe.throw('No pending fiscal receipt. Request a manual copy from Settled Bills.', frappe.PermissionError)
    if job.status == 'PENDING_RECEIPT':
        frappe.db.set_value(_COPY_LOG, job.name, {
            'status': 'AWAITING_FDN', 'failure_reason': (delivery or '').strip().lower() or None,
        })
        job.status = 'AWAITING_FDN'
    return {'status': job.status, 'print_job_id': job.name, 'invoice': invoice}


def process_pending_fiscal_reprints():
    """Scheduled worker: dispatch queued COPY #n reprints on network printers once FDN arrives."""
    try:
        from uganda_compliance.efris.efris_queue import get_efris_status
    except ImportError:
        return
    from ury.ury.api.ury_print import _bill_printers

    pending = frappe.get_all(
        _COPY_LOG,
        filters={'status': 'AWAITING_FDN'},
        fields=['name', 'invoice', 'job_owner'],
    )
    for row in pending:
        try:
            efris = get_efris_status('POS Invoice', row.invoice) or {}
            if not efris.get('fdn'):
                continue
            inv = frappe.get_doc('POS Invoice', row.invoice)
            if inv.custom_merged_pos_invoice:
                partner_efris = get_efris_status('POS Invoice', inv.custom_merged_pos_invoice) or {}
                if partner_efris.get('efris_status') and not partner_efris.get('fdn'):
                    continue
            if cint(frappe.db.get_value('POS Profile', inv.pos_profile, 'qz_print')):
                continue
            if not _bill_printers(inv.pos_profile, inv.restaurant_table):
                continue
            if not row.job_owner or not cint(frappe.db.get_value('User', row.job_owner, 'enabled')):
                frappe.db.set_value(
                    _COPY_LOG,
                    row.name,
                    {'status': 'FAILED', 'failure_reason': _DISABLED_REPRINT_OWNER_REASON},
                )
                frappe.log_error(
                    title='Pending Fiscal Reprint Skipped',
                    message=f'{row.name} ({row.invoice}): {_DISABLED_REPRINT_OWNER_REASON} (job_owner={row.job_owner!r})',
                )
                frappe.db.commit()
                continue
            prev_user = frappe.session.user
            try:
                frappe.set_user(row.job_owner)
                outcome = print_bill_copy(
                    invoice=row.invoice,
                    idempotency_key=f'efris-auto-{row.invoice}',
                    delivery='network',
                )
            finally:
                frappe.set_user(prev_user)
            if outcome.get('status') in ('SUBMITTED', 'COMPLETED'):
                frappe.db.set_value(_COPY_LOG, row.name, {'status': 'DISPATCHED'})
            # Persist each dispatched queue row in its own transaction before the next row runs.
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title='Pending Fiscal Reprint Dispatch Failed')
            frappe.db.commit()


@frappe.whitelist(methods=['GET'])
def credit_accounts(pos_profile: str, query: str):
    from ury.ury_pos.credit import balance, default_accounts, require_credit_access

    profile = frappe.get_doc('POS Profile', pos_profile)
    require_credit_access(profile)
    text = query.strip()[:100]
    if not text or not profile.customer:
        return []
    pattern = '%' + text + '%'
    accounts = frappe.get_list('Customer',
        filters={'disabled': 0, 'name': ['not in', list(filter(None, default_accounts(profile)))],
                 'credit_limits.company': profile.company, 'credit_limits.credit_limit': ['>', 0]},
        or_filters={'name': ['like', pattern], 'customer_name': ['like', pattern]},
        fields=['name','customer_name','credit_limits.credit_limit as limit_amount'],
        order_by='customer_name asc, name asc', page_length=20)
    currency = frappe.get_cached_value('Company', profile.company, 'default_currency')
    profile_currency = (profile.get('currency') or currency or '').strip()
    conversion_rate = 1.0
    if profile_currency and currency and profile_currency.upper() != str(currency).strip().upper():
        try:
            from erpnext.setup.utils import get_exchange_rate

            conversion_rate = frappe.utils.flt(get_exchange_rate(profile_currency, currency)) or 0.0
        except Exception:
            conversion_rate = 0.0
        if conversion_rate <= 0:
            conversion_rate = frappe.utils.flt(
                frappe.db.get_value(
                    'Currency Exchange',
                    {'from_currency': profile_currency, 'to_currency': currency},
                    'exchange_rate',
                )
            ) or 0.0
    result = []
    for row in accounts:
        outstanding = balance(row.name, profile.company)
        result.append(dict(name=row.name, customer_name=row.customer_name, balance=outstanding,
                           limit=row.limit_amount, available=frappe.utils.flt(row.limit_amount)-outstanding,
                           currency=currency, conversion_rate=conversion_rate))
    return result


def _safe_order_type_tax_config(profile):
    try:
        from ury.ury_pos.tax_determination import get_order_type_tax_config

        return get_order_type_tax_config(profile)
    except Exception:
        return {"tax_category": None, "order_type_tax_categories": {}}


def _apply_preview_selling_pricing_rules(doc):
    """Evaluate native ERPNext selling Pricing Rules when previewing a cart without a customer.

    ERPNext v16's ``set_transaction_type()`` omits ``POS Invoice`` from its selling doctype
    tuple, falling back to ``transaction_type='buying'`` whenever ``doc.customer`` is blank.
    Invoking ``get_pricing_rule_for_item`` with explicit ``transaction_type='selling'`` ensures
    cart preview matches ``MenuCard`` and ``sync_order`` identically before a customer is chosen.
    """
    if doc.get("customer") or cint(doc.get("ignore_pricing_rule")) == 1:
        return
    try:
        from erpnext.accounts.doctype.pricing_rule.pricing_rule import get_pricing_rule_for_item
    except (ImportError, ModuleNotFoundError):
        return

    apply_fn = getattr(doc, "apply_pricing_rule_on_items", None)
    if not callable(apply_fn):
        return

    dict_cls = getattr(frappe, "_dict", None)
    if not isinstance(dict_cls, type):
        dict_cls = dict

    for item in doc.get("items") or []:
        if (
            cint(item.get("is_disposable")) == 1
            or not item.get("item_code")
            or cint(item.get("ignore_pricing_rule")) == 1
            or cint(item.get("custom_price_overridden")) == 1
            or cint(item.get("custom_is_open_item")) == 1
            or cint(item.get("custom_is_complimentary")) == 1
            or (
                item.get("custom_line_role") == "Combo Upcharge"
                and (item.get("custom_parent_line_key") or "").strip()
            )
        ):
            continue
        ctx = dict_cls(
            {
                "doctype": "POS Invoice",
                "name": doc.get("name"),
                "child_doctype": "POS Invoice Item",
                "child_docname": item.get("name"),
                "transaction_type": "selling",
                "company": doc.get("company"),
                "currency": doc.get("currency"),
                "conversion_rate": doc.get("conversion_rate") or 1.0,
                "price_list": doc.get("selling_price_list"),
                "selling_price_list": doc.get("selling_price_list"),
                "price_list_currency": doc.get("price_list_currency") or doc.get("currency"),
                "plc_conversion_rate": doc.get("plc_conversion_rate") or 1.0,
                "posting_date": doc.get("posting_date"),
                "transaction_date": doc.get("posting_date"),
                "coupon_code": doc.get("coupon_code"),
                "ignore_pricing_rule": 0,
                "item_code": item.get("item_code"),
                "item_group": item.get("item_group"),
                "brand": item.get("brand"),
                "qty": item.get("qty"),
                "stock_qty": item.get("stock_qty") or item.get("qty"),
                "uom": item.get("uom") or item.get("stock_uom"),
                "price_list_rate": item.get("price_list_rate"),
                "rate": item.get("rate"),
                "warehouse": item.get("warehouse"),
            }
        )
        try:
            ret = get_pricing_rule_for_item(ctx, doc=doc, for_validate=False)
        except Exception:
            continue
        if ret and ret.get("pricing_rules"):
            apply_fn(item, ret)


def _format_rate_pct(rate: float) -> str:
    rounded = round(float(rate), 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"


def _clean_tax_account_label(account_head: str | None, fallback_desc: str | None = None) -> str:
    import re

    acct = (account_head or "").strip()
    if acct:
        stripped = re.sub(r"\s+-\s+[A-Za-z0-9]+$", "", acct).strip()
        if stripped:
            return stripped
    desc = (fallback_desc or "").strip()
    if desc:
        return re.sub(r"\s*\(\s*\d+(?:\.\d+)?%\s*\)\s*$", "", desc).strip() or desc
    return ""


def _build_preview_tax_rows(doc, prec: int) -> list[dict]:
    """Build truthful preview tax rows, surfacing actual item-level rates when Item Tax Templates
    override the header Sales Taxes and Charges template rate."""
    from frappe.utils import flt

    item_wise_rows = doc.get("item_wise_tax_details") or []
    items = [r for r in (doc.get("items") or []) if cint(r.get("is_disposable")) != 1]

    result = []
    for row in doc.get("taxes") or []:
        account_head = row.get("account_head") or ""
        header_rate = flt(row.get("rate"))
        raw_desc = row.get("description") or account_head or ""
        tax_row_name = row.get("name")

        charge_kind = (row.get("custom_pos_charge_kind") or "").strip()
        charge_type = (row.get("charge_type") or "").strip()
        is_special_charge_row = bool(charge_kind) or (
            bool(charge_type) and charge_type != "On Net Total"
        )

        applied_rates: list[float] = []
        if not is_special_charge_row:
            if tax_row_name and item_wise_rows:
                for iwd in item_wise_rows:
                    if iwd.get("tax_row") == tax_row_name and flt(iwd.get("taxable_amount")) > 0:
                        applied_rates.append(round(flt(iwd.get("rate")), 4))

            if not applied_rates and account_head and items:
                for item in items:
                    raw_map = item.get("item_tax_rate")
                    item_rate = header_rate
                    if raw_map:
                        try:
                            parsed = json.loads(raw_map) if isinstance(raw_map, str) else raw_map
                            if isinstance(parsed, dict) and account_head in parsed:
                                item_rate = flt(parsed[account_head])
                        except Exception:
                            pass
                    applied_rates.append(round(item_rate, 4))

        distinct_rates = sorted(set(applied_rates))
        non_zero_rates = sorted({r for r in distinct_rates if r > 0})
        overridden = any(abs(r - header_rate) > 1e-6 for r in distinct_rates)

        item_rate_override = False
        effective_rate = header_rate
        description = raw_desc

        if overridden:
            base_label = _clean_tax_account_label(account_head, raw_desc)
            if len(distinct_rates) == 1:
                item_rate_override = True
                effective_rate = distinct_rates[0]
                description = f"{base_label} ({_format_rate_pct(effective_rate)}%)" if base_label else f"{_format_rate_pct(effective_rate)}%"
            elif len(non_zero_rates) == 1 and abs(non_zero_rates[0] - header_rate) > 1e-6:
                item_rate_override = True
                effective_rate = non_zero_rates[0]
                description = f"{base_label} ({_format_rate_pct(effective_rate)}%)" if base_label else f"{_format_rate_pct(effective_rate)}%"
            elif len(non_zero_rates) > 1:
                item_rate_override = True
                effective_rate = None
                description = base_label or raw_desc

        result.append(
            {
                "description": description,
                "rate": effective_rate if effective_rate is not None else header_rate,
                "header_rate": header_rate,
                "effective_rate": effective_rate,
                "item_rate_override": item_rate_override,
                "tax_amount": flt(row.get("tax_amount"), prec),
                "account_head": account_head,
                "custom_pos_charge_kind": charge_kind,
            }
        )
    return result


_PREVIEW_MODIFIER_COMBO_FIELDS = (
    "reservation_line_key",
    "custom_parent_line_key",
    "custom_line_role",
    "custom_is_modifier",
    "custom_modifier_prefix",
    "custom_modifier_group",
    "custom_modifier_option",
    "custom_modifiers_json",
    "custom_bundle_overrides_json",
    "custom_inherit_parent_routing",
    "custom_hide_unpriced_on_receipt",
)


def _has_modifier_or_combo_metadata(rows) -> bool:
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if (
            cint(row.get("custom_is_modifier")) == 1
            or bool((row.get("custom_parent_line_key") or "").strip())
            or bool((row.get("custom_modifier_group") or "").strip())
            or bool((row.get("custom_modifier_option") or "").strip())
            or bool((row.get("custom_modifiers_json") or "").strip())
            or bool((row.get("custom_bundle_overrides_json") or "").strip())
            or (row.get("custom_line_role") or "").strip() in ("Modifier", "Combo Header", "Combo Upcharge")
        ):
            return True
    return False


def _is_privileged_preview_row(row, orig_invoice=None) -> bool:
    from frappe.utils import flt

    if (
        cint(row.get("custom_price_overridden")) == 1
        or cint(row.get("custom_is_open_item")) == 1
        or cint(row.get("custom_is_complimentary")) == 1
        or cint(row.get("ignore_pricing_rule")) == 1
    ):
        return True
    return bool(
        orig_invoice is not None
        and cint(orig_invoice.get("custom_is_complimentary")) == 1
        and flt(row.get("rate")) == 0
        and flt(row.get("discount_percentage")) >= 100
    )


def _append_orig_preview_row(
    doc,
    row,
    *,
    qty=None,
    cost_center=None,
    warehouse=None,
    privileged_snapshots=None,
    orig_invoice=None,
):
    from frappe.utils import flt

    is_privileged_row = _is_privileged_preview_row(row, orig_invoice=orig_invoice)
    raw_plr = row.get("price_list_rate")
    plr = flt(raw_plr) if raw_plr is not None else flt(row.get("rate"))
    raw_base_plr = row.get("base_price_list_rate")
    base_plr = flt(raw_base_plr) if raw_base_plr is not None else plr
    raw_base_rate = row.get("base_rate")
    base_rate = flt(raw_base_rate) if raw_base_rate is not None else flt(row.get("rate"))
    eff_qty = flt(qty) if qty is not None else flt(row.get("qty"))

    item_entry = {
        "item_code": row.get("item_code"),
        "item_name": row.get("item_name") or row.get("item_code"),
        "qty": eff_qty,
        "rate": flt(row.get("rate")),
        "base_rate": base_rate,
        "price_list_rate": plr,
        "base_price_list_rate": base_plr,
        "discount_percentage": flt(row.get("discount_percentage")),
        "discount_amount": flt(row.get("discount_amount")),
        "ignore_pricing_rule": 1 if is_privileged_row else cint(row.get("ignore_pricing_rule")),
        "custom_price_overridden": cint(row.get("custom_price_overridden")),
        "custom_is_open_item": cint(row.get("custom_is_open_item")),
        "custom_is_complimentary": cint(row.get("custom_is_complimentary")),
        "uom": row.get("uom"),
        "cost_center": row.get("cost_center") or cost_center,
        "warehouse": row.get("warehouse") or warehouse,
    }
    if row.get("pricing_rules") is not None:
        item_entry["pricing_rules"] = row.get("pricing_rules")
    for tax_field in ("item_tax_template", "item_tax_rate", "custom_tax_exempt"):
        if row.get(tax_field) is not None:
            item_entry[tax_field] = row.get(tax_field)
    for mod_field in _PREVIEW_MODIFIER_COMBO_FIELDS:
        if row.get(mod_field) is not None:
            item_entry[mod_field] = row.get(mod_field)

    cloned_row = doc.append("items", item_entry)
    is_combo_upcharge = (row.get("custom_line_role") or "") == "Combo Upcharge"
    if (is_privileged_row or is_combo_upcharge) and cloned_row is not None and privileged_snapshots is not None:
        privileged_snapshots[id(cloned_row)] = {
            "price_list_rate": plr,
            "base_price_list_rate": base_plr,
            "rate": flt(row.get("rate")),
            "base_rate": base_rate,
            "discount_percentage": flt(row.get("discount_percentage")),
            "discount_amount": flt(row.get("discount_amount")),
            "pricing_rules": row.get("pricing_rules"),
            "ignore_pricing_rule": 1 if is_privileged_row else cint(row.get("ignore_pricing_rule")),
            "custom_price_overridden": cint(row.get("custom_price_overridden")),
            "custom_is_open_item": cint(row.get("custom_is_open_item")),
            "custom_is_complimentary": cint(row.get("custom_is_complimentary")),
        }
    return cloned_row


def _match_preview_items_to_orig_rows(raw_items, orig_rows):
    """Match incoming cart preview items 1-to-1 to saved POS Invoice Item rows.

    Pass 1 binds explicit row identities (`name` / `invoice_item_name`, then
    `reservation_line_key` / `reservation_line_ref` / `line_ref`) with same
    `item_code` so edits, qty changes, and newly added duplicate items stay
    unambiguously separated. Pass 2 falls back to unclaimed rows of the same
    `item_code` only for callers that omitted explicit row identities.
    """
    from frappe.utils import flt

    candidates = [
        (o_idx, row)
        for o_idx, row in enumerate(orig_rows or [])
        if cint(row.get("is_disposable")) != 1 and row.get("item_code")
    ]
    claimed: set[int] = set()
    matched: dict[int, dict] = {}
    has_explicit_key: dict[int, bool] = {}

    for idx, raw in enumerate(raw_items or []):
        code = raw.get("item") or raw.get("item_code")
        if not code:
            continue
        raw_name = str(raw.get("name") or raw.get("invoice_item_name") or "").strip()
        raw_key = str(
            raw.get("reservation_line_key")
            or raw.get("reservation_line_ref")
            or raw.get("line_ref")
            or ""
        ).strip()
        has_explicit_key[idx] = bool(raw_name or raw_key)

        chosen = None
        if raw_name:
            for o_idx, row in candidates:
                if o_idx in claimed or row.get("item_code") != code:
                    continue
                if str(row.get("name") or "").strip() == raw_name:
                    chosen = (o_idx, row)
                    break
        if chosen is None and raw_key:
            for o_idx, row in candidates:
                if o_idx in claimed or row.get("item_code") != code:
                    continue
                row_key = str(row.get("reservation_line_key") or "").strip()
                row_name = str(row.get("name") or "").strip()
                if (row_key and row_key == raw_key) or (row_name and row_name == raw_key):
                    chosen = (o_idx, row)
                    break
        if chosen is not None:
            claimed.add(chosen[0])
            matched[idx] = chosen[1]

    for idx, raw in enumerate(raw_items or []):
        if idx in matched or has_explicit_key.get(idx):
            continue
        code = raw.get("item") or raw.get("item_code")
        if not code:
            continue
        pool = [(o_idx, row) for o_idx, row in candidates if o_idx not in claimed and row.get("item_code") == code]
        if not pool:
            continue
        raw_item_name = str(raw.get("item_name") or "").strip()
        raw_rate = raw.get("rate") if raw.get("rate") is not None else raw.get("price")
        raw_is_mod = cint(raw.get("custom_is_modifier"))
        raw_parent_key = str(raw.get("custom_parent_line_key") or "").strip()

        def _rank(entry):
            o_idx, row = entry
            mod_miss = 0 if cint(row.get("custom_is_modifier")) == raw_is_mod else 1
            parent_miss = (
                0
                if (not raw_parent_key or str(row.get("custom_parent_line_key") or "").strip() == raw_parent_key)
                else 1
            )
            name_miss = 0 if (raw_item_name and str(row.get("item_name") or "").strip() == raw_item_name) else 1
            rate_diff = abs(flt(row.get("rate")) - flt(raw_rate)) if raw_rate is not None else 0.0
            return (mod_miss, parent_miss, name_miss, rate_diff, o_idx)

        best_idx, best_row = min(pool, key=_rank)
        claimed.add(best_idx)
        matched[idx] = best_row

    return matched


@frappe.whitelist()
def preview_order_totals(
    pos_profile,
    order_type=None,
    customer=None,
    items=None,
    no_of_pax=0,
    invoice_name=None,
    additional_discount_percentage=0,
    coupon_code=None,
    room=None,
    table=None,
    captured_at=None,
):
    """Stateless, server-authoritative order totals & tax preview (ZERO DB writes).

    Constructs an in-memory POS Invoice, stamps outlet timezone, resolves menu/daypart
    price list + time-of-day Pricing Rules + SAP/Simphony order-type Tax Category &
    native ERPNext Tax Rule + per-item Item Tax Templates + calculate_taxes_and_totals(),
    plus optional country-gated EFRIS readiness warnings. Never inserts, saves, or commits.
    """
    from frappe.utils import flt, getdate

    from ury.ury_pos.dayparts import resolve_menu_and_price_list, stamp_invoice_outlet_time
    from ury.ury_pos.default_customer import resolve_default_customer
    from ury.ury_pos.tax_determination import apply_order_type_tax, validate_efris_readiness

    if isinstance(items, str):
        items = frappe.parse_json(items) or []
    items = list(items or [])

    profile = frappe.get_doc("POS Profile", pos_profile)
    _authorize_pos_profile_access(profile)
    user_roles = set(frappe.get_roles() or [])
    is_super = frappe.session.user == "Administrator" or "System Manager" in user_roles

    if captured_at is not None and cint(profile.get("ury_offline_capture")) != 1:
        frappe.throw(
            coded_message(
                "OFFLINE-CAPTURE-DISABLED",
                _("Offline capture is not enabled for this POS Profile."),
            ),
            frappe.ValidationError,
        )

    doc = frappe.new_doc("POS Invoice")
    doc.is_pos = 1
    doc.pos_profile = pos_profile
    doc.company = profile.get("company")
    doc.currency = profile.get("currency")
    doc.branch = profile.get("branch")
    doc.restaurant = (
        profile.get("restaurant")
        or (
            frappe.db.get_value("URY Restaurant", {"branch": doc.branch}, "name")
            if doc.branch
            else None
        )
    )

    orig_invoice = None
    if invoice_name:
        orig_invoice = frappe.get_doc("POS Invoice", invoice_name)
        if callable(getattr(orig_invoice, "check_permission", None)):
            orig_invoice.check_permission("read")
        if not is_super and orig_invoice.get("branch") and doc.branch and orig_invoice.branch != doc.branch:
            frappe.throw(_("Not permitted outside your active branch."), frappe.PermissionError)

    effective_order_type = (
        (order_type or "").strip()
        or (orig_invoice.get("order_type") if orig_invoice else None)
        or "Dine In"
    )
    doc.order_type = effective_order_type
    doc.no_of_pax = cint(no_of_pax) or (cint(orig_invoice.get("no_of_pax")) if orig_invoice else 0)
    doc.restaurant_table = table or (orig_invoice.get("restaurant_table") if orig_invoice else None)
    doc.custom_restaurant_room = room or (orig_invoice.get("custom_restaurant_room") if orig_invoice else None)

    effective_customer = (
        (customer or "").strip()
        or (orig_invoice.get("customer") if orig_invoice else None)
        or resolve_default_customer(profile, effective_order_type)
    )
    doc.customer = effective_customer
    customer_doc = None
    if effective_customer:
        customer_doc = _authorize_customer_for_pos_profile(effective_customer, profile)

    if orig_invoice and cint(orig_invoice.get("custom_tax_exempt")) == 1:
        doc.custom_tax_exempt = 1
        doc.tax_category = orig_invoice.get("tax_category") or ""
        doc.taxes_and_charges = orig_invoice.get("taxes_and_charges") or ""
    if orig_invoice:
        if cint(orig_invoice.get("ignore_pricing_rule")) == 1:
            doc.ignore_pricing_rule = 1
        for sc_field in (
            "custom_is_complimentary",
            "custom_service_charge_waived",
            "custom_service_charge_override_rate",
            "custom_service_charge_override_amount",
        ):
            val = orig_invoice.get(sc_field)
            if val is not None:
                setattr(doc, sc_field, val)

    if orig_invoice and orig_invoice.get("custom_serving_period"):
        doc.custom_serving_period = orig_invoice.get("custom_serving_period")
    if orig_invoice and orig_invoice.get("custom_transaction_local_time"):
        doc.custom_transaction_local_time = orig_invoice.get("custom_transaction_local_time")
    if orig_invoice and orig_invoice.get("custom_transaction_weekday"):
        doc.custom_transaction_weekday = orig_invoice.get("custom_transaction_weekday")

    stamp_at = captured_at if captured_at is not None else (orig_invoice.get("ury_offline_captured_at") if orig_invoice else None)
    stamp_meta = stamp_invoice_outlet_time(doc, at=stamp_at)
    local_dt = stamp_meta["local_dt"]
    posting_date = getdate(local_dt.date() if local_dt else None)
    doc.posting_date = posting_date
    doc.posting_time = stamp_meta["local_time"]

    resolved = resolve_menu_and_price_list(
        doc.branch,
        restaurant=doc.restaurant,
        table=doc.restaurant_table,
        room=doc.custom_restaurant_room,
        order_type=effective_order_type,
        customer=effective_customer,
        local_dt=local_dt,
    )
    orig_order_type = (orig_invoice.get("order_type") or "").strip() if orig_invoice else ""
    same_order_type = bool(orig_invoice) and (not orig_order_type or orig_order_type == effective_order_type)
    active_price_list = resolved.get("price_list") or profile.get("selling_price_list")
    price_list = (
        (orig_invoice.get("selling_price_list") if same_order_type and orig_invoice.get("selling_price_list") else None)
        or active_price_list
    )
    doc.selling_price_list = price_list
    if getattr(doc, "flags", None) is not None:
        doc.flags.ury_authoritative_price_list = price_list

    cost_center = profile.get("cost_center") or (
        frappe.db.get_value("POS Profile", pos_profile, "cost_center") if pos_profile else None
    )
    warehouse = profile.get("warehouse")

    privileged_snapshots = {}
    if not items and orig_invoice is not None:
        for row in orig_invoice.get("items") or []:
            if cint(row.get("is_disposable")) == 1:
                continue
            _append_orig_preview_row(
                doc,
                row,
                cost_center=cost_center,
                warehouse=warehouse,
                privileged_snapshots=privileged_snapshots,
                orig_invoice=orig_invoice,
            )
    else:
        matched_orig_rows = (
            _match_preview_items_to_orig_rows(items, orig_invoice.get("items") or [])
            if orig_invoice is not None
            else {}
        )
        new_line_price_list = active_price_list or price_list
        modifier_price_overrides = {}
        if _has_modifier_or_combo_metadata(items):
            from ury.ury.api.ury_modifiers import validate_and_normalize_order_items

            rate_precision = 2
            if callable(getattr(doc, "precision", None)):
                try:
                    p = doc.precision("rate")
                    if isinstance(p, int) and not isinstance(p, bool):
                        rate_precision = p
                except Exception:
                    pass
            if doc.get("currency"):
                try:
                    frac = cint(frappe.db.get_value("Currency", doc.currency, "fraction_units"))
                    if frac >= 1:
                        rate_precision = max(0, len(str(frac)) - 1)
                except Exception:
                    pass
            past_item = (
                [
                    dict(r.as_dict() if callable(getattr(r, "as_dict", None)) else r)
                    for r in (orig_invoice.get("items") or [])
                    if cint(r.get("is_disposable")) != 1
                ]
                if orig_invoice is not None
                else []
            )
            preview_menu = resolved.get("menu") or (
                frappe.db.get_value("URY Menu", {"branch": doc.branch}, "name") if doc.branch else None
            )
            items, modifier_price_overrides = validate_and_normalize_order_items(
                items,
                past_item,
                preview_menu,
                new_line_price_list,
                doc.branch,
                doc.company,
                posting_date=posting_date,
                customer=effective_customer,
                currency_precision=rate_precision,
            )

        release_field, released = (None, None)
        if new_line_price_list:
            try:
                release_field, released = item_price_release_states()
            except Exception:
                release_field, released = (None, None)

        for idx, raw in enumerate(items):
            code = raw.get("item") or raw.get("item_code")
            qty = flt(raw.get("qty") or raw.get("quantity") or 0)
            if not code or qty <= 0:
                continue
            orig_row = matched_orig_rows.get(idx)
            if orig_row is not None and (_is_privileged_preview_row(orig_row, orig_invoice=orig_invoice) or same_order_type):
                _append_orig_preview_row(
                    doc,
                    orig_row,
                    qty=qty,
                    cost_center=cost_center,
                    warehouse=warehouse,
                    privileged_snapshots=privileged_snapshots,
                    orig_invoice=orig_invoice,
                )
                continue
            line_key = raw.get("reservation_line_key")
            mod_override = modifier_price_overrides.get(line_key) if line_key else None
            resolved_rate = None
            if mod_override and mod_override.get("is_validated_modifier"):
                resolved_rate = flt(mod_override["rate"])
            elif new_line_price_list:
                try:
                    ip = price_valid_on(
                        code, new_line_price_list, posting_date, release_field, released, customer=effective_customer
                    )
                    if ip and ip.get("price_list_rate") is not None:
                        resolved_rate = flt(ip.get("price_list_rate"))
                except Exception:
                    resolved_rate = None
            if resolved_rate is None:
                resolved_rate = flt(raw.get("rate") or raw.get("price") or 0)
            item_name = raw.get("item_name") or frappe.db.get_value("Item", code, "item_name") or code
            item_entry = {
                "item_code": code,
                "item_name": item_name,
                "qty": qty,
                "rate": resolved_rate,
                "price_list_rate": resolved_rate,
                "base_price_list_rate": resolved_rate,
                "cost_center": (orig_row.get("cost_center") if orig_row else None) or cost_center,
            }
            eff_warehouse = (orig_row.get("warehouse") if orig_row else None) or warehouse
            if eff_warehouse:
                item_entry["warehouse"] = eff_warehouse
            for tax_field in ("item_tax_template", "item_tax_rate", "custom_tax_exempt"):
                if raw.get(tax_field) is not None:
                    item_entry[tax_field] = raw.get(tax_field)
                elif orig_row is not None and orig_row.get(tax_field) is not None and tax_field == "custom_tax_exempt":
                    item_entry[tax_field] = orig_row.get(tax_field)
            for mod_field in _PREVIEW_MODIFIER_COMBO_FIELDS:
                if raw.get(mod_field) is not None:
                    item_entry[mod_field] = raw.get(mod_field)
            appended = doc.append("items", item_entry)
            if (
                (raw.get("custom_line_role") or "") == "Combo Upcharge"
                and appended is not None
                and mod_override
                and mod_override.get("is_validated_modifier")
            ):
                privileged_snapshots[id(appended)] = {
                    "price_list_rate": resolved_rate,
                    "base_price_list_rate": resolved_rate,
                    "rate": resolved_rate,
                    "base_rate": resolved_rate,
                    "discount_percentage": 0.0,
                    "discount_amount": 0.0,
                    "pricing_rules": None,
                    "ignore_pricing_rule": 0,
                    "custom_price_overridden": 0,
                    "custom_is_open_item": 0,
                    "custom_is_complimentary": 0,
                }

    disc_pct = flt(additional_discount_percentage)
    if disc_pct < 0 or disc_pct > 100:
        frappe.throw(_("Discount percentage must be between 0 and 100"), frappe.ValidationError)
    if disc_pct > 0:
        doc.additional_discount_percentage = disc_pct
        doc.discount_amount = 0
    elif orig_invoice and additional_discount_percentage in (None, "", 0, "0"):
        if flt(orig_invoice.get("additional_discount_percentage")) > 0:
            doc.additional_discount_percentage = flt(orig_invoice.get("additional_discount_percentage"))
            doc.discount_amount = 0
        elif flt(orig_invoice.get("discount_amount")) > 0:
            doc.additional_discount_percentage = 0
            doc.discount_amount = flt(orig_invoice.get("discount_amount"))
        else:
            doc.additional_discount_percentage = 0
            doc.discount_amount = 0
        if orig_invoice.get("apply_discount_on"):
            doc.apply_discount_on = orig_invoice.get("apply_discount_on")
    else:
        doc.additional_discount_percentage = 0
        doc.discount_amount = 0

    if coupon_code and getattr(doc, "meta", None) and doc.meta.has_field("coupon_code"):
        doc.coupon_code = coupon_code

    # Frappe caches per-document field currency under (doctype, doc.name) on frappe.local.field_currency;
    # for unsaved in-memory docs doc.name is None, so an earlier unsaved POS Invoice in another currency
    # (e.g. UGX 0dp before USD 2dp or BHD 3dp) would otherwise poison precision() on this preview.
    local_fc = getattr(getattr(frappe, "local", None), "field_currency", None)
    saved_none_fc = None
    if isinstance(local_fc, dict):
        saved_none_fc = {k: local_fc.pop(k) for k in list(local_fc.keys()) if isinstance(k, tuple) and len(k) == 2 and k[1] is None}

    try:
        apply_order_type_tax(doc, profile, customer_doc=customer_doc, posting_date=posting_date)
        doc_flags = getattr(doc, "flags", None)
        prev_priv_verified = bool(getattr(doc_flags, "ury_privileged_action_verified", False)) if doc_flags is not None else False
        if privileged_snapshots and doc_flags is not None:
            doc_flags.ury_privileged_action_verified = True
        try:
            doc.set_missing_values(for_validate=True)
        finally:
            if privileged_snapshots and doc_flags is not None:
                doc_flags.ury_privileged_action_verified = prev_priv_verified
        if privileged_snapshots:
            restored_names = set()
            for r in doc.get("items") or []:
                snap = privileged_snapshots.get(id(r))
                if snap is None:
                    continue
                for field, value in snap.items():
                    setattr(r, field, value)
                rname = r.get("name") if hasattr(r, "get") else getattr(r, "name", None)
                if rname:
                    restored_names.add(rname)
            pr_rows = doc.get("pricing_rules")
            if isinstance(pr_rows, list) and restored_names:
                kept_pr = [
                    pr
                    for pr in pr_rows
                    if (pr.get("child_docname") if hasattr(pr, "get") else getattr(pr, "child_docname", None))
                    not in restored_names
                ]
                if callable(getattr(doc, "set", None)):
                    doc.set("pricing_rules", kept_pr)
                else:
                    doc.pricing_rules = kept_pr
        if not orig_invoice:
            _apply_preview_selling_pricing_rules(doc)
        try:
            from ury.ury_pos.pricing_actions import sync_service_charge

            sync_service_charge(doc, profile)
        except ImportError:
            pass
        doc.calculate_taxes_and_totals()

        prec = doc.precision("grand_total") if callable(getattr(doc, "precision", None)) else 2
        if not isinstance(prec, int) or isinstance(prec, bool):
            prec = 2
    finally:
        final_fc = getattr(getattr(frappe, "local", None), "field_currency", None)
        if isinstance(final_fc, dict):
            for k in list(final_fc.keys()):
                if isinstance(k, tuple) and len(k) == 2 and k[1] is None:
                    final_fc.pop(k, None)
            if saved_none_fc:
                final_fc.update(saved_none_fc)

    efris_warnings = validate_efris_readiness(doc, throw=False)

    return {
        "net_total": flt(doc.get("net_total"), prec),
        "discount_amount": flt(doc.get("discount_amount"), prec),
        "tax_rows": _build_preview_tax_rows(doc, prec),
        "total_taxes_and_charges": flt(doc.get("total_taxes_and_charges"), prec),
        "grand_total": flt(doc.get("grand_total"), prec),
        "rounded_total": flt(doc.get("rounded_total") or doc.get("grand_total"), prec),
        "rounding_adjustment": flt(doc.get("rounding_adjustment"), prec),
        "tax_category": doc.get("tax_category") or "",
        "taxes_and_charges": doc.get("taxes_and_charges") or "",
        "serving_period": doc.get("custom_serving_period") or None,
        "transaction_local_time": doc.get("custom_transaction_local_time") or None,
        "transaction_weekday": doc.get("custom_transaction_weekday") or None,
        "item_taxes": [
            {
                "item_code": r.get("item_code"),
                "item_tax_template": r.get("item_tax_template") or None,
                "item_tax_rate": r.get("item_tax_rate") or "{}",
                "rate": flt(r.get("rate"), prec),
                "net_amount": flt(r.get("net_amount", r.get("amount")), prec),
            }
            for r in (doc.get("items") or [])
        ],
        "efris_warnings": efris_warnings,
    }
