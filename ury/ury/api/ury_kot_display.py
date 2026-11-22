import json
from datetime import timedelta
from math import isfinite

import frappe
from ury.ury_pos.api import getBranch
from frappe.utils import get_datetime
from frappe import _

# Function to set order status in a KOT document
@frappe.whitelist(methods=["POST"])
def serve_kot(name, time=None):
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(_("POST requests only"), frappe.PermissionError)

    kot_doc = frappe.get_doc("URY KOT", name)
    if not frappe.has_permission("URY KOT", "write", doc=kot_doc):
        frappe.throw(_("Not permitted to serve this KOT"), frappe.PermissionError)

    if kot_doc.type in ("Cancelled", "Partially cancelled"):
        frappe.throw(_("KOT has been cancelled and cannot be served"), frappe.ValidationError)

    record_kot_served(name, kot_doc.creation, kot_doc)


def record_kot_served(name, creation_time, kot_doc=None):
    """READY (posting intent) then Served with timing; callers own authorization."""
    _ensure_ready_before_serve(name)
    if kot_doc is None:
        kot_doc = frappe.get_doc("URY KOT", name)

    current_time = get_datetime()

    production_time = current_time - creation_time
    production_time_minutes = production_time.total_seconds() / 60

    server_time_str = current_time.strftime("%H:%M:%S")

    # One document write, so on_change and Value Change notifications see the serve.
    kot_doc.db_set({
        "start_time_serv": server_time_str,
        "production_time": production_time_minutes,
        "order_status": "Served",
    })


KOT_RECALL_WINDOW_MINUTES = 15


def is_kitchen_admin(user=None):
    """All-branch kitchen scope: the board, its counts and recall share this one rule."""
    user = user or frappe.session.user
    return user == "Administrator" or "URY Admin" in frappe.get_roles(user)


@frappe.whitelist(methods=["POST"])
def recall_kot(name):
    """Return a KOT served in the last 15 minutes to the kitchen board.

    Refusal ids ([URY-KOT-RECALL-*]) are append-only; never renumber or reuse one.
    """
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(_("[URY-KOT-RECALL-POST-ONLY] POST requests only"), frappe.PermissionError)

    kot_doc = frappe.get_doc("URY KOT", name)
    if not frappe.has_permission("URY KOT", "write", doc=kot_doc):
        frappe.throw(
            _("[URY-KOT-RECALL-NOT-PERMITTED] You do not have permission to modify this KOT."),
            frappe.PermissionError,
        )

    session_branch = None if is_kitchen_admin() else getBranch()

    if session_branch and kot_doc.branch != session_branch:
        frappe.throw(
            _("[URY-KOT-RECALL-OTHER-BRANCH] You do not have permission to modify KOTs from other branches."),
            frappe.PermissionError,
        )

    if kot_doc.order_status != "Served":
        frappe.throw(_("[URY-KOT-RECALL-NOT-SERVED] Only Served KOTs can be recalled."), frappe.ValidationError)

    # production_time is minutes from creation to serving; unlike modified it is not
    # extended by a later edit and carries the date across midnight.
    try:
        production_minutes = float(kot_doc.production_time)
        if not isfinite(production_minutes) or production_minutes < 0:
            raise ValueError
        served_at = get_datetime(kot_doc.creation) + timedelta(minutes=production_minutes)
    except (TypeError, ValueError, OverflowError):
        frappe.throw(_("[URY-KOT-RECALL-NO-SERVE-TIME] This KOT has no valid serving time."), frappe.ValidationError)

    elapsed = (get_datetime() - served_at).total_seconds()
    if not 0 <= elapsed <= KOT_RECALL_WINDOW_MINUTES * 60:
        frappe.throw(
            _("[URY-KOT-RECALL-WINDOW-CLOSED] KOTs can only be recalled within 15 minutes of serving."),
            frappe.ValidationError,
        )

    kot_doc.db_set("order_status", "Ready For Prepare")
    kot_doc.add_comment("Comment", _("Recalled by {0}").format(frappe.session.user))


def _ensure_ready_before_serve(kot_name):
    """Fire the READY transition -- which attaches the real-time production
    posting intent -- for any item execution row still QUEUED or
    IN_PREPARATION.

    Every serve surface funnels through :func:`serve_kot` for the final
    ``order_status`` flip, but only the Mosaic kitchen display calls
    ``mark_item_ready`` on the way. Served without READY, an order never
    creates its posting intent: MTO Work Orders stay "In Process" forever
    and the Manufacture Stock Entry is never posted. Rows already READY
    (the normal kitchen-display path) are untouched, and a failure here is
    logged without blocking the customer-facing serve -- the transition's
    own savepoint guarantees a failed READY never half-applies.
    """
    from ury.ury.api.ury_kot_item_execution_service import mark_item_ready

    pending_rows = frappe.get_all(
        "URY KOT Item Execution",
        filters={"kot": kot_name, "state": ["in", ("QUEUED", "IN_PREPARATION")]},
        pluck="kot_item",
    )
    for kot_item in pending_rows:
        try:
            mark_item_ready(kot_item, f"serve-auto-ready:{kot_name}:{kot_item}")
        except Exception:
            frappe.log_error(
                title="ury_kot_display._ensure_ready_before_serve",
                message=frappe.get_traceback(),
            )


# Function to mark it as verified in a cancel type KOT.
# The verifying user is derived from the session and must hold a manager-level
# role, so confirmation cannot be self-attributed or forged by the caller.
@frappe.whitelist()
def confirm_cancel_kot(name):
    manager_roles = {"URY Manager", "URY Admin", "System Manager"}
    if not manager_roles.intersection(frappe.get_roles()) and frappe.session.user != "Administrator":
        frappe.throw(
            "Only a manager can confirm a cancelled KOT.",
            frappe.PermissionError,
        )

    # Fetch the KOT document
    try:
        kot_doc = frappe.get_doc("URY KOT", name)
    except frappe.DoesNotExistError:
        frappe.throw(f"Restaurant KOT {name} not found.", frappe.DoesNotExistError)

    # Document-level permission check
    if not frappe.has_permission("URY KOT", "write", doc=kot_doc):
        frappe.throw(
            "You do not have permission to modify this KOT.",
            frappe.PermissionError
        )

    # Branch-level permission check
    try:
        session_branch = getBranch()
    except Exception:
        if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
            session_branch = None
        else:
            raise

    if session_branch and kot_doc.branch != session_branch:
        frappe.throw(
            "You do not have permission to modify KOTs from other branches.",
            frappe.PermissionError
        )

    frappe.db.set_value("URY KOT", name, "verified", 1)
    frappe.db.set_value("URY KOT", name, "verified_by", frappe.session.user)


@frappe.whitelist(allow_guest=True)
def get_site_name():
    return {"site_name": frappe.local.site}

def _attach_item_states(kot_jsons):
    """Stamp each kot_item dict with its URY KOT Item Execution ``state``.

    One batched query for all KOTs (no N+1). Items with no execution row are
    implicitly QUEUED. Plain fields only, so it is safe on v15 and v16.
    """
    kot_names = [k.get("name") for k in kot_jsons if k.get("name")]
    state_by_item = {}
    if kot_names:
        rows = frappe.get_all(
            "URY KOT Item Execution",
            filters={"kot": ["in", kot_names]},
            fields=["kot_item", "state"],
        )
        state_by_item = {r.kot_item: r.state for r in rows}
    for kot in kot_jsons:
        for item in kot.get("kot_items") or []:
            item["state"] = state_by_item.get(item.get("name")) or "QUEUED"
    return kot_jsons


def build_dashboard_summary(kot_list):
    summary = {}

    for kot in kot_list:
        production = kot.get("production")

        if not production:
            continue

        if production not in summary:
            summary[production] = {
                "name": production,
                "active_orders": 0,
                "pending_orders": 0,
                "ready_orders": 0,
                "orders": []
            }

        summary[production]["active_orders"] += 1

        if kot.get("order_status") == "Ready For Prepare":
            summary[production]["ready_orders"] += 1
        else:
            summary[production]["pending_orders"] += 1

        summary[production]["orders"].append(kot)

    return list(summary.values())

def _get_cancel_confirmed_original_kots(branch):
    """Names of KOTs that a verified cancel-KOT references via `original_kot`.

    `original_kot` is a comma-separated string of KOT names stamped onto the
    cancel-KOT record at creation time (see `ury_kot_generate.py`), not a
    link field, so it has to be matched with LIKE and split in Python rather
    than joined in the query.
    """
    filters = {
        "type": ["in", ["Cancelled", "Partially cancelled"]],
        "verified": 1,
        "docstatus": 1,
    }
    if branch:
        filters["branch"] = branch

    cancel_rows = frappe.get_all(
        "URY KOT",
        fields=["original_kot"],
        filters=filters,
    )
    original_names = set()
    for row in cancel_rows:
        if not row.get("original_kot"):
            continue
        original_names.update(name.strip() for name in row.get("original_kot").split(",") if name.strip())
    return original_names


KOT_DISPLAY_WINDOW_HOURS = 3  # default when the POS Profile sets no window
KOT_DISPLAY_WINDOW_MAX_HOURS = 24
KOT_DISPLAY_TYPES = [
    "New Order",
    "Order Modified",
    "Duplicate",
    "Cancelled",
    "Partially cancelled",
]


def _kot_display_window_hours(branch):
    """Hours of KOTs the board shows: the branch POS Profile setting, default 3, at most 24."""
    hours = None
    if branch:
        hours = frappe.db.get_value("POS Profile", {"branch": branch}, "ury_kds_display_window_hours")
    try:
        hours = int(hours or 0)
    except (TypeError, ValueError):
        hours = 0
    if hours <= 0:
        return KOT_DISPLAY_WINDOW_HOURS
    return min(hours, KOT_DISPLAY_WINDOW_MAX_HOURS)


def _kot_display_filters(order_status, branch=None):
    """Single definition of which KOTs the Mosaic KDS shows.

    Shared by the per-unit view (kot_list / served_kot_list) and the overview
    counts (production_unit_counts) so both always use the same time window.
    """
    filters = {
        "order_status": order_status,
        "type": ["in", KOT_DISPLAY_TYPES],
        "docstatus": 1,
        "verified": 0,
        "creation": (
            ">=",
            frappe.utils.add_to_date(
                frappe.utils.now(), hours=-_kot_display_window_hours(branch)
            ),
        ),
    }
    if branch:
        filters["branch"] = branch
    return filters


@frappe.whitelist()
def production_unit_counts(units=None):
    """Active/served/total KOT counts per production unit, windowed like the unit view."""
    if isinstance(units, str):
        units = json.loads(units)
    branch = None if is_kitchen_admin() else getBranch()
    cancelled = _get_cancel_confirmed_original_kots(branch)
    production_filters = {}

    def count(order_status):
        rows = frappe.get_list(
            "URY KOT",
            fields=["name", "production", "invoice"],
            filters=_kot_display_filters(order_status, branch),
            limit_page_length=0,
        )
        out = {}
        for r in rows:
            if r.name in cancelled or not r.production:
                continue
            if not _matches_production_order_type(r, production_filters):
                continue
            out[r.production] = out.get(r.production, 0) + 1
        return out

    active, served = count("Ready For Prepare"), count("Served")
    names = units or sorted(set(active) | set(served))
    return {
        u: {
            "active_orders": active.get(u, 0),
            "served_orders": served.get(u, 0),
            "total_orders": active.get(u, 0) + served.get(u, 0),
        }
        for u in names
    }


def _matches_production_order_type(kot, production_filters):
    """The per-unit order-type restriction kot_list and served_kot_list apply, for the counts."""
    if not kot.production:
        return True
    if kot.production not in production_filters:
        prod_doc = frappe.get_doc("URY Production Unit", kot.production)
        production_filters[kot.production] = (
            [row.order_type for row in prod_doc.get("order_type", [])]
            if prod_doc.enable_order_type_wise_display_on_mosaic else None
        )
    allowed_order_types = production_filters[kot.production]
    return allowed_order_types is None or frappe.db.get_value(
        "POS Invoice", kot.invoice, "order_type"
    ) in allowed_order_types


@frappe.whitelist()
def kot_list():
    today = frappe.utils.now()
    branch = None if is_kitchen_admin() else getBranch()

    pos_profile_filters = {}
    if branch:
        pos_profile_filters["branch"] = branch

    kot_alert_time = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_warning_time"
    )
    daily_order_number = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_reset_order_number_daily"
    )
    three_hours_ago = frappe.utils.add_to_date(today, hours=-3)
    audio_alert = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_alert"
    )
    audio_file = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_alert_sound"
    )

    filters = _kot_display_filters("Ready For Prepare", branch)

    kotList = frappe.get_list(
        "URY KOT",
        fields=["name"],
        filters=filters,
        order_by="creation desc",
    )
    cancelled_original_kots = _get_cancel_confirmed_original_kots(branch)
    production_filters = {}
    KOT = []
    for kot in kotList:
        if kot.name in cancelled_original_kots:
            continue

        kotdoc = frappe.get_doc("URY KOT", kot.name)

        if kotdoc.production:
            if kotdoc.production not in production_filters:
                prod_doc = frappe.get_doc("URY Production Unit", kotdoc.production)
                if prod_doc.enable_order_type_wise_display_on_mosaic:
                    production_filters[kotdoc.production] = [row.order_type for row in prod_doc.get("order_type", [])]
                else:
                    production_filters[kotdoc.production] = None

            allowed_order_types = production_filters[kotdoc.production]
            if allowed_order_types is not None:
                invoice_order_type = frappe.db.get_value("POS Invoice", kotdoc.invoice, "order_type")
                if invoice_order_type not in allowed_order_types:
                    continue

        kotjson = json.loads(frappe.as_json(kotdoc))
        KOT.append(kotjson)
    _attach_item_states(KOT)
    dashboard = build_dashboard_summary(KOT)
    return {
        "KOT": KOT,
        "Dashboard": dashboard,
        "Branch": branch,
        "server_time": today,
        "kot_alert_time": kot_alert_time,
        "audio_alert": audio_alert,
        "audio_file": audio_file,
        "daily_order_number":daily_order_number
    }

@frappe.whitelist()
def served_kot_list():
    today = frappe.utils.now()
    branch = None if is_kitchen_admin() else getBranch()

    pos_profile_filters = {}
    if branch:
        pos_profile_filters["branch"] = branch

    kot_alert_time = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_warning_time"
    )
    daily_order_number = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_reset_order_number_daily"
    )
    three_hours_ago = frappe.utils.add_to_date(today, hours=-3)
    audio_alert = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_alert"
    )

    filters = _kot_display_filters("Served", branch)

    kotList = frappe.get_list(
        "URY KOT",
        fields=["name"],
        filters=filters,
        order_by="creation desc",
    )
    production_filters = {}

    print(kotList,"kotList..................")
    KOT = []
    for kot in kotList:
        kotdoc = frappe.get_doc("URY KOT", kot.name)
        print(kot.name,".................kotdoc")
        
        if kotdoc.production:
            if kotdoc.production not in production_filters:
                prod_doc = frappe.get_doc("URY Production Unit", kotdoc.production)
                if prod_doc.enable_order_type_wise_display_on_mosaic:
                    production_filters[kotdoc.production] = [row.order_type for row in prod_doc.get("order_type", [])]
                else:
                    production_filters[kotdoc.production] = None
            
            allowed_order_types = production_filters[kotdoc.production]
            if allowed_order_types is not None:
                invoice_order_type = frappe.db.get_value("POS Invoice", kotdoc.invoice, "order_type")
                if invoice_order_type not in allowed_order_types:
                    continue

        invoice=frappe.db.get_value("URY KOT",kot.name,"invoice")
        print(invoice,".....................invoice")
        kotjson = json.loads(frappe.as_json(kotdoc))
        KOT.append(kotjson)
    _attach_item_states(KOT)
    return {
        "KOT": KOT,
        "Branch": branch,
        "kot_alert_time": kot_alert_time,
        "audio_alert": audio_alert,
        "daily_order_number":daily_order_number
    }

