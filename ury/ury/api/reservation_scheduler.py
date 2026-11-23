# Copyright (c) 2026, Tridz Technologies Pvt. Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import now_datetime, get_datetime, cint
from datetime import timedelta
from ury.ury.api.table_reservation import (
    end_seated_visit,
    get_branch_reservation_settings,
    seated_visit_has_ended,
)


def process_reservation_no_shows():
    """
    Scheduled background task and inline API processor that automatically marks
    Confirmed reservations as 'No Show' when current time exceeds reservation_time + grace_period.
    Supports branch-specific custom_grace_period and exact expiration time (>=).
    It also ends a Seated visit that has no open check once its table was
    cleared or its hold ran out (Completed, with no bill).
    reserved_at is a site-local naive datetime, so it is compared with the site
    clock only; a booking is never judged against another time zone.
    """
    now = now_datetime()

    # Get all Confirmed and Seated reservations
    confirmed_reservations = frappe.db.get_all(
        "URY Table Reservation",
        filters={"status": ["in", ["Confirmed", "Seated"]]},
        fields=["name", "branch", "reserved_at", "reserved_table", "status", "pos_invoice"],
    )

    if not confirmed_reservations:
        return

    branch_settings_cache = {}
    updated_count = 0

    for res in confirmed_reservations:
        b = res.branch
        if not b and res.reserved_table:
            b = frappe.db.get_value("URY Table", res.reserved_table, "branch")

        if b not in branch_settings_cache:
            branch_settings_cache[b] = get_branch_reservation_settings(b)

        # A branch that turned reservations off keeps its records as they are.
        if not cint(branch_settings_cache[b].get("enable_reservation", 0)):
            continue

        if res.status == "Seated":
            if seated_visit_has_ended(res, branch_settings_cache[b], now) and end_seated_visit(res.name):
                updated_count += 1
            continue

        grace_mins = cint(branch_settings_cache[b].get("grace_period", 0))
        res_time = get_datetime(res.reserved_at)
        no_show_threshold = res_time + timedelta(minutes=grace_mins)

        is_overdue = now >= no_show_threshold

        if is_overdue:
            try:
                doc = frappe.get_doc("URY Table Reservation", res.name)
                # Double check status hasn't changed concurrently
                if doc.status == "Confirmed":
                    doc.status = "No Show"
                    # The site clock's own rule, not a user's Mark as No Show.
                    doc.flags.system_transition = ("Confirmed", "No Show")
                    try:
                        doc.save(ignore_permissions=True)
                    except Exception:
                        doc.db_set("status", "No Show")
                    updated_count += 1
            except Exception as e:
                frappe.log_error(
                    f"Failed to mark reservation {res.name} as No Show: {str(e)}",
                    "Reservation No-Show Error"
                )

    if updated_count > 0:
        frappe.db.commit()
