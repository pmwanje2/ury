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

    current_time = get_datetime()
    creation_time = kot_doc.creation

    production_time = current_time - creation_time
    production_time_minutes = production_time.total_seconds() / 60
    
    server_time_str = current_time.strftime("%H:%M:%S")
    
    kot_doc.db_set({
        "start_time_serv": server_time_str,
        "production_time": production_time_minutes,
        "order_status": "Served",
    })


@frappe.whitelist(methods=["POST"])
def recall_kot(name):
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(_("POST requests only"), frappe.PermissionError)

    kot_doc = frappe.get_doc("URY KOT", name)
    if not frappe.has_permission("URY KOT", "write", doc=kot_doc):
        frappe.throw(
            _("You do not have permission to modify this KOT."),
            frappe.PermissionError,
        )

    try:
        session_branch = getBranch()
    except Exception:
        if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
            session_branch = None
        else:
            raise

    if session_branch and kot_doc.branch != session_branch:
        frappe.throw(
            _("You do not have permission to modify KOTs from other branches."),
            frappe.PermissionError,
        )

    if kot_doc.order_status != "Served":
        frappe.throw(_("Only Served KOTs can be recalled."))

    # production_time records minutes from creation to serving. Unlike modified,
    # it is not extended by a later edit and includes the date across midnight.
    try:
        production_minutes = float(kot_doc.production_time)
        if not isfinite(production_minutes) or production_minutes < 0:
            raise ValueError
        served_at = get_datetime(kot_doc.creation) + timedelta(minutes=production_minutes)
    except (TypeError, ValueError, OverflowError):
        frappe.throw(_("This KOT has no valid serving time."))

    elapsed = (get_datetime() - served_at).total_seconds()
    if not 0 <= elapsed <= 15 * 60:
        frappe.throw(_("KOTs can only be recalled within 15 minutes of serving."))

    kot_doc.db_set("order_status", "Ready For Prepare")
    kot_doc.add_comment("Comment", _("Recalled by {0}").format(frappe.session.user))


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
        frappe.throw(f"URY KOT {name} not found.", frappe.DoesNotExistError)

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


KOT_DISPLAY_WINDOW_HOURS = 3
KOT_DISPLAY_TYPES = [
    "New Order",
    "Order Modified",
    "Duplicate",
    "Cancelled",
    "Partially cancelled",
]


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
                frappe.utils.now(), hours=-KOT_DISPLAY_WINDOW_HOURS
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
    # Keep the fork's branch-bound board authority, including for administrators.
    branch = getBranch()
    cancelled = _get_cancel_confirmed_original_kots(branch)

    def count(order_status):
        rows = frappe.get_list(
            "URY KOT",
            fields=["name", "production"],
            filters=_kot_display_filters(order_status, branch),
            limit_page_length=0,
        )
        out = {}
        for r in rows:
            if r.name in cancelled or not r.production:
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


@frappe.whitelist()
def kot_list():
    today = frappe.utils.now()
    branch = getBranch()
    kot_alert_time = frappe.db.get_value(
        "POS Profile", {"branch": branch}, "custom_kot_warning_time"
    )
    daily_order_number = frappe.db.get_value(
        "POS Profile", {"branch": branch}, "custom_reset_order_number_daily"
    )
    audio_alert = frappe.db.get_value(
        "POS Profile", {"branch": branch}, "custom_kot_alert"
    )
    audio_file = frappe.db.get_value(
        "POS Profile", {"branch": branch}, "custom_kot_alert_sound"
    )
    kotList = frappe.get_list(
        "URY KOT",
        fields=["name"],
        filters=_kot_display_filters("Ready For Prepare", branch),
        order_by="creation desc",
    )
    production_filters = {}
    cancelled = _get_cancel_confirmed_original_kots(branch)
    KOT = []
    for kot in kotList:
        if kot.name in cancelled:
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
    branch = getBranch()
    kot_alert_time = frappe.db.get_value(
        "POS Profile", {"branch": branch}, "custom_kot_warning_time"
    )
    daily_order_number = frappe.db.get_value(
        "POS Profile", {"branch": branch}, "custom_reset_order_number_daily"
    )
    audio_alert = frappe.db.get_value(
        "POS Profile", {"branch": branch}, "custom_kot_alert"
    )
    kotList = frappe.get_list(
        "URY KOT",
        fields=["name"],
        filters=_kot_display_filters("Served", branch),
        order_by="creation desc",
    )
    production_filters = {}

    cancelled = _get_cancel_confirmed_original_kots(branch)
    KOT = []
    for kot in kotList:
        if kot.name in cancelled:
            continue
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
    return {
        "KOT": KOT,
        "Branch": branch,
        "kot_alert_time": kot_alert_time,
        "audio_alert": audio_alert,
        "daily_order_number":daily_order_number
    }
