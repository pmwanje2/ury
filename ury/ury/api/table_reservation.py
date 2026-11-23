# Copyright (c) 2026, Tridz Technologies Pvt. Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import (
    get_datetime,
    now_datetime,
    add_to_date,
    flt,
    cint,
    get_time,
)
from datetime import datetime, timedelta, time
from ury.ury.report.average_table_time.average_table_time import (
    get_branch_last_day_avg_time,
    get_branch_last_week_avg_time,
    get_branch_reservation_duration,
)


# Reservation timing is Branch configuration (fixtures/custom_field.json), next to
# custom_buffer_time and custom_grace_period: settings key -> Branch field.
RESERVATION_TIMING_FIELDS = {
    "grace_period": "custom_grace_period",
    "unset_grace_policy": "custom_unset_grace_policy",
    "min_reservation_notice": "custom_min_reservation_notice",
    "target_turn_time": "custom_target_turn_time",
    "min_avg_table_time": "custom_min_avg_table_time",
    "seated_lookup_hours": "custom_seated_lookup_hours",
}
# custom_unset_grace_policy's option that stops a branch without a grace period switching reservations on.
REFUSE_TO_ENABLE = "Refuse to Enable"

ANOTHER_BOOKING_SAVED = "Another reservation for table {0} was saved at the same moment. Check the reservations and try again."


def parse_to_datetime(val, default_now=None):
    """
    Safely converts any datetime, date, time, timedelta, or string input into a valid datetime object.
    If input is a time or timedelta (seconds since midnight from MySQL Time field), combines it with default_now date.
    """
    if not default_now:
        default_now = now_datetime()

    if not val:
        return default_now

    if isinstance(val, datetime):
        return val

    if isinstance(val, timedelta):
        t_time = (datetime.min + val).time()
        return datetime.combine(default_now.date(), t_time)

    if isinstance(val, time):
        return datetime.combine(default_now.date(), val)

    if isinstance(val, str):
        val_str = val.replace("T", " ").strip()
        if " " in val_str:
            return get_datetime(val_str)
        else:
            t_obj = get_time(val_str)
            return datetime.combine(default_now.date(), t_obj)

    return get_datetime(val)


def reservations_enabled(branch):
    """Reservations are opt-in per Branch (custom_enable_reservation, default 0).

    A branch that has not enabled them sees no reservation reads or writes on
    its till, order, floor and closing paths.

    Before migrate adds the Branch columns (new code already served), the
    branch reads as disabled instead of failing POS Invoice validate, or
    booking without its timing settings.
    """
    if not branch:
        return False
    for column in ("custom_enable_reservation", *RESERVATION_TIMING_FIELDS.values()):
        if not frappe.db.has_column("Branch", column):
            return False
    return cint(frappe.db.get_value("Branch", branch, "custom_enable_reservation") or 0) == 1


def seeded_setting(fieldname):
    """The default a Branch reservation setting ships with: its Custom Field's own default."""
    field = frappe.get_meta("Branch").get_field(fieldname)
    return field.default if field else None


def _is_unset(value):
    return value is None or str(value).strip() == ""


def _timing_setting(values, key):
    """A Branch timing setting from a Branch row; a blank one takes its seeded default."""
    fieldname = RESERVATION_TIMING_FIELDS[key]
    value = values.get(fieldname)
    return seeded_setting(fieldname) if _is_unset(value) else value


def branch_timing_setting(branch, key):
    """Read one Branch timing setting on its own, for paths that need nothing else."""
    fieldname = RESERVATION_TIMING_FIELDS[key]
    return _timing_setting({fieldname: frappe.db.get_value("Branch", branch, fieldname)}, key)


def _grace_is_unset(grace):
    return _is_unset(grace) or cint(grace) <= 0


def _reservation_settings(branch_doc, branch=None):
    """Reservation settings from a Branch row (an empty one without a branch).

    A blank or zero grace takes the grace field's seed, so it never means No
    Show at the booked minute. Under "Refuse to Enable" such a branch cannot
    be saved with reservations on; a row that skipped that save still reads
    the seed.
    """
    buffer_time_raw = branch_doc.get("custom_buffer_time")
    if buffer_time_raw is None or str(buffer_time_raw).strip() == "":
        buffer_time = 0
    else:
        buffer_time = max(0, cint(buffer_time_raw))

    grace_period = branch_doc.get("custom_grace_period")
    if _grace_is_unset(grace_period):
        grace_period = seeded_setting("custom_grace_period")
    grace_period = max(0, cint(grace_period))

    target_turn_time = max(0, cint(_timing_setting(branch_doc, "target_turn_time")))
    min_avg_table_time = max(0, cint(_timing_setting(branch_doc, "min_avg_table_time")))
    calc_duration = get_branch_reservation_duration(
        branch, target_turn_time=target_turn_time, min_avg_table_time=min_avg_table_time)

    if branch:
        # A settings read never writes the Branch; the averages are
        # refreshed when a till closes on a branch with reservations enabled.
        last_day_avg = get_branch_last_day_avg_time(branch, update_branch=False)
        last_week_avg = get_branch_last_week_avg_time(branch, update_branch=False)
    else:
        last_day_avg = last_week_avg = 0

    return {
        "enable_reservation": cint(branch_doc.get("custom_enable_reservation") or 0),
        "buffer_time": buffer_time,
        "grace_period": grace_period,
        "unset_grace_policy": _timing_setting(branch_doc, "unset_grace_policy"),
        "min_reservation_notice": cint(_timing_setting(branch_doc, "min_reservation_notice")),
        "target_turn_time": target_turn_time,
        "min_avg_table_time": min_avg_table_time,
        "seated_lookup_hours": max(0, cint(_timing_setting(branch_doc, "seated_lookup_hours"))),
        "avg_table_time_last_day": round(flt(last_day_avg), 2),
        "avg_table_time_last_week": round(flt(last_week_avg), 2),
        "calculated_duration": cint(calc_duration),
    }


def validate_branch_reservation_settings(doc, method=None):
    """Branch validate: under "Refuse to Enable", a branch with no grace period cannot switch reservations on."""
    if not cint(doc.get("custom_enable_reservation") or 0):
        return
    if _timing_setting(doc, "unset_grace_policy") != REFUSE_TO_ENABLE:
        return
    if _grace_is_unset(doc.get("custom_grace_period")):
        frappe.throw(_("Set a grace period before switching reservations on for this branch."))


def ensure_reservations_enabled(branch):
    """Refuse a reservation write on a branch that has not switched reservations on."""
    if not reservations_enabled(branch):
        frappe.throw(_("Table reservations are switched off for this branch."))


def reservations_enabled_for_table(table):
    if not table:
        return False
    return reservations_enabled(frappe.db.get_value("URY Table", table, "branch"))


@frappe.whitelist()
def get_branch_reservation_settings(branch):
    """
    Returns reservation configuration and metrics for the specified branch.
    Without a branch, or when the read fails, reservations are off and each
    timing is its seeded default.
    """
    if not branch:
        return _reservation_settings({})

    try:
        branch_doc = frappe.db.get_value(
            "Branch",
            branch,
            [
                "custom_enable_reservation",
                "custom_buffer_time",
                *RESERVATION_TIMING_FIELDS.values(),
                "custom_avg_table_time_last_day",
                "custom_avg_table_time_last_week",
            ],
            as_dict=True,
        ) or {}
        return _reservation_settings(branch_doc, branch)
    except Exception as e:
        frappe.log_error(f"Error in get_branch_reservation_settings: {str(e)}", "Reservation Settings Error")
        return _reservation_settings({})


def validate_reservation_conflicts(table, branch, reserved_at, exclude_name=None):
    """
    Centralized validation engine to ensure that no overlapping reservations exist on the table.
    Considers:
      - Start Time: T_start
      - Expected Duration: D (from Average Table Time calculation)
      - Buffer Time: B (from Branch settings)
      - Protection window overlap: each booking holds its table over
        [T_start - B, T_start + D), and two holds on one table may not overlap.
    """
    if not table or not reserved_at:
        return

    current_now = now_datetime()
    new_start = parse_to_datetime(reserved_at, current_now)

    if not branch:
        branch = frappe.db.get_value("URY Table", table, "branch")

    settings = get_branch_reservation_settings(branch)
    new_hold_start = new_start - timedelta(minutes=settings["buffer_time"])
    new_end = new_start + timedelta(minutes=reservation_stay_minutes(settings))

    # Serialise bookings on one table: concurrent writers wait on this row lock.
    frappe.db.get_value("URY Table", table, "name", for_update=True)

    # A Seated party still holds its window.
    query = """
        SELECT name, reserved_at, branch
        FROM `tabURY Table Reservation`
        WHERE reserved_table = %s
          AND status IN ('Confirmed', 'Active', 'Requested', 'Seated')
    """
    params = [table]

    if exclude_name:
        query += " AND name != %s"
        params.append(exclude_name)

    # A locking read, after the table lock: under REPEATABLE READ a plain SELECT
    # answers from the snapshot this request opened before it waited, and misses
    # a booking the first writer committed meanwhile.
    query += " FOR UPDATE"
    try:
        existing_reservations = frappe.db.sql(query, tuple(params), as_dict=True)
    except frappe.QueryDeadlockError:
        # MariaDB 11.8 (innodb_snapshot_isolation=ON) raises 1020 when that read
        # meets a booking committed after this request's snapshot.
        frappe.throw(_(ANOTHER_BOOKING_SAVED).format(table))

    for ex in existing_reservations:
        ex_start = parse_to_datetime(ex.reserved_at, current_now)
        ex_branch = ex.branch or branch
        ex_settings = get_branch_reservation_settings(ex_branch)
        ex_hold_start = ex_start - timedelta(minutes=ex_settings["buffer_time"])
        ex_end = ex_start + timedelta(minutes=reservation_stay_minutes(ex_settings))

        # Conflict occurs if the two holds, buffer included, overlap
        if ex_hold_start < new_end and new_hold_start < ex_end:
            frappe.throw(
                _("Table {0} already has a reservation during the selected time. Please select a different reservation time.").format(table)
            )


def validate_reservation_rules(table, branch, reserved_at, no_of_pax=1, exclude_name=None):
    """
    Comprehensive validation order:
    1. Mandatory table check
    2. Capacity check (no_of_pax <= table.no_of_seats)
    3. Past datetime check (reserved_at >= now_datetime())
    4. Occupancy check & expected release calculation
    5. Overlapping reservation window conflicts check
    """
    if not table:
        frappe.throw(_("Please select a table."))

    table_data = frappe.db.get_value(
        "URY Table",
        table,
        ["name", "no_of_seats", "minimum_seating", "occupied", "latest_invoice_time", "branch", "restaurant_room"],
        as_dict=True,
    )
    if not table_data:
        frappe.throw(_("Table {0} does not exist.").format(table))

    if not branch:
        branch = table_data.branch
        if not branch and table_data.restaurant_room:
            branch = frappe.db.get_value("URY Room", table_data.restaurant_room, "branch")

    # 1. Capacity Validation
    table_seats = cint(table_data.get("no_of_seats") or 0)
    pax = cint(no_of_pax or 1)
    if table_seats > 0 and pax > table_seats:
        frappe.throw(_("Number of persons cannot exceed the table capacity of {0}.").format(table_seats))
    minimum_seats = cint(table_data.get("minimum_seating") or 0)
    if minimum_seats > 0 and pax < minimum_seats:
        frappe.throw(_("Number of persons must be at least the table minimum of {0}.").format(minimum_seats))

    # 2. Past Datetime Validation
    if not reserved_at:
        frappe.throw(_("Please select a reservation time."))

    current_now = now_datetime()
    res_start = parse_to_datetime(reserved_at, current_now)
    settings = get_branch_reservation_settings(branch)

    # The Branch's Minimum Reservation Notice. Its seed, -1, accepts a time one
    # minute back, for clock and network delay between the POS and the server.
    notice = settings["min_reservation_notice"]
    if res_start < current_now + timedelta(minutes=notice):
        if notice > 0:
            frappe.throw(_("A reservation needs at least {0} minutes' notice.").format(notice))
        frappe.throw(_("Reservation date and time cannot be in the past."))

    # 3. Buffer-Time & Table Occupancy Validation
    # Case A: Table is free (occupied == 0) -> proceed
    # Case B: Table is occupied (occupied == 1) -> check expected finish time
    if cint(table_data.get("occupied") or 0) == 1:
        active_inv_creation = frappe.db.get_value(
            "POS Invoice",
            {"restaurant_table": table, "docstatus": 0},
            "creation",
        )
        occupied_start_raw = active_inv_creation or table_data.get("latest_invoice_time") or current_now
        occupied_start_dt = parse_to_datetime(occupied_start_raw, current_now)

        expected_finish = occupied_start_dt + timedelta(minutes=settings["calculated_duration"])
        # The new booking holds the table from its buffer start, not its time.
        buffer_mins = settings["buffer_time"]
        res_hold_start = res_start - timedelta(minutes=buffer_mins)

        if expected_finish > res_hold_start:
            formatted_time = expected_finish.strftime("%I:%M %p").lstrip("0")
            frappe.throw(
                _("Table {0} is currently occupied and is expected to be available after {1}, and a booking needs it free {2} minutes before its time. Please select a later reservation time.").format(
                    table, formatted_time, buffer_mins
                )
            )

    # 4. Reservation Window Conflict Validation
    validate_reservation_conflicts(
        table=table,
        branch=branch,
        reserved_at=res_start,
        exclude_name=exclude_name,
    )


@frappe.whitelist()
def check_table_reservation(table):
    """
    Fetches the current active/protecting reservation or upcoming reservation for a table.
    """
    if not table or not reservations_enabled_for_table(table):
        return None

    try:
        branch = frappe.db.get_value("URY Table", table, "branch")
        settings = get_branch_reservation_settings(branch)
        buffer_mins = settings["buffer_time"]
        grace_mins = settings["grace_period"]
        duration_mins = settings["calculated_duration"]

        now = now_datetime()

        # Find all Confirmed reservations for this table
        reservations = frappe.db.get_all(
            "URY Table Reservation",
            filters={
                "reserved_table": table,
                "status": ["in", ["Confirmed", "Active"]],
            },
            fields=[
                "name",
                "branch",
                "reserved_table",
                "customer",
                "customer_name",
                "customer_phone",
                "no_of_pax",
                "reserved_at",
                "comments",
                "status",
            ],
            order_by="reserved_at asc",
        )

        if not reservations:
            return None

        # Priority 1: Check if any reservation is currently in its active lock window:
        # [reserved_at - buffer_time, reserved_at + grace_period). After the grace
        # end an unclaimed table is released, before the scheduler marks No Show.
        for res in reservations:
            res_time = get_datetime(res.reserved_at)
            lock_start = res_time - timedelta(minutes=buffer_mins)
            grace_end = res_time + timedelta(minutes=grace_mins)

            if lock_start <= now < grace_end:
                res["is_lock_window_active"] = True
                res["buffer_minutes"] = buffer_mins
                res["grace_minutes"] = grace_mins
                res["duration_minutes"] = duration_mins
                return res

        # Priority 2: Return next upcoming reservation for informational display
        for res in reservations:
            res_time = get_datetime(res.reserved_at)
            if res_time > now:
                res["is_lock_window_active"] = False
                res["buffer_minutes"] = buffer_mins
                res["grace_minutes"] = grace_mins
                res["duration_minutes"] = duration_mins
                return res

        # Otherwise return the first confirmed
        res = reservations[0]
        res["is_lock_window_active"] = False
        res["buffer_minutes"] = buffer_mins
        res["grace_minutes"] = grace_mins
        res["duration_minutes"] = duration_mins
        return res
    except Exception as e:
        frappe.log_error(f"Error in check_table_reservation: {str(e)}", "Reservation Error")
        return None


@frappe.whitelist()
def get_completed_reservation_for_table(table, branch=None):
    """
    Returns the recent Seated reservation for a table ONLY IF the table is currently
    Occupied (occupied = 1) and no submitted invoice exists yet for that reservation.
    If the table is Available (occupied = 0), returns None.
    The name is kept from upstream (ury-erp/ury#358), where arrival was "Completed";
    ury_order imports it under this name.
    """
    if not table:
        return None
    try:
        table_branch = branch or frappe.db.get_value("URY Table", table, "branch")
        if not reservations_enabled(table_branch):
            return None
        # Check current table state - MUST be currently occupied
        is_occupied = frappe.db.get_value("URY Table", table, "occupied")
        if not is_occupied or cint(is_occupied) != 1:
            return None

        # The Branch's Seated Party Lookup: how long ago the party may have been seated.
        lookup_hours = max(0, cint(branch_timing_setting(table_branch, "seated_lookup_hours")))
        now = now_datetime()
        filters = {
            "reserved_table": table,
            "status": "Seated",
            "modified": [">", add_to_date(now, hours=-lookup_hours)],
        }
        if branch:
            filters["branch"] = branch

        reservations = frappe.db.get_all(
            "URY Table Reservation",
            filters=filters,
            fields=["name", "customer", "customer_name", "customer_phone", "no_of_pax", "reserved_at", "pos_invoice"],
            order_by="modified desc",
            limit=1,
        )
        if reservations:
            res = reservations[0]
            if res.get("pos_invoice"):
                inv_status = frappe.db.get_value("POS Invoice", res.get("pos_invoice"), "docstatus")
                if inv_status == 1:
                    return None
            return res
        return None
    except Exception as e:
        frappe.log_error(f"Error in get_completed_reservation_for_table: {str(e)}", "Reservation Lookup Error")
        return None



def get_current_pos_opening_entry(branch):
    """
    Returns the active POS Opening Entry document for a branch if currently open.
    """
    if not branch:
        return None
    openings = frappe.db.get_all(
        "POS Opening Entry",
        filters={
            "branch": branch,
            "status": "Open",
            "docstatus": 1,
        },
        fields=["name", "period_start_date", "creation"],
        order_by="creation desc",
        limit=1,
    )
    if openings:
        return openings[0]
    return None


@frappe.whitelist()
def create_table_reservation(
    table,
    customer,
    reserved_at,
    customer_name=None,
    customer_phone=None,
    no_of_pax=1,
    notes=None,
    branch=None,
):
    """
    Creates a new table reservation with status 'Confirmed' after validating conflicts.
    Associates the reservation with the active POS session if one exists.
    """
    if isinstance(reserved_at, str) and "T" in reserved_at:
        reserved_at = reserved_at.replace("T", " ")

    if not branch:
        branch = frappe.db.get_value("URY Table", table, "branch")

    ensure_reservations_enabled(branch)

    if not customer_name and customer:
        customer_name = frappe.db.get_value("Customer", customer, "customer_name") or customer

    if not customer_phone and customer:
        customer_phone = frappe.db.get_value("Customer", customer, "mobile_number") or ""

    active_opening = get_current_pos_opening_entry(branch)

    doc_dict = {
        "doctype": "URY Table Reservation",
        "branch": branch,
        "reserved_table": table,
        "customer": customer,
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "no_of_pax": cint(no_of_pax) or 1,
        "reserved_at": reserved_at,
        "comments": notes,
        "status": "Confirmed",
    }
    if active_opening:
        doc_dict["pos_opening_entry"] = active_opening.name

    doc = frappe.get_doc(doc_dict)
    # Native DocPerm on URY Table Reservation decides who may reserve.
    try:
        doc.insert()
    except frappe.QueryDeadlockError:
        # Another booking was saved at the same moment (MariaDB 1213, or 1020 on
        # the naming series under innodb_snapshot_isolation=ON).
        frappe.throw(_(ANOTHER_BOOKING_SAVED).format(table))
    frappe.db.commit()
    return doc.name


@frappe.whitelist()
def update_table_reservation(
    reservation_name,
    table=None,
    customer=None,
    customer_name=None,
    customer_phone=None,
    no_of_pax=None,
    reserved_at=None,
    notes=None,
    branch=None,
):
    """
    Edits an existing reservation. If table or reserved_at changes, re-validates conflicts.
    """
    if not reservation_name:
        frappe.throw(_("Reservation ID is required."))

    doc = frappe.get_doc("URY Table Reservation", reservation_name)
    doc.check_permission("write")
    ensure_reservations_enabled(doc.branch)

    if doc.status not in ("Requested", "Confirmed", "Active"):
        frappe.throw(_("Cannot edit reservation in {0} status.").format(doc.status))

    if table:
        doc.reserved_table = table
        if not branch:
            doc.branch = frappe.db.get_value("URY Table", table, "branch")
    if branch:
        doc.branch = branch
    if customer:
        doc.customer = customer
    if customer_name:
        doc.customer_name = customer_name
    if customer_phone:
        doc.customer_phone = customer_phone
    if no_of_pax:
        doc.no_of_pax = cint(no_of_pax)
    if reserved_at:
        if isinstance(reserved_at, str) and "T" in reserved_at:
            reserved_at = reserved_at.replace("T", " ")
        doc.reserved_at = reserved_at
    if notes is not None:
        doc.comments = notes

    ensure_reservations_enabled(doc.branch)
    doc.save()
    frappe.db.commit()
    return True


def is_user_authorized_for_reservation_status_change(branch=None, user=None):
    """
    Checks whether the user is authorized to perform privileged reservation status changes
    (such as transitioning to Completed or Cancelled).
    Only Cashier (role_allowed_for_billing) or Restaurant Manager (transfer_role_permissions),
    or Administrator / System Manager are permitted. Order Takers are denied.
    """
    user = user or frappe.session.user
    if not user:
        return False

    user_roles = set(frappe.get_roles(user))
    if user == "Administrator" or "System Manager" in user_roles:
        return True

    # Find matching POS Profile(s) for the branch or user
    profiles = []
    if branch:
        profiles = frappe.db.get_all("POS Profile", filters={"branch": branch, "disabled": 0}, fields=["name"])

    if not profiles:
        user_profiles = frappe.db.get_all("POS Profile User", filters={"user": user}, fields=["parent as name"])
        if user_profiles:
            profiles = user_profiles

    if not profiles:
        profiles = frappe.db.get_all("POS Profile", filters={"disabled": 0}, fields=["name"], limit=5)

    for p in profiles:
        try:
            profile_doc = frappe.get_doc("POS Profile", p.name)
            billing_roles = {row.role for row in (profile_doc.role_allowed_for_billing or [])}
            manager_roles = {row.role for row in (profile_doc.transfer_role_permissions or [])}
            if (user_roles & billing_roles) or (user_roles & manager_roles):
                return True
        except Exception:
            continue

    return False


@frappe.whitelist()
def update_reservation_status(reservation_name, status, pos_invoice=None, cancel_reason=None):
    """
    Updates the status of a reservation (Seated, Completed, Cancelled, No Show) with validation.
    Seated is open to anyone with write permission: whoever greets the party seats it.
    """
    if not reservation_name:
        frappe.throw(_("Reservation ID is required."))

    doc = frappe.get_doc("URY Table Reservation", reservation_name)
    doc.check_permission("write")
    ensure_reservations_enabled(doc.branch or frappe.db.get_value("URY Table", doc.reserved_table, "branch"))

    # Validate role permissions for critical status changes
    if status in ("Completed", "Cancelled"):
        if not is_user_authorized_for_reservation_status_change(branch=doc.branch):
            frappe.throw(
                _("Not permitted to change reservation status to {0}.").format(status),
                frappe.PermissionError,
            )

    # Validate status transitions
    if status == "Completed":
        if doc.status in ("Cancelled", "No Show"):
            frappe.throw(_("Cannot mark a {0} reservation as Completed.").format(doc.status))

    if status == "Cancelled":
        if doc.status == "Completed":
            frappe.throw(_("Cannot cancel a reservation that is already Completed."))

    doc.status = status
    if pos_invoice:
        doc.pos_invoice = pos_invoice
    if cancel_reason:
        doc.cancel_reason = cancel_reason

    doc.save()
    frappe.db.commit()
    return True


def reservation_stay_minutes(settings):
    """How long a booking holds its table after its time: [reserved_at - buffer, reserved_at + stay).

    The last day's average table time, unless it is below the Branch's minimum;
    then the branch's calculated duration (an average, or its target turn time).
    """
    stay = flt(settings.get("avg_table_time_last_day"))
    if not stay or stay < settings["min_avg_table_time"]:
        return flt(settings["calculated_duration"])
    return stay


def open_checks_on(tables):
    """Open checks (draft POS Invoices) on any of these tables, as their own table or a merged one."""
    tables = {table for table in tables if table}
    if not tables:
        return []
    fields = ["name", "restaurant_table", "custom_merged_tables", "custom_split_group",
        "custom_split_from", "creation"]
    checks = {check.name: check for check in frappe.db.get_all(
        "POS Invoice", filters={"docstatus": 0, "restaurant_table": ["in", sorted(tables)]}, fields=fields)}
    for table in sorted(tables):
        for check in frappe.db.get_all("POS Invoice",
                filters={"docstatus": 0, "custom_merged_tables": ["like", f"%{table}%"]}, fields=fields):
            merged = {name.strip() for name in (check.custom_merged_tables or "").split(",")}
            if table in merged:
                checks[check.name] = check
    return list(checks.values())


def seated_visit_has_ended(reservation, settings, now):
    """A Seated party with no open check has gone when its table was cleared or its hold ran out."""
    if open_checks_on([reservation.reserved_table]):
        return False  # still ordering, or paying a printed check
    if not cint(frappe.db.get_value("URY Table", reservation.reserved_table, "occupied") or 0):
        return True
    hold_end = get_datetime(reservation.reserved_at) + timedelta(minutes=reservation_stay_minutes(settings))
    return now >= hold_end


def end_seated_visit(reservation_name):
    """Complete a Seated visit that ended without a bill.

    Saved under the session user's own authority; a user without it leaves the
    party Seated, and the scheduler (as Administrator) ends it on its next run.
    """
    try:
        reservation = frappe.get_doc("URY Table Reservation", reservation_name)
        if reservation.status != "Seated":
            return False
        reservation.status = "Completed"
        reservation.save()
        return True
    except frappe.PermissionError:
        return False
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Reservation visit end")
        return False


def sync_branch_table_occupancy(branch=None):
    """
    Ensures table occupied flags in `tabURY Table` accurately reflect active state.
    A table should be `occupied = 1` ONLY IF:
      1. An active draft POS Invoice (docstatus = 0) exists for the table, OR
      2. An active lock-window reservation exists for the table, OR
      3. A party is Seated there: it keeps the table until its bill closes, it
         is unseated, or process_reservation_no_shows ends its visit, so the
         reset never leaves a Seated reservation on a free table.
    Otherwise, if no active draft invoice and no active reservation lock exists, reset occupied = 0.
    """
    try:
        filters = {"occupied": 1}
        if branch:
            filters["branch"] = branch

        occupied_tables = frappe.db.get_all(
            "URY Table",
            filters=filters,
            fields=["name", "branch"]
        )

        if not occupied_tables:
            return

        for t in occupied_tables:
            table_name = t.name

            # 1. Check for active draft POS Invoice (docstatus = 0)
            has_draft_invoice = frappe.db.exists(
                "POS Invoice",
                {
                    "docstatus": 0,
                    "restaurant_table": table_name
                }
            )

            if not has_draft_invoice:
                # Check merged tables field as well
                merged_draft = frappe.db.sql(
                    """
                    SELECT name FROM `tabPOS Invoice`
                    WHERE docstatus = 0 AND custom_merged_tables LIKE %s
                    LIMIT 1
                    """,
                    (f"%{table_name}%",),
                    as_dict=True
                )
                has_draft_invoice = bool(merged_draft)

            if has_draft_invoice:
                continue

            # 2. Check for active lock-window reservation
            active_res = check_table_reservation(table_name)
            if active_res and active_res.get("is_lock_window_active") and active_res.get("status") in ("Confirmed", "Active"):
                continue

            # 3. A Seated party holds its table, ordered or not
            if frappe.db.exists("URY Table Reservation", {"reserved_table": table_name, "status": "Seated"}):
                continue

            # If no draft invoice, no active lock window, and no seated party -> table is NOT occupied
            frappe.db.set_value(
                "URY Table",
                table_name,
                {
                    "occupied": 0,
                    "latest_invoice_time": None
                },
                update_modified=False
            )
    except Exception as e:
        frappe.log_error(f"Error in sync_branch_table_occupancy: {str(e)}", "Table Sync Error")


@frappe.whitelist()
def get_active_reservations(branch=None, date=None):
    """
    Returns reservations for a branch based on calendar date.
    If date is provided (YYYY-MM-DD), returns reservations for that specific calendar day.
    If date is not provided, returns reservations for current calendar date onwards (today + all future dates).
    Past calendar dates (< today) are excluded.
    Auto-processes past-due Confirmed reservations to 'No Show' server-side.
    Cleans up stale table occupied flags if no open invoice/reservation exists.
    """
    try:
        if not reservations_enabled(branch):
            return []

        from ury.ury.api.reservation_scheduler import process_reservation_no_shows
        process_reservation_no_shows()
        sync_branch_table_occupancy(branch)

        now = now_datetime()
        today_date = now.date()
        today_start_str = f"{today_date.strftime('%Y-%m-%d')} 00:00:00"

        params = [branch]

        if date:
            # Parse requested date safely
            try:
                if isinstance(date, str):
                    target_date = datetime.strptime(date.strip()[:10], "%Y-%m-%d").date()
                elif hasattr(date, "date"):
                    target_date = date.date()
                else:
                    target_date = today_date
            except Exception:
                target_date = today_date

            day_start_str = f"{target_date.strftime('%Y-%m-%d')} 00:00:00"
            day_end_str = f"{target_date.strftime('%Y-%m-%d')} 23:59:59"
            date_clause = "AND reserved_at >= %s AND reserved_at <= %s"
            params.extend([day_start_str, day_end_str])
        else:
            # Current calendar date + all future calendar dates
            date_clause = "AND reserved_at >= %s"
            params.append(today_start_str)

        query = f"""
            SELECT
                name, branch, reserved_table, customer, customer_name,
                customer_phone, no_of_pax, reserved_at, comments, status, pos_opening_entry,
                cancel_reason, pos_invoice
            FROM `tabURY Table Reservation`
            WHERE branch = %s
              {date_clause}
            ORDER BY reserved_at ASC
        """

        reservations = frappe.db.sql(query, tuple(params), as_dict=True)

        now = now_datetime()
        branch_settings_cache = {}

        result = []
        for res in reservations:
            b = res.branch or branch
            if b not in branch_settings_cache:
                branch_settings_cache[b] = get_branch_reservation_settings(b)

            b_settings = branch_settings_cache[b]
            buf = b_settings["buffer_time"]
            grace = b_settings["grace_period"]
            duration = b_settings["calculated_duration"]

            res_time = get_datetime(res.reserved_at)
            lock_start = res_time - timedelta(minutes=buf)
            grace_end = res_time + timedelta(minutes=grace)

            is_active_status = res.get("status") in ("Confirmed", "Active")
            res["is_lock_window_active"] = is_active_status and (lock_start <= now < grace_end)
            res["buffer_minutes"] = buf
            res["grace_minutes"] = grace
            res["duration_minutes"] = duration
            res["lock_start_time"] = lock_start.strftime("%Y-%m-%d %H:%M:%S")

            result.append(res)

        return result
    except Exception as e:
        frappe.log_error(f"Error in get_active_reservations: {str(e)}", "Reservation Error")
        return []


def _party_fits(table, pax):
    seats = cint(table.get("no_of_seats") or 0)
    minimum = cint(table.get("minimum_seating") or 0)
    return (seats <= 0 or pax <= seats) and (minimum <= 0 or pax >= minimum)


@frappe.whitelist()
def get_available_tables_for_reservation(branch=None, reserved_at=None, exclude_reservation=None, no_of_pax=None):
    """
    Returns the list of tables in the branch that are available for the requested reservation date/time.
    Filters out:
    1. Tables that are currently occupied where expected finish time exceeds the requested
       hold's start (the reservation time less the buffer).
    2. Tables with another Confirmed/Active/Requested/Seated reservation whose hold (buffer
       included) overlaps the requested hold.
    3. When no_of_pax is given, tables the party does not fit (seats or minimum seating).
    (Excludes exclude_reservation so editing a reservation does not conflict with itself).
    """
    if not branch or not reserved_at or not reservations_enabled(branch):
        return []

    try:
        current_now = now_datetime()
        res_start = parse_to_datetime(reserved_at, current_now)

        # 1. Fetch all tables for the branch (excluding takeaway)
        tables = frappe.db.sql(
            """
            SELECT t.name, t.occupied, t.latest_invoice_time, t.is_take_away,
                   t.restaurant_room, t.table_shape, t.merged_with, t.no_of_seats,
                   t.layout_x, t.layout_y, t.minimum_seating, t.branch
            FROM `tabURY Table` t
            LEFT JOIN `tabURY Room` r ON t.restaurant_room = r.name
            WHERE (t.branch = %s OR r.branch = %s)
              AND (t.is_take_away = 0 OR t.is_take_away IS NULL)
            ORDER BY t.restaurant_room ASC, t.name ASC
            """,
            (branch, branch),
            as_dict=True,
        )

        if not tables:
            return []

        # 2. Get Branch reservation settings for duration calculation
        settings = get_branch_reservation_settings(branch)
        res_hold_start = res_start - timedelta(minutes=settings["buffer_time"])
        res_end = res_start + timedelta(minutes=reservation_stay_minutes(settings))

        expected_duration = settings["calculated_duration"]

        # 3. Check current occupancy conflicts
        occupied_tables = {t.name: t for t in tables if cint(t.get("occupied") or 0) == 1}
        occupied_conflicts = set()

        if occupied_tables:
            table_names = list(occupied_tables.keys())
            active_invoices = frappe.db.get_all(
                "POS Invoice",
                filters={"restaurant_table": ["in", table_names], "docstatus": 0},
                fields=["restaurant_table", "creation"],
            )
            inv_map = {inv.restaurant_table: inv.creation for inv in active_invoices}

            for t_name, t_data in occupied_tables.items():
                occupied_start_raw = inv_map.get(t_name) or t_data.get("latest_invoice_time") or current_now
                occupied_start_dt = parse_to_datetime(occupied_start_raw, current_now)
                expected_finish = occupied_start_dt + timedelta(minutes=expected_duration)

                if expected_finish > res_hold_start:
                    occupied_conflicts.add(t_name)

        # 4. Check conflicting reservations
        table_names = [t.name for t in tables]
        placeholders = ", ".join(["%s"] * len(table_names))
        res_query = f"""
            SELECT name, reserved_table, reserved_at, branch
            FROM `tabURY Table Reservation`
            WHERE reserved_table IN ({placeholders})
              AND status IN ('Confirmed', 'Active', 'Requested', 'Seated')
        """
        res_params = list(table_names)
        if exclude_reservation:
            res_query += " AND name != %s"
            res_params.append(exclude_reservation)

        existing_reservations = frappe.db.sql(res_query, tuple(res_params), as_dict=True)

        reservation_conflicts = set()
        branch_settings_cache = {branch: settings}

        for ex in existing_reservations:
            ex_table = ex.get("reserved_table")
            ex_start = parse_to_datetime(ex.reserved_at, current_now)
            ex_branch = ex.get("branch") or branch

            if ex_branch not in branch_settings_cache:
                branch_settings_cache[ex_branch] = get_branch_reservation_settings(ex_branch)
            ex_settings = branch_settings_cache[ex_branch]
            ex_hold_start = ex_start - timedelta(minutes=ex_settings["buffer_time"])
            ex_end = ex_start + timedelta(minutes=reservation_stay_minutes(ex_settings))

            # Overlap check: [ex_start - buffer, ex_end) overlaps [res_start - buffer, res_end)
            if ex_hold_start < res_end and res_hold_start < ex_end:
                reservation_conflicts.add(ex_table)

        # 5. Filter available tables
        pax = cint(no_of_pax or 0)
        available_tables = [
            t for t in tables
            if t.name not in occupied_conflicts and t.name not in reservation_conflicts
            and (not pax or _party_fits(t, pax))
        ]

        return available_tables
    except Exception as e:
        frappe.log_error(f"Error in get_available_tables_for_reservation: {str(e)}", "Reservation Availability Error")
        return []


def _party_has_another_open_check(doc, tables):
    """An earlier check on these tables, or another part of this split bill, is still open."""
    opened = get_datetime(doc.get("creation")) if doc.get("creation") else None
    group = doc.get("custom_split_group")
    for check in open_checks_on(tables):
        if check.name == doc.name:
            continue
        same_bill = bool(group and check.custom_split_group == group) or (
            check.custom_split_from == doc.name or check.name == doc.get("custom_split_from"))
        if same_bill or (opened and check.creation and get_datetime(check.creation) < opened):
            return True
    return False


def complete_seated_reservation_on_submit(doc, method=None):
    """POS Invoice on_submit: closing the party's last check completes its Seated reservation.

    Only a Dine-In sale (never a credit note) on the reservation's own branch,
    on its table or a merged partner, and only once no earlier check on those
    tables and no other part of this split bill is still open. A party linked
    to another bill, or whose hold had ended before this bill was opened, is
    someone else's. The save runs under the closer's own authority (native
    DocPerm and the Completed gate any completion meets), so the Version names
    the closer, and a closer without that authority leaves the party Seated.
    It never blocks the payment: a refusal or failure is logged, not raised.
    """
    if cint(doc.get("is_return")) or (doc.get("order_type") or "Dine In") != "Dine In":
        return
    branch = doc.get("branch")
    tables = [doc.get("restaurant_table")] + (doc.get("custom_merged_tables") or "").split(",")
    tables = [table.strip() for table in tables if table and table.strip()]
    if not tables or not reservations_enabled(branch):
        return
    try:
        seated = frappe.db.get_all(
            "URY Table Reservation",
            filters={"reserved_table": ["in", tables], "branch": branch, "status": "Seated"},
            fields=["name", "reserved_at", "pos_invoice"],
        )
        if not seated or _party_has_another_open_check(doc, tables):
            return
        stay = timedelta(minutes=reservation_stay_minutes(get_branch_reservation_settings(branch)))
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Reservation completion on bill close")
        return
    opened = get_datetime(doc.get("creation")) if doc.get("creation") else now_datetime()
    for res in seated:
        if res.pos_invoice and res.pos_invoice != doc.name:
            continue  # another bill's party
        if not res.pos_invoice and opened >= get_datetime(res.reserved_at) + stay:
            continue  # its hold had ended before this bill was opened
        try:
            reservation = frappe.get_doc("URY Table Reservation", res.name)
            reservation.status = "Completed"
            reservation.pos_invoice = doc.name
            reservation.save()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Reservation completion on bill close")
