import json
from collections import OrderedDict
from datetime import timedelta
from math import isfinite

import frappe
from frappe import _
from frappe.utils import get_datetime
from ury.brand import coded_message
from ury.ury_pos.api import getBranch


def _cint(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except (TypeError, ValueError):
            return 0
    return 0


def _flt(value):
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return 0.0
    return 0.0


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

    branch = getattr(kot_doc, "branch", None)
    if isinstance(branch, str) and branch and not is_kitchen_admin() and getBranch() != branch:
        frappe.throw(_("You cannot bump orders from another outlet."), frappe.PermissionError)

    invoice = _lock_course_fire_order(getattr(kot_doc, "invoice", None), getattr(kot_doc, "pos_profile", None))
    if invoice:
        if invoice.branch != branch:
            frappe.throw(_("You cannot bump orders from another outlet."), frappe.PermissionError)
        if frappe.db.get_value("URY KOT", name, "order_status", for_update=True) != "Ready For Prepare":
            return
    record_kot_served(name, kot_doc.creation, kot_doc)
    if invoice:
        _advance_course(invoice, [name])


def _lock_course_fire_order(invoice, profile):
    if not isinstance(invoice, str) or not invoice or not isinstance(profile, str) or not profile:
        return None
    if not _cint(frappe.db.get_value("POS Profile", profile, "auto_fire_next_course_on_bump")):
        return None

    return frappe.get_doc("POS Invoice", invoice, for_update=True)


def _advance_course(invoice, bumped):
    from ury.ury.doctype.ury_order.ury_order import _fire_next_held_course

    savepoint = "advance_kitchen_course"
    discard_release_effects = _capture_course_release_effects()
    frappe.db.savepoint(savepoint)
    try:
        _fire_next_held_course(invoice, bumped)
    except Exception:
        traceback = frappe.get_traceback()
        # Keep the completed bump and discard writes and queued notifications
        # from the failed release. The outer request still owns the commit.
        frappe.db.rollback(save_point=savepoint)
        discard_release_effects()
        frappe.log_error(title="Automatic kitchen course release failed", message=traceback)
    frappe.db.release_savepoint(savepoint)


def _capture_course_release_effects():
    try:
        from frappe.desk.form.linked_with import capture_pending_side_effects, discard_side_effects_since
    except ImportError:
        # Older v16 benches do not export Frappe's savepoint side-effect helpers.
        queues = {
            hook: getattr(frappe.db, hook)._functions
            for hook in ("before_commit", "after_commit", "before_rollback", "after_rollback")
        }
        counts = {hook: len(queue) for hook, queue in queues.items()}
        logs = {
            key: list(getattr(frappe.local, key) or []) if hasattr(frappe.local, key) else None
            for key in ("message_log", "_realtime_log", "_webhook_queue")
        }
        currently_saving = list(frappe.flags.currently_saving or [])

        def discard():
            frappe.flags.currently_saving = currently_saving
            for hook, queue in queues.items():
                callbacks = []
                while len(queue) > counts[hook]:
                    callbacks.append(queue.pop())
                if hook.endswith("_rollback"):
                    for callback in reversed(callbacks):
                        callback()
            for key, saved in logs.items():
                if saved is not None:
                    setattr(frappe.local, key, saved)
                elif hasattr(frappe.local, key):
                    delattr(frappe.local, key)

        return discard

    snapshot = capture_pending_side_effects()
    return lambda: discard_side_effects_since(snapshot)


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
KOT_RECALL_WINDOW_MAX_MINUTES = 240


def _parse_positive_int(raw):
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


def _kot_recall_window_minutes(branch=None, pos_profile=None, *, production=None):
    """Use the station's profile when assigned, then the ticket or outlet profile."""
    station_profile = (
        frappe.db.get_value("URY Production Unit", production, "pos_profile") if production else None
    )
    for profile in (station_profile, pos_profile):
        if isinstance(profile, str) and profile.strip():
            minutes = _parse_positive_int(frappe.db.get_value(
                "POS Profile", profile.strip(), "custom_kds_recall_window_minutes"
            ))
            if minutes is not None:
                return min(minutes, KOT_RECALL_WINDOW_MAX_MINUTES)
    if isinstance(branch, str) and branch.strip():
        minutes = _parse_positive_int(frappe.db.get_value(
            "POS Profile", {"branch": branch.strip(), "disabled": 0}, "custom_kds_recall_window_minutes"
        ))
        if minutes is not None:
            return min(minutes, KOT_RECALL_WINDOW_MAX_MINUTES)
    return KOT_RECALL_WINDOW_MINUTES


def is_kitchen_admin(user=None):
    """All-branch kitchen scope: the board, its counts and recall share this one rule."""
    user = user or frappe.session.user
    return user == "Administrator" or "URY Admin" in frappe.get_roles(user)


@frappe.whitelist(methods=["POST"])
def recall_kot(name):
    """Return a recently served KOT to the kitchen board within the configured recall window.

    Refusal keys (KOT-RECALL-*, emitted via coded_message) are append-only; never renumber or reuse one.
    """
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(coded_message("KOT-RECALL-POST-ONLY", _("POST requests only")), frappe.PermissionError)

    kot_doc = frappe.get_doc("URY KOT", name)
    if not frappe.has_permission("URY KOT", "write", doc=kot_doc):
        frappe.throw(
            coded_message("KOT-RECALL-NOT-PERMITTED", _("You do not have permission to modify this KOT.")),
            frappe.PermissionError,
        )

    session_branch = None if is_kitchen_admin() else getBranch()

    if session_branch and kot_doc.branch != session_branch:
        frappe.throw(
            coded_message("KOT-RECALL-OTHER-BRANCH", _("You do not have permission to modify KOTs from other branches.")),
            frappe.PermissionError,
        )

    locked_order = _lock_course_fire_order(
        getattr(kot_doc, "invoice", None), getattr(kot_doc, "pos_profile", None)
    )
    if locked_order:
        kot_doc = frappe.get_doc("URY KOT", name, for_update=True)

    if kot_doc.order_status != "Served":
        frappe.throw(coded_message("KOT-RECALL-NOT-SERVED", _("Only Served KOTs can be recalled.")), frappe.ValidationError)

    # production_time is minutes from creation to serving; unlike modified it is not
    # extended by a later edit and carries the date across midnight.
    try:
        production_minutes = float(kot_doc.production_time)
        if not isfinite(production_minutes) or production_minutes < 0:
            raise ValueError
        served_at = get_datetime(kot_doc.creation) + timedelta(minutes=production_minutes)
    except (TypeError, ValueError, OverflowError):
        frappe.throw(coded_message("KOT-RECALL-NO-SERVE-TIME", _("This KOT has no valid serving time.")), frappe.ValidationError)

    window_minutes = _kot_recall_window_minutes(
        branch=getattr(kot_doc, "branch", None),
        pos_profile=getattr(kot_doc, "pos_profile", None),
        production=getattr(kot_doc, "production", None),
    )
    elapsed = (get_datetime() - served_at).total_seconds()
    if not 0 <= elapsed <= window_minutes * 60:
        frappe.throw(
            coded_message(
                "KOT-RECALL-WINDOW-CLOSED",
                _("KOTs can only be recalled within {0} minutes of serving.").format(window_minutes),
            ),
            frappe.ValidationError,
        )

    kot_doc.db_set("order_status", "Ready For Prepare")
    kot_doc.add_comment("Comment", _("Recalled by {0}").format(frappe.session.user))


@frappe.whitelist()
def recent_served_kots(branch=None, production=None):
    """Return served tickets still within the branch POS Profile recall window."""
    session_branch = None if is_kitchen_admin() else getBranch()
    if session_branch and branch and branch != session_branch:
        frappe.throw(
            coded_message("KOT-RECALL-OTHER-BRANCH", _("You do not have permission to read KOTs from other branches.")),
            frappe.PermissionError,
        )
    effective_branch = session_branch or branch
    unit_pos_profile = None
    if production:
        unit_doc = frappe.get_doc("URY Production Unit", production)
        if not frappe.has_permission("URY Production Unit", "read", doc=unit_doc):
            frappe.throw(
                coded_message(
                    "KOT-RECALL-NOT-PERMITTED",
                    _("You do not have permission to read KOTs for this production station."),
                ),
                frappe.PermissionError,
            )
        unit_branch = (
            unit_doc.get("branch")
            if hasattr(unit_doc, "get")
            else getattr(unit_doc, "branch", None)
        )
        if session_branch and isinstance(unit_branch, str) and unit_branch and unit_branch != session_branch:
            frappe.throw(
                coded_message("KOT-RECALL-OTHER-BRANCH", _("You do not have permission to read KOTs from other branches.")),
                frappe.PermissionError,
            )
        if not effective_branch:
            effective_branch = (
                unit_branch
                if isinstance(unit_branch, str) and unit_branch
                else frappe.db.get_value("URY Production Unit", production, "branch")
            )
        unit_pos_profile = frappe.db.get_value("URY Production Unit", production, "pos_profile")
    window_minutes = _kot_recall_window_minutes(
        branch=effective_branch, pos_profile=unit_pos_profile
    )
    now_dt = get_datetime()
    # An expo station can include tickets from profiles with longer windows.
    cutoff = (now_dt - timedelta(minutes=KOT_RECALL_WINDOW_MAX_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")

    filters = {
        "order_status": "Served",
        "docstatus": 1,
        "type": ["not in", ["Cancelled", "Partially cancelled"]],
        "modified": [">=", cutoff],
    }
    if effective_branch:
        filters["branch"] = effective_branch

    is_expo = _is_expo_unit(production)
    if production and not is_expo:
        filters["production"] = production

    fields = [
        "name", "invoice", "production", "branch", "pos_profile", "restaurant_table",
        "table_takeaway", "order_no", "order_status", "creation", "production_time",
        "custom_guest_label",
    ]
    eligible = []
    windows = {}
    offset = 0
    while len(eligible) < 100:
        rows = frappe.get_list(
            "URY KOT", fields=fields, filters=filters, order_by="modified desc, name desc",
            limit_start=offset, limit_page_length=100,
        )
        for row in rows:
            try:
                prod_mins = float(row.get("production_time"))
                if not isfinite(prod_mins) or prod_mins < 0:
                    continue
                served_at = get_datetime(row.get("creation")) + timedelta(minutes=prod_mins)
            except (TypeError, ValueError, OverflowError):
                continue
            key = (row.get("branch") or effective_branch, row.get("pos_profile"), row.get("production"))
            if key not in windows:
                windows[key] = _kot_recall_window_minutes(key[0], key[1], production=key[2])
            row_window = windows[key]
            if 0 <= (now_dt - served_at).total_seconds() <= row_window * 60:
                row["recall_window_minutes"] = row_window
                eligible.append(row)
        if len(rows) < 100:
            break
        offset += len(rows)

    return {
        "kots": eligible[:100],
        "recall_window_minutes": window_minutes,
        "server_time": frappe.utils.now(),
    }


def _ensure_ready_before_serve(kot_name):
    """Fire the READY transition -- which attaches the real-time production
    posting intent -- for any item execution row still QUEUED or
    IN_PREPARATION."""
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
@frappe.whitelist(methods=["POST"])
def confirm_cancel_kot(name):
    manager_roles = {"URY Manager", "URY Admin", "System Manager"}
    if not manager_roles.intersection(frappe.get_roles()) and frappe.session.user != "Administrator":
        frappe.throw(
            _("Only a manager can confirm a cancelled KOT."),
            frappe.PermissionError,
        )

    if not name or not frappe.db.exists("URY KOT", name):
        frappe.throw(_("Kitchen ticket {0} was not found.").format(name), frappe.DoesNotExistError)

    message_log = getattr(frappe.local, "message_log", None)
    message_count = len(message_log) if message_log is not None else 0
    # Frappe's msgprint raises missing-document errors even when muted. Scope
    # suppression to the lookup so its internal DocType never reaches clients.
    previous_mute = getattr(frappe.flags, "mute_messages", False)
    frappe.flags.mute_messages = True
    try:
        kot_doc = frappe.get_doc("URY KOT", name)
    except frappe.DoesNotExistError:
        # Preserve earlier messages if a document hook wrote directly to the log.
        if message_log is not None:
            del message_log[message_count:]
        kot_doc = None
    finally:
        frappe.flags.mute_messages = previous_mute

    if kot_doc is None:
        frappe.throw(_("Kitchen ticket {0} was not found.").format(name), frappe.DoesNotExistError)

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

    invoice = _lock_course_fire_order(getattr(kot_doc, "invoice", None), getattr(kot_doc, "pos_profile", None))
    if invoice and frappe.db.get_value("URY KOT", name, "verified", for_update=True):
        return
    frappe.db.set_value("URY KOT", name, "verified", 1)
    frappe.db.set_value("URY KOT", name, "verified_by", frappe.session.user)
    if invoice and invoice.branch == kot_doc.branch and kot_doc.type in ("Cancelled", "Partially cancelled"):
        originals = (kot_doc.get("original_kot") or "").split(",")
        _advance_course(invoice, [original.strip() for original in originals if original.strip()])


@frappe.whitelist(methods=["POST"])
def set_check_priority(invoice, is_rush=None, is_vip=None, reason=None):
    """Flag a POS Invoice and all its active kitchen tickets as Rush and/or VIP (audited)."""
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(
            coded_message("KOT-PRIORITY-POST-ONLY", _("POST requests only")),
            frappe.PermissionError,
        )

    if not invoice:
        frappe.throw(
            coded_message("KOT-PRIORITY-INVOICE-REQUIRED", _("An invoice name is required.")),
            frappe.ValidationError,
        )

    if is_rush is None and is_vip is None:
        frappe.throw(
            coded_message("KOT-PRIORITY-NO-CHANGE", _("Specify Rush or VIP priority to update.")),
            frappe.ValidationError,
        )

    inv_doc = frappe.get_doc("POS Invoice", invoice)
    if not frappe.has_permission("POS Invoice", "write", doc=inv_doc):
        frappe.throw(
            coded_message("KOT-PRIORITY-NOT-PERMITTED", _("You do not have permission to prioritise this order.")),
            frappe.PermissionError,
        )

    session_branch = None if is_kitchen_admin() else getBranch()
    if session_branch and getattr(inv_doc, "branch", None) and inv_doc.branch != session_branch:
        frappe.throw(
            coded_message("KOT-PRIORITY-OTHER-BRANCH", _("You cannot prioritise orders in another branch.")),
            frappe.PermissionError,
        )

    updates = {}
    if is_rush is not None:
        updates["custom_is_rush"] = 1 if _cint(is_rush) else 0
    if is_vip is not None:
        updates["custom_is_vip"] = 1 if _cint(is_vip) else 0

    inv_doc.db_set(updates)

    labels = []
    if "custom_is_rush" in updates:
        labels.append("RUSH=ON" if updates["custom_is_rush"] else "RUSH=OFF")
    if "custom_is_vip" in updates:
        labels.append("VIP=ON" if updates["custom_is_vip"] else "VIP=OFF")
    audit_note = _("Kitchen priority updated ({0}) by {1}").format(
        ", ".join(labels), frappe.session.user
    )
    if reason and str(reason).strip():
        audit_note = f"{audit_note} — {str(reason).strip()}"
    inv_doc.add_comment("Comment", audit_note)

    active_kots = frappe.get_all(
        "URY KOT",
        filters={
            "invoice": invoice,
            "docstatus": 1,
            "order_status": ["!=", "Served"],
        },
        fields=["name", "production"],
    )
    updated_kots = []
    inv_branch = getattr(inv_doc, "branch", None)
    for row in active_kots:
        kot_name = row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
        if not kot_name:
            continue
        kot_doc = frappe.get_doc("URY KOT", kot_name)
        kot_doc.db_set(updates)
        if hasattr(kot_doc, "add_comment"):
            kot_doc.add_comment("Comment", audit_note)
        updated_kots.append(kot_name)
        prod = row.get("production") if isinstance(row, dict) else getattr(row, "production", None)
        if inv_branch and prod:
            frappe.publish_realtime(
                f"kot_priority_{inv_branch}_{prod}",
                {"invoice": invoice, "kot": kot_name, **updates},
                after_commit=True,
            )

    if inv_branch:
        frappe.publish_realtime(
            f"kot_priority_{inv_branch}",
            {"invoice": invoice, "updated_kots": updated_kots, **updates},
            after_commit=True,
        )

    final_rush = updates.get("custom_is_rush", _cint(getattr(inv_doc, "custom_is_rush", 0)))
    final_vip = updates.get("custom_is_vip", _cint(getattr(inv_doc, "custom_is_vip", 0)))
    return {
        "invoice": invoice,
        "custom_is_rush": final_rush,
        "custom_is_vip": final_vip,
        "updated_kots": updated_kots,
    }


@frappe.whitelist(methods=["POST"])
def expo_bump_order(invoice, production_unit=None, tickets=None):
    """Serve the active tickets displayed for an invoice on Expo."""
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(
            coded_message("KOT-EXPO-POST-ONLY", _("POST requests only")),
            frappe.PermissionError,
        )

    if not invoice:
        frappe.throw(
            coded_message("KOT-EXPO-INVOICE-REQUIRED", _("An invoice name is required.")),
            frappe.ValidationError,
        )

    if not frappe.has_permission("URY KOT", "write"):
        frappe.throw(
            coded_message("KOT-EXPO-NOT-PERMITTED", _("Not permitted to bump this order.")),
            frappe.PermissionError,
        )

    session_branch = None if is_kitchen_admin() else getBranch()
    if session_branch:
        if production_unit:
            unit_branch = frappe.db.get_value("URY Production Unit", production_unit, "branch")
            if isinstance(unit_branch, str) and unit_branch and unit_branch != session_branch:
                frappe.throw(
                    coded_message("KOT-EXPO-OTHER-BRANCH", _("You cannot bump orders from another branch.")),
                    frappe.PermissionError,
                )
        inv_branch = frappe.db.get_value("POS Invoice", invoice, "branch")
        if isinstance(inv_branch, str) and inv_branch and inv_branch != session_branch:
            frappe.throw(
                coded_message("KOT-EXPO-OTHER-BRANCH", _("You cannot bump orders from another branch.")),
                frappe.PermissionError,
            )

    # The board supplies the ticket names it displayed. Retries then cannot
    # accidentally bump the new course created by the original request.
    if tickets is None:
        frappe.throw(_("Select the displayed kitchen tickets to bump."))
    if isinstance(tickets, str):
        try:
            tickets = json.loads(tickets)
        except ValueError:
            frappe.throw(_("Invalid kitchen ticket selection."))
    if not isinstance(tickets, list) or any(not isinstance(name, str) or not name.strip() for name in tickets):
        frappe.throw(_("Invalid kitchen ticket selection."))
    if tickets == []:
        return {"invoice": invoice, "served_kots": []}

    profile = frappe.db.get_value("POS Invoice", invoice, "pos_profile")
    locked_invoice = _lock_course_fire_order(invoice, profile)
    if locked_invoice and session_branch and locked_invoice.branch != session_branch:
        frappe.throw(_("You cannot bump orders from another outlet."), frappe.PermissionError)

    filters = {
        "invoice": invoice,
        "docstatus": 1,
        "order_status": "Ready For Prepare",
        "type": ["not in", ["Cancelled", "Partially cancelled"]],
        "name": ["in", tickets],
    }

    kots = frappe.get_all("URY KOT", filters=filters, fields=["name", "branch"])
    if not kots:
        return {"invoice": invoice, "served_kots": []}

    kot_docs = []
    for row in kots:
        kot_name = row.get("name") if isinstance(row, dict) else getattr(row, "name", None)
        kot_branch = row.get("branch") if isinstance(row, dict) else getattr(row, "branch", None)
        if session_branch and isinstance(kot_branch, str) and kot_branch and kot_branch != session_branch:
            frappe.throw(
                coded_message("KOT-EXPO-OTHER-BRANCH", _("You cannot bump orders from another branch.")),
                frappe.PermissionError,
            )
        kot_doc = frappe.get_doc("URY KOT", kot_name)
        if not frappe.has_permission("URY KOT", "write", doc=kot_doc):
            frappe.throw(
                coded_message("KOT-EXPO-NOT-PERMITTED", _("Not permitted to bump this order.")),
                frappe.PermissionError,
            )
        if session_branch and getattr(kot_doc, "branch", None) and kot_doc.branch != session_branch:
            frappe.throw(
                coded_message("KOT-EXPO-OTHER-BRANCH", _("You cannot bump orders from another branch.")),
                frappe.PermissionError,
            )
        kot_docs.append((kot_name, kot_doc))

    served = []
    for kot_name, kot_doc in kot_docs:
        if locked_invoice and frappe.db.get_value(
            "URY KOT", kot_name, "order_status", for_update=True
        ) != "Ready For Prepare":
            continue
        record_kot_served(kot_name, kot_doc.creation, kot_doc)
        served.append(kot_name)

    if locked_invoice and served:
        _advance_course(locked_invoice, served)

    return {"invoice": invoice, "served_kots": served}


def _expo_held_branch(production_unit, permission):
    if not production_unit or not _is_expo_unit(production_unit):
        frappe.throw(_("Select an expo station."), frappe.PermissionError)
    station = frappe.get_doc("URY Production Unit", production_unit)
    branch = station.get("branch")
    if (not branch or not frappe.has_permission("URY Production Unit", "read", doc=station)
            or not frappe.has_permission("URY KOT", permission)
            or (not is_kitchen_admin() and getBranch() != branch)):
        frappe.throw(_("You do not have access to held items at this outlet."), frappe.PermissionError)
    return branch


@frappe.whitelist(methods=["GET"])
def list_held_kitchen_items(production_unit):
    """Only expo can see scheduled and confirmation-ready lines for its outlet."""
    branch = _expo_held_branch(production_unit, "read")
    rows = frappe.db.sql(
        """
        SELECT invoice.name AS invoice, invoice.restaurant_table, invoice.custom_ury_order_number AS order_no,
               item.name AS line, item.item_name, item.qty, item.custom_course AS course,
               item.fire_at, item.fire_confirmation_pending
        FROM `tabPOS Invoice Item` item
        JOIN `tabPOS Invoice` invoice ON invoice.name = item.parent
        JOIN `tabPOS Profile` profile ON profile.name = invoice.pos_profile
        WHERE invoice.branch = %s AND invoice.docstatus = 0
          AND profile.branch = invoice.branch AND profile.ury_enable_hold_fire = 1
          AND item.ury_held = 1 AND item.qty > 0 AND IFNULL(item.custom_parent_line_key, '') = ''
          AND (item.fire_at IS NOT NULL OR (
              item.fire_confirmation_pending = 1 AND profile.auto_fire_next_course_on_bump = 1
              AND profile.require_course_fire_confirmation = 1
          ))
        ORDER BY item.fire_at, invoice.name, item.idx
        """,
        (branch,),
        as_dict=True,
    )
    now = get_datetime()
    for row in rows:
        row["seconds_until_fire"] = (
            max(0, int((get_datetime(row["fire_at"]) - now).total_seconds()))
            if row["fire_at"] else None
        )
    return rows


@frappe.whitelist(methods=["POST"])
def fire_held_kitchen_items(production_unit, invoice, line):
    """Expo may override a timer or confirm a course marked ready by KDS."""
    branch = _expo_held_branch(production_unit, "write")
    from ury.ury.doctype.ury_order.ury_order import _release_held_rows, _selected_held_rows

    order = frappe.get_doc("POS Invoice", invoice, for_update=True)
    profile = frappe.get_doc("POS Profile", order.pos_profile)
    if (order.branch != branch or profile.get("branch") != branch
            or not _cint(profile.get("ury_enable_hold_fire")) or order.docstatus != 0):
        frappe.throw(_("You do not have access to held items on this check."), frappe.PermissionError)
    held = _selected_held_rows(order, line, None)
    if not held:
        return
    selected = next((row for row in held if row.name == line and not row.get("custom_parent_line_key")), None)
    if not selected:
        frappe.throw(_("Select a held kitchen line."))
    if not selected.get("fire_at"):
        if not (_cint(selected.get("fire_confirmation_pending"))
                and _cint(profile.get("auto_fire_next_course_on_bump"))
                and _cint(profile.get("require_course_fire_confirmation"))):
            frappe.throw(_("This course is not awaiting confirmation."), frappe.PermissionError)
        held = [
            row for row in _selected_held_rows(order, None, selected.get("custom_course"))
            if _cint(row.get("fire_confirmation_pending")) and not row.get("fire_at")
        ]
    _release_held_rows(order, held, kitchen_authority=True)


@frappe.whitelist(methods=["POST"])
def record_station_heartbeat(production_unit):
    """Record a KDS heartbeat timestamp for automatic station failover monitoring."""
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(
            coded_message("KOT-HEARTBEAT-POST-ONLY", _("POST requests only")),
            frappe.PermissionError,
        )
    if not production_unit:
        return {"status": "Ignored"}

    unit_doc = frappe.get_doc("URY Production Unit", production_unit)
    can_heartbeat = frappe.has_permission("URY Production Unit", "write", doc=unit_doc) or (
        frappe.has_permission("URY Production Unit", "read", doc=unit_doc)
        and frappe.has_permission("URY KOT", "write")
    )
    if not can_heartbeat:
        frappe.throw(
            coded_message("KOT-HEARTBEAT-NOT-PERMITTED", _("You do not have permission to update station heartbeat.")),
            frappe.PermissionError,
        )
    session_branch = None if is_kitchen_admin() else getBranch()
    if session_branch and getattr(unit_doc, "branch", None) and unit_doc.branch != session_branch:
        frappe.throw(
            coded_message("KOT-HEARTBEAT-OTHER-BRANCH", _("Cannot update station heartbeat for another branch.")),
            frappe.PermissionError,
        )

    now_dt = get_datetime()
    frappe.db.set_value(
        "URY Production Unit",
        production_unit,
        "custom_last_heartbeat",
        now_dt,
        update_modified=False,
    )
    return {"production_unit": production_unit, "last_heartbeat": str(now_dt)}


def _extract_station_device_config(production_unit):
    if not production_unit or not isinstance(production_unit, str):
        return None
    fields = [
        "custom_is_expo",
        "custom_device_mode",
        "custom_printer_transport",
        "custom_primary_printer",
        "custom_backup_printer",
        "custom_print_format",
        "custom_page_width",
        "custom_command_mode",
        "custom_failover_production_unit",
        "custom_auto_failover_enabled",
        "custom_failover_active",
        "custom_heartbeat_timeout_seconds",
        "custom_last_heartbeat",
    ]
    try:
        row = frappe.db.get_value("URY Production Unit", production_unit, fields, as_dict=True)
    except TypeError:
        return None
    if not isinstance(row, dict):
        return None

    primary = str(row.get("custom_primary_printer") or "").strip()
    backup = str(row.get("custom_backup_printer") or "").strip()
    transport = str(row.get("custom_printer_transport") or "None").strip()
    device_mode = str(row.get("custom_device_mode") or "KDS Display").strip()
    page_width = _parse_positive_int(row.get("custom_page_width")) or 576
    if page_width not in (576, 384):
        page_width = 576
    command_mode = str(row.get("custom_command_mode") or "standard").strip()
    if command_mode not in ("standard", "raster"):
        command_mode = "standard"

    def _split_target(value):
        if ":" in value and not value.startswith("/") and " " not in value:
            host_part, _, port_part = value.rpartition(":")
            if host_part and port_part.isdigit():
                return {"printer": "", "host": host_part, "port": int(port_part)}
        return {"printer": value, "host": "", "port": 9100}

    primary_target = _split_target(primary)
    backup_target = _split_target(backup)
    return {
        "production_unit": production_unit,
        "is_expo": bool(_cint(row.get("custom_is_expo"))),
        "device_mode": device_mode,
        "printer_transport": transport,
        "enabled": device_mode in ("Network Printer", "KDS + Auto Print") and bool(primary),
        "printer": primary_target["printer"],
        "host": primary_target["host"],
        "port": primary_target["port"],
        "backupPrinter": backup_target["printer"],
        "backupHost": backup_target["host"],
        "backupPort": backup_target["port"],
        "printFormat": str(row.get("custom_print_format") or "").strip(),
        "pageWidth": page_width,
        "commandMode": command_mode,
        "failover_production_unit": row.get("custom_failover_production_unit") or "",
        "auto_failover_enabled": bool(_cint(row.get("custom_auto_failover_enabled"))),
        "failover_active": bool(_cint(row.get("custom_failover_active"))),
        "heartbeat_timeout_seconds": _parse_positive_int(row.get("custom_heartbeat_timeout_seconds")) or 120,
        "last_heartbeat": str(row.get("custom_last_heartbeat") or ""),
    }


@frappe.whitelist()
def get_station_device_config(production_unit):
    """Return server-stored order device & backup printer settings for a Production Unit."""
    if not production_unit:
        return None
    unit_doc = frappe.get_doc("URY Production Unit", production_unit)
    if not frappe.has_permission("URY Production Unit", "read", doc=unit_doc):
        frappe.throw(
            coded_message("KOT-DEVICE-NOT-PERMITTED", _("You do not have permission to read station settings.")),
            frappe.PermissionError,
        )
    session_branch = None if is_kitchen_admin() else getBranch()
    if session_branch and getattr(unit_doc, "branch", None) and unit_doc.branch != session_branch:
        frappe.throw(
            coded_message("KOT-DEVICE-OTHER-BRANCH", _("Cannot read station settings for another branch.")),
            frappe.PermissionError,
        )
    return _extract_station_device_config(production_unit)


@frappe.whitelist(methods=["POST"])
def save_station_device_config(production_unit, config):
    """Persist unified station printer & backup printer config on `URY Production Unit`."""
    if frappe.request and frappe.request.method != "POST":
        frappe.throw(
            coded_message("KOT-DEVICE-POST-ONLY", _("POST requests only")),
            frappe.PermissionError,
        )
    if isinstance(config, str):
        config = json.loads(config)
    if not isinstance(config, dict):
        frappe.throw(
            coded_message("KOT-DEVICE-INVALID-CONFIG", _("Station device settings must be an object.")),
            frappe.ValidationError,
        )

    unit_doc = frappe.get_doc("URY Production Unit", production_unit)
    if not frappe.has_permission("URY Production Unit", "write", doc=unit_doc):
        frappe.throw(
            coded_message("KOT-DEVICE-NOT-PERMITTED", _("You do not have permission to update station settings.")),
            frappe.PermissionError,
        )

    session_branch = None if is_kitchen_admin() else getBranch()
    if session_branch and getattr(unit_doc, "branch", None) and unit_doc.branch != session_branch:
        frappe.throw(
            coded_message("KOT-DEVICE-OTHER-BRANCH", _("Cannot update station settings for another branch.")),
            frappe.PermissionError,
        )

    printer = str(config.get("printer") or "").strip()
    host = str(config.get("host") or "").strip()
    port = _cint(config.get("port") or 9100) or 9100
    primary_value = printer or (f"{host}:{port}" if host else "")

    backup_printer = str(config.get("backupPrinter") or "").strip()
    backup_host = str(config.get("backupHost") or "").strip()
    backup_port = _cint(config.get("backupPort") or 9100) or 9100
    backup_value = backup_printer or (f"{backup_host}:{backup_port}" if backup_host else "")

    page_width = str(config.get("pageWidth") or 576)
    if page_width not in ("576", "384"):
        page_width = "576"
    command_mode = str(config.get("commandMode") or "standard")
    if command_mode not in ("standard", "raster"):
        command_mode = "standard"

    enabled = bool(config.get("enabled"))
    unit_doc.custom_primary_printer = primary_value
    unit_doc.custom_backup_printer = backup_value
    unit_doc.custom_print_format = str(config.get("printFormat") or "").strip()
    unit_doc.custom_page_width = page_width
    unit_doc.custom_command_mode = command_mode
    if enabled:
        unit_doc.custom_device_mode = "KDS + Auto Print"
        unit_doc.custom_printer_transport = "QZ Tray"
    elif getattr(unit_doc, "custom_device_mode", None) == "KDS + Auto Print":
        unit_doc.custom_device_mode = "KDS Display"
    unit_doc.save()
    return _extract_station_device_config(production_unit)


@frappe.whitelist(allow_guest=True)
def get_site_name():
    return {"site_name": frappe.local.site}


def _attach_item_states(kot_jsons):
    """Stamp each kot_item dict with its URY KOT Item Execution ``state``."""
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


def group_items_by_course(kot_items):
    """Order KOT items by `serve_priority` and group them under distinct Course headers."""
    items = list(kot_items or [])
    items.sort(
        key=lambda row: (
            _cint(row.get("serve_priority") or 0),
            str(row.get("course") or ""),
            _cint(row.get("idx") or 0),
        )
    )
    grouped = OrderedDict()
    for item in items:
        course_name = str(item.get("course") or "").strip()
        prio = _cint(item.get("serve_priority") or 0)
        key = (prio, course_name)
        if key not in grouped:
            grouped[key] = {
                "course": course_name,
                "serve_priority": prio,
                "items": [],
            }
        grouped[key]["items"].append(item)
    return list(grouped.values())


def build_all_day_summary(kots):
    """Aggregate total open item quantities across active non-cancelled tickets."""
    totals = {}
    for kot in kots or []:
        if kot.get("type") in ("Cancelled", "Partially cancelled"):
            continue
        is_rush = bool(_cint(kot.get("custom_is_rush")))
        is_vip = bool(_cint(kot.get("custom_is_vip")))
        kot_name = kot.get("name")
        for item in kot.get("kot_items") or []:
            if item.get("state") == "SERVED":
                continue
            qty = _flt(item.get("quantity")) - _flt(item.get("cancelled_qty"))
            if qty <= 0:
                continue
            item_name = str(item.get("item_name") or item.get("item") or "").strip()
            if not item_name:
                continue
            item_code = str(item.get("item") or item_name).strip()
            course = str(item.get("course") or "").strip()
            key = (item_code, item_name, course)
            entry = totals.get(key)
            if entry is None:
                entry = {
                    "item": item_code,
                    "item_name": item_name,
                    "course": course,
                    "total_qty": 0.0,
                    "rush_qty": 0.0,
                    "vip_qty": 0.0,
                    "tickets": set(),
                }
                totals[key] = entry
            entry["total_qty"] = round(entry["total_qty"] + qty, 4)
            if is_rush:
                entry["rush_qty"] = round(entry["rush_qty"] + qty, 4)
            if is_vip:
                entry["vip_qty"] = round(entry["vip_qty"] + qty, 4)
            if kot_name:
                entry["tickets"].add(kot_name)

    summary = [
        {
            "item": entry["item"],
            "item_name": entry["item_name"],
            "course": entry["course"],
            "total_qty": entry["total_qty"],
            "rush_qty": entry["rush_qty"],
            "vip_qty": entry["vip_qty"],
            "ticket_count": len(entry["tickets"]),
        }
        for entry in totals.values()
    ]
    summary.sort(key=lambda r: (-r["rush_qty"], -r["total_qty"], r["item_name"].lower()))
    return summary


def sort_kots_for_display(kots):
    """Pin Rush tickets first, then VIP tickets, preserving relative order otherwise."""
    return sorted(
        kots,
        key=lambda k: (
            0 if _cint(k.get("custom_is_rush")) else 1,
            0 if _cint(k.get("custom_is_vip")) else 1,
        ),
    )


def _station_readiness_status(kot):
    if kot.get("order_status") == "Served":
        return "Served", True, len(kot.get("kot_items") or []), len(kot.get("kot_items") or [])
    items = kot.get("kot_items") or []
    if not items:
        return "Ready", True, 0, 0
    states = [str(item.get("state") or "QUEUED").upper() for item in items]
    ready_count = sum(1 for st in states if st in ("READY", "SERVED"))
    if ready_count == len(items):
        return "Ready", True, ready_count, len(items)
    if any(st in ("IN_PREPARATION", "READY", "SERVED") for st in states):
        return "Preparing", False, ready_count, len(items)
    return "Queued", False, ready_count, len(items)


def build_expo_orders(kots):
    """Group active KOTs across all branch stations by `invoice` for Expo expediting."""
    by_invoice = OrderedDict()
    for kot in kots or []:
        if kot.get("type") in ("Cancelled", "Partially cancelled"):
            continue
        invoice = kot.get("invoice") or kot.get("name")
        if invoice not in by_invoice:
            by_invoice[invoice] = {
                "invoice": invoice,
                "restaurant_table": kot.get("restaurant_table"),
                "table_takeaway": kot.get("table_takeaway"),
                "custom_merged_tables": kot.get("custom_merged_tables"),
                "custom_guest_label": kot.get("custom_guest_label"),
                "order_no": kot.get("order_no"),
                "user": kot.get("user"),
                "custom_is_rush": 0,
                "custom_is_vip": 0,
                "earliest_creation": kot.get("creation"),
                "earliest_time": kot.get("time"),
                "stations": [],
                "active_kot_names": [],
            }
        order = by_invoice[invoice]
        if _cint(kot.get("custom_is_rush")):
            order["custom_is_rush"] = 1
        if _cint(kot.get("custom_is_vip")):
            order["custom_is_vip"] = 1
        if kot.get("creation") and (
            not order["earliest_creation"] or str(kot["creation"]) < str(order["earliest_creation"])
        ):
            order["earliest_creation"] = kot.get("creation")
            order["earliest_time"] = kot.get("time")

        status_label, is_ready, ready_count, total_count = _station_readiness_status(kot)
        order["stations"].append({
            "kot_name": kot.get("name"),
            "production": kot.get("production") or "",
            "order_status": kot.get("order_status"),
            "type": kot.get("type"),
            "status": status_label,
            "all_items_ready": is_ready,
            "ready_items": ready_count,
            "total_items": total_count,
            "kot_items": kot.get("kot_items") or [],
            "course_groups": group_items_by_course(kot.get("kot_items")),
        })
        if kot.get("order_status") != "Served" and kot.get("name"):
            order["active_kot_names"].append(kot.get("name"))

    expo_orders = []
    for order in by_invoice.values():
        order["all_stations_ready"] = bool(order["stations"]) and all(
            st["all_items_ready"] for st in order["stations"]
        )
        expo_orders.append(order)

    return sort_kots_for_display(expo_orders)


def _is_expo_unit(production_unit):
    if not production_unit or not isinstance(production_unit, str):
        return False
    val = frappe.db.get_value("URY Production Unit", production_unit, "custom_is_expo")
    return bool(_cint(val)) if isinstance(val, (int, float, str, bool)) else False


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
                "orders": [],
            }

        summary[production]["active_orders"] += 1

        if kot.get("order_status") == "Ready For Prepare":
            summary[production]["ready_orders"] += 1
        else:
            summary[production]["pending_orders"] += 1

        summary[production]["orders"].append(kot)

    return list(summary.values())


def _get_cancel_confirmed_original_kots(branch, *, invoices=None):
    closed, _ = _get_kot_cancellation_state(branch, invoices=invoices)
    return closed


def _get_kot_cancellation_state(branch, *, invoice=None, invoices=None):
    """Return fully cancelled originals and originals awaiting confirmation."""
    filters = {
        "type": ["in", ["Cancelled", "Partially cancelled"]],
        "docstatus": 1,
    }
    if branch:
        filters["branch"] = branch
    if invoices is not None:
        if not invoices:
            return set(), set()
        filters["invoice"] = ["in", sorted(invoices)]

    fields = ["name", "type", "original_kot", "verified"]
    order_by = "creation asc, name asc"
    if invoice:
        # Course advancement already holds the order lock, including for
        # confirmations. Read the current state after acquiring that lock.
        cancel_rows = frappe.db.get_values(
            "URY KOT", {**filters, "invoice": invoice}, fields, as_dict=True,
            for_update=True, order_by=order_by,
        )
    else:
        cancel_rows = frappe.get_all("URY KOT", fields=fields, filters=filters, order_by=order_by)

    fully_cancelled = set()
    pending = set()
    partials = []
    for row in cancel_rows:
        originals = {name.strip() for name in (row.get("original_kot") or "").split(",") if name.strip()}
        verified = _cint(row.get("verified", 1))
        if not verified:
            pending.update(originals)
        if row.get("type") == "Partially cancelled":
            if originals:
                partials.append((row["name"], originals))
        elif verified:
            fully_cancelled.update(originals)

    # Replay claims in creation order. Earlier unverified claims must reserve
    # their units so a later confirmed cancellation cannot use them again.
    if partials:
        fully_cancelled.update(_fully_consumed_kots(partials, for_update=bool(invoice)))
    return fully_cancelled - pending, pending


def _fully_consumed_kots(partials, *, for_update):
    from ury.ury.api.ury_kot_generate import consume_kot_releases

    originals = set().union(*(names for _, names in partials))
    rows = frappe.db.get_values(
        "URY KOT Items", {"parent": ["in", sorted(originals | {name for name, _ in partials})]},
        ["parent", "item", "quantity", "cancelled_qty", "comments", "reservation_line_key", "course", "seat_no"],
        order_by="creation asc, parent asc, idx asc", as_dict=True, for_update=for_update,
    )
    releases = []
    cancel_items = {}
    for row in rows:
        if row["parent"] in originals:
            releases.append([row["parent"], row, _flt(row.get("quantity"))])
        else:
            cancel_items.setdefault(row["parent"], []).append(row)

    for ticket, referenced in partials:
        for row in cancel_items.get(ticket, []):
            consume_kot_releases(
                releases, row["item"], row.get("reservation_line_key"), row.get("comments"),
                _flt(row.get("cancelled_qty")), referenced, course=row.get("course"), seat=int(row.get("seat_no") or 0),
            )

    precision = frappe.get_precision("URY KOT Items", "quantity")
    return {name for name, _, _ in releases} - {
        name for name, _, quantity in releases if round(quantity, precision) > 0
    }


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
    hours = _parse_positive_int(hours) or 0
    if hours <= 0:
        return KOT_DISPLAY_WINDOW_HOURS
    return min(hours, KOT_DISPLAY_WINDOW_MAX_HOURS)


def _kot_display_filters(order_status, branch=None):
    """Single definition of which KOTs the Mosaic KDS shows."""
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
def kot_list(production_unit=None):
    today = frappe.utils.now()
    branch = None if is_kitchen_admin() else getBranch()
    availability_branch = branch
    if production_unit:
        unit_doc = frappe.get_doc("URY Production Unit", production_unit)
        if not frappe.has_permission("URY Production Unit", "read", doc=unit_doc):
            frappe.throw(
                coded_message(
                    "KOT-LIST-NOT-PERMITTED",
                    _("You do not have permission to view this production station."),
                ),
                frappe.PermissionError,
            )
        unit_branch = (
            unit_doc.get("branch")
            if hasattr(unit_doc, "get")
            else getattr(unit_doc, "branch", None)
        )
        if branch and isinstance(unit_branch, str) and unit_branch and unit_branch != branch:
            frappe.throw(
                coded_message(
                    "KOT-LIST-OTHER-BRANCH",
                    _("You cannot view kitchen tickets or station settings for another branch."),
                ),
                frappe.PermissionError,
            )
        if not availability_branch:
            availability_branch = (
                unit_branch
                if isinstance(unit_branch, str) and unit_branch
                else frappe.db.get_value("URY Production Unit", production_unit, "branch")
            )

    pos_profile_filters = {}
    if branch:
        pos_profile_filters["branch"] = branch

    kot_alert_time = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_warning_time"
    )
    daily_order_number = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_reset_order_number_daily"
    )
    audio_alert = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_alert"
    )
    audio_file = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_alert_sound"
    )
    unit_pos_profile = (
        frappe.db.get_value("URY Production Unit", production_unit, "pos_profile")
        if production_unit
        else None
    )
    recall_window_minutes = _kot_recall_window_minutes(
        branch=availability_branch or branch,
        pos_profile=unit_pos_profile,
    )
    is_expo = _is_expo_unit(production_unit)
    station_device_config = _extract_station_device_config(production_unit) if production_unit else None

    filters = _kot_display_filters("Ready For Prepare", branch)

    kotList = frappe.get_list(
        "URY KOT",
        fields=["name", "invoice"],
        filters=filters,
        order_by="creation desc",
    )
    cancelled_original_kots = _get_cancel_confirmed_original_kots(
        branch, invoices={kot.get("invoice") for kot in kotList if kot.get("invoice")},
    )
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
    for kotjson in KOT:
        kotjson["course_groups"] = group_items_by_course(kotjson.get("kot_items"))
    KOT = sort_kots_for_display(KOT)

    dashboard = build_dashboard_summary(KOT)
    summary_source = (
        KOT if (is_expo or not production_unit)
        else [k for k in KOT if k.get("production") == production_unit]
    )
    all_day_summary = build_all_day_summary(summary_source)
    expo_orders = build_expo_orders(KOT) if is_expo else []

    return {
        "KOT": KOT,
        "Dashboard": dashboard,
        "Branch": branch,
        "availability_branch": availability_branch,
        "roles": frappe.get_roles(),
        "server_time": today,
        "kot_alert_time": kot_alert_time,
        "audio_alert": audio_alert,
        "audio_file": audio_file,
        "daily_order_number": daily_order_number,
        "recall_window_minutes": recall_window_minutes,
        "is_expo": is_expo,
        "all_day_summary": all_day_summary,
        "expo_orders": expo_orders,
        "station_device_config": station_device_config,
    }


@frappe.whitelist()
def get_kds_summary(production_unit=None, branch=None):
    """Return All-Day aggregated item counts, course-grouped tickets, and Expo view."""
    session_branch = None if is_kitchen_admin() else getBranch()
    if session_branch and branch and branch != session_branch:
        frappe.throw(
            coded_message("KOT-SUMMARY-OTHER-BRANCH", _("You cannot read kitchen summary for another branch.")),
            frappe.PermissionError,
        )
    data = kot_list(production_unit=production_unit)
    kots = data["KOT"]
    if production_unit and not data["is_expo"]:
        kots = [k for k in kots if k.get("production") == production_unit]
    return {
        "branch": data["Branch"],
        "production_unit": production_unit,
        "is_expo": data["is_expo"],
        "recall_window_minutes": data["recall_window_minutes"],
        "all_day_summary": data["all_day_summary"],
        "expo_orders": data["expo_orders"],
        "course_grouped_kots": [
            {
                "name": k.get("name"),
                "invoice": k.get("invoice"),
                "production": k.get("production"),
                "custom_is_rush": _cint(k.get("custom_is_rush")),
                "custom_is_vip": _cint(k.get("custom_is_vip")),
                "course_groups": k.get("course_groups") or [],
            }
            for k in kots
        ],
        "total_active_kots": len(kots),
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
    audio_alert = frappe.db.get_value(
        "POS Profile", pos_profile_filters, "custom_kot_alert"
    )
    recall_window_minutes = _kot_recall_window_minutes(branch=branch)

    filters = _kot_display_filters("Served", branch)

    kotList = frappe.get_list(
        "URY KOT",
        fields=["name"],
        filters=filters,
        order_by="creation desc",
    )
    production_filters = {}

    KOT = []
    for kot in kotList:
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
    return {
        "KOT": KOT,
        "Branch": branch,
        "kot_alert_time": kot_alert_time,
        "audio_alert": audio_alert,
        "daily_order_number": daily_order_number,
        "recall_window_minutes": recall_window_minutes,
    }
