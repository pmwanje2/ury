"""G36: POS Employee Clock-In / Clock-Out over native HR `Employee Checkin`.

- Zero custom attendance tables: creates standard `Employee Checkin` records (`IN` / `OUT`)
  with `device_id = "POS:<pos_profile>"` so Shift Auto-Attendance & Payroll work natively.
- Gracefully hidden / no-op when optional `hrms` app or `Employee Checkin` DocType is absent.
- Enforces open-work guard on clock-out (`[POS-TIMECARD-OPEN-WORK]`) when open draft checks
  or an open till shift belong to the operator.
- Enforces opt-in `POS Profile.custom_require_clock_in_to_order` (`[POS-TIMECARD-CLOCK-IN-REQUIRED]`)
  and backs `POS Privilege Rule.require_clocked_in` in `ury_pos/override.py`.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from ury.brand import coded_message
from ury.ury_pos import outlet_context

POS_SELF_CHECKIN_ROLES = frozenset({
    "Employee",
    "URY Waiter",
    "URY Captain",
    "URY Cashier",
    "URY Manager",
    "URY Admin",
    "System Manager",
})


def is_hrms_timeclock_available() -> bool:
    """True only when the optional HR app is installed and `Employee Checkin` DocType exists."""
    try:
        apps = frappe.get_installed_apps() or []
    except Exception:
        apps = []
    if not isinstance(apps, (list, tuple, set)) or "hrms" not in apps:
        return False
    try:
        return bool(frappe.db.exists("DocType", "Employee Checkin"))
    except Exception:
        return False


def _checkin_table_available() -> bool:
    if is_hrms_timeclock_available():
        return True
    try:
        return bool(frappe.db.table_exists("Employee Checkin"))
    except Exception:
        return False


def _resolve_active_employee(user: str, company: str | None = None) -> dict[str, Any] | None:
    if not user or user in ("Administrator", "Guest"):
        return None
    filters: dict[str, Any] = {"user_id": user, "status": "Active"}
    if company:
        filters["company"] = company
    row = frappe.db.get_value(
        "Employee",
        filters,
        ["name", "employee_name", "user_id", "company", "status"],
        as_dict=True,
    )
    if isinstance(row, dict) and row.get("name"):
        return row
    return None


def _latest_checkin(employee: str) -> dict[str, Any] | None:
    rows = frappe.get_all(
        "Employee Checkin",
        filters={"employee": employee},
        fields=["name", "log_type", "time", "shift", "device_id"],
        order_by="time desc, creation desc",
        limit=1,
    )
    if isinstance(rows, (list, tuple)) and rows:
        return rows[0]
    latest_type = frappe.db.get_value(
        "Employee Checkin",
        {"employee": employee},
        "log_type",
        order_by="time desc",
    )
    if isinstance(latest_type, str) and latest_type:
        return {"log_type": latest_type, "time": None, "shift": None, "device_id": None}
    return None


def is_user_clocked_in(
    user: str,
    company: str | None = None,
    *,
    allow_if_no_history_without_hrms: bool = True,
) -> bool:
    """True if user is clocked in, or if attendance enforcement is inactive for this user/site.

    - If `Employee Checkin` is absent: returns True (graceful degradation).
    - If user has no active `Employee` record in `company`: returns True.
    - If latest `Employee Checkin` exists: returns `log_type == "IN"`.
    - If employee has zero check-ins: returns False when `hrms` is installed, else
      `allow_if_no_history_without_hrms` (preserving legacy site-less unit tests).
    """
    if not _checkin_table_available():
        return True
    emp = _resolve_active_employee(user, company=company)
    if not emp:
        return True
    latest = _latest_checkin(emp["name"])
    if latest and latest.get("log_type"):
        return str(latest.get("log_type")).upper() == "IN"
    if is_hrms_timeclock_available():
        return False
    return bool(allow_if_no_history_without_hrms)


def _open_work_for_user(user: str, profile) -> tuple[list[str], list[str]]:
    branch = profile.get("branch") if profile else None
    company = profile.get("company") if profile else None

    inv_filters: dict[str, Any] = {"docstatus": 0, "waiter": user}
    if branch:
        inv_filters["branch"] = branch
    if company:
        inv_filters["company"] = company
    open_checks = frappe.get_all("POS Invoice", filters=inv_filters, pluck="name", limit=20)
    open_check_names = list(open_checks) if isinstance(open_checks, (list, tuple)) else []

    shift_filters: dict[str, Any] = {"docstatus": 1, "status": "Open", "user": user}
    if company:
        shift_filters["company"] = company
    open_shifts = frappe.get_all("POS Opening Entry", filters=shift_filters, pluck="name", limit=10)
    open_shift_names = list(open_shifts) if isinstance(open_shifts, (list, tuple)) else []

    return open_check_names, open_shift_names


def _block_open_work_enabled(profile) -> bool:
    if not profile:
        return True
    raw = profile.get("custom_block_clock_out_with_open_work")
    if raw is None or raw == "":
        return True
    return cint(raw) == 1


def _build_status_payload(user: str, profile) -> dict[str, Any]:
    company = profile.get("company") if profile else None
    emp = _resolve_active_employee(user, company=company)
    block_open_work = _block_open_work_enabled(profile)
    require_clock_in = bool(profile and cint(profile.get("custom_require_clock_in_to_order")) == 1)
    profile_name = profile.get("name") if profile else None

    if not emp:
        return {
            "available": True,
            "has_employee": False,
            "employee": None,
            "employee_name": None,
            "is_clocked_in": False,
            "last_log_type": None,
            "last_punch_time": None,
            "clocked_in_since": None,
            "elapsed_minutes": 0,
            "shift": None,
            "open_checks_count": 0,
            "open_checks": [],
            "has_open_shift": False,
            "open_shift": None,
            "block_clock_out_with_open_work": block_open_work,
            "require_clock_in_to_order": require_clock_in,
            "pos_profile": profile_name,
        }

    latest = _latest_checkin(emp["name"])
    last_type = str(latest.get("log_type") or "").upper() if latest else None
    is_in = last_type == "IN"
    last_time = latest.get("time") if latest else None
    elapsed_minutes = 0
    if is_in and last_time:
        try:
            delta = now_datetime() - get_datetime(last_time)
            elapsed_minutes = max(0, int(delta.total_seconds() // 60))
        except Exception:
            elapsed_minutes = 0

    open_checks, open_shifts = _open_work_for_user(user, profile)
    return {
        "available": True,
        "has_employee": True,
        "employee": emp["name"],
        "employee_name": emp.get("employee_name") or emp["name"],
        "is_clocked_in": is_in,
        "last_log_type": last_type,
        "last_punch_time": str(last_time) if last_time else None,
        "clocked_in_since": str(last_time) if is_in and last_time else None,
        "elapsed_minutes": elapsed_minutes,
        "shift": latest.get("shift") if latest else None,
        "open_checks_count": len(open_checks),
        "open_checks": open_checks[:5],
        "has_open_shift": bool(open_shifts),
        "open_shift": open_shifts[0] if open_shifts else None,
        "block_clock_out_with_open_work": block_open_work,
        "require_clock_in_to_order": require_clock_in,
        "pos_profile": profile_name,
    }


def enforce_clock_in_for_profile(profile, user: str | None = None) -> None:
    """Enforce opt-in `POS Profile.custom_require_clock_in_to_order` cleanly."""
    if not profile or cint(profile.get("custom_require_clock_in_to_order")) != 1:
        return
    if not is_hrms_timeclock_available():
        return
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    if not resolved_user or resolved_user in ("Administrator", "Guest"):
        return
    emp = _resolve_active_employee(resolved_user, company=profile.get("company"))
    if not emp:
        return
    if not is_user_clocked_in(resolved_user, company=profile.get("company")):
        frappe.throw(
            coded_message(
                "TIMECARD-CLOCK-IN-REQUIRED",
                _("Clock in before taking orders, settling bills, or opening a shift."),
            ),
            frappe.ValidationError,
        )


@frappe.whitelist(methods=["GET"])
def get_my_timeclock_status(pos_profile: str | None = None) -> dict[str, Any]:
    """Return current session user's timeclock status for the active outlet."""
    user = getattr(getattr(frappe, "session", None), "user", None)
    if not user or user == "Guest":
        frappe.throw(_("Sign in first."), frappe.PermissionError)

    profile = None
    if pos_profile:
        outlet_context.assert_outlet_access(pos_profile, user=user)
        profile = frappe.get_doc("POS Profile", pos_profile)
    else:
        resolved = outlet_context.resolve_active_pos_profile(user=user, allow_ambiguous=True)
        if resolved:
            profile = frappe.get_doc("POS Profile", resolved["pos_profile"])

    if not is_hrms_timeclock_available():
        return {"available": False}

    return _build_status_payload(user, profile)


@frappe.whitelist(methods=["POST"])
def punch_timeclock(action: str, pos_profile: str) -> dict[str, Any]:
    """Record a native `Employee Checkin` (`IN` or `OUT`) from the POS header."""
    user = getattr(getattr(frappe, "session", None), "user", None)
    if not user or user == "Guest":
        frappe.throw(_("Sign in first."), frappe.PermissionError)

    normalized_action = str(action or "").strip().upper()
    if normalized_action not in ("IN", "OUT"):
        frappe.throw(
            coded_message("TIMECARD-INVALID-ACTION", _("Choose Clock In or Clock Out.")),
            frappe.ValidationError,
        )

    if not is_hrms_timeclock_available():
        frappe.throw(
            coded_message("TIMECARD-UNAVAILABLE", _("Employee timeclock is not enabled on this site.")),
            frappe.ValidationError,
        )

    outlet_context.assert_outlet_access(pos_profile, user=user)
    profile = frappe.get_doc("POS Profile", pos_profile)

    emp = _resolve_active_employee(user, company=profile.get("company"))
    if not emp:
        frappe.throw(
            coded_message(
                "TIMECARD-NO-EMPLOYEE",
                _("No active employee profile is linked to your account for this company."),
            ),
            frappe.ValidationError,
        )

    latest = _latest_checkin(emp["name"])
    is_currently_in = bool(latest and str(latest.get("log_type") or "").upper() == "IN")

    if normalized_action == "IN" and is_currently_in:
        frappe.throw(
            coded_message("TIMECARD-ALREADY-IN", _("You are already clocked in.")),
            frappe.ValidationError,
        )

    if normalized_action == "OUT" and not is_currently_in:
        frappe.throw(
            coded_message("TIMECARD-NOT-CLOCKED-IN", _("You must clock in before clocking out.")),
            frappe.ValidationError,
        )

    if normalized_action == "OUT" and _block_open_work_enabled(profile):
        open_checks, open_shifts = _open_work_for_user(user, profile)
        if open_checks or open_shifts:
            parts = []
            if open_checks:
                parts.append(_("open checks: {0}").format(", ".join(open_checks[:5])))
            if open_shifts:
                parts.append(_("open shift: {0}").format(", ".join(open_shifts[:3])))
            frappe.throw(
                coded_message(
                    "TIMECARD-OPEN-WORK",
                    _("Transfer or close your open work before clocking out ({0}).").format("; ".join(parts)),
                ),
                frappe.ValidationError,
            )

    punch_time = now_datetime()
    checkin = frappe.get_doc(
        {
            "doctype": "Employee Checkin",
            "employee": emp["name"],
            "log_type": normalized_action,
            "time": punch_time,
            "device_id": f"POS:{profile.name}",
        }
    )

    try:
        checkin.insert(ignore_permissions=False)
    except frappe.PermissionError:
        user_roles = set(frappe.get_roles(user) or [])
        if emp.get("user_id") == user and bool(POS_SELF_CHECKIN_ROLES & user_roles):
            checkin.insert(ignore_permissions=True)
        else:
            raise

    if callable(getattr(checkin, "fetch_shift", None)):
        try:
            checkin.fetch_shift()
        except Exception:
            pass

    status = _build_status_payload(user, profile)
    status.update(
        {
            "checkin": checkin.name,
            "log_type": normalized_action,
            "time": str(checkin.get("time") or punch_time),
            "device_id": f"POS:{profile.name}",
        }
    )
    return status
