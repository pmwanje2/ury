"""Session-scoped multi-outlet switching (G43) and Business Date / Shift X-Z UI wiring (G44).

Design invariants:
- NEVER mutates `User Permission` (zero DB writes to `tabUser Permission`).
- Active outlet is stored in `frappe.cache` keyed strictly by `(user, sid)` with an 8-hour TTL
  so concurrent sessions for the same user remain isolated.
- Every outlet lookup, switch, menu load, and shift report re-verifies fail-closed authorization
  against current `User Permission` (Company, Branch, POS Profile), `POS Profile.applicable_for_users`,
  and `Branch.user` membership.
- Open-till cashier guard (`[POS-OUTLET-OPEN-TILL-ACTIVE]`): blocks opening a second till or
  settling bills on Outlet B while the user owns an open shift on Outlet A.
- G44 Business Date (`custom_business_date`) and Shift X/Z Report (`Day Close Summary` /
  `POS Shift Z Report`) degrade gracefully when those site packages are not installed.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint

from ury.brand import coded_message

ACTIVE_OUTLET_TTL_SECONDS = 8 * 60 * 60
OUTLET_HEADER_NAME = "X-URY-POS-Profile"


def _get_user_permissions_safe(user: str) -> dict[str, list[dict[str, Any]]]:
    try:
        from frappe.core.doctype.user_permission.user_permission import get_user_permissions

        perms = get_user_permissions(user)
        return perms if isinstance(perms, dict) else {}
    except Exception:
        return {}


def _session_sid() -> str:
    session = getattr(frappe, "session", None)
    sid = getattr(session, "sid", None)
    if isinstance(sid, str) and sid.strip() and sid != "Guest":
        return sid.strip()
    request = getattr(frappe, "request", None)
    if request is not None:
        try:
            cookies = getattr(request, "cookies", None) or {}
            if hasattr(cookies, "get"):
                cookie_sid = cookies.get("sid")
                if isinstance(cookie_sid, str) and cookie_sid.strip() and cookie_sid != "Guest":
                    return cookie_sid.strip()
        except RuntimeError:
            pass
    return "default"


def _active_outlet_cache_key(user: str | None = None, sid: str | None = None) -> str:
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None) or ""
    resolved_sid = sid or _session_sid()
    return f"ury:active_outlet:{resolved_user}:{resolved_sid}"


def _header_pos_profile() -> str | None:
    request = getattr(frappe, "request", None)
    if request is None:
        return None
    try:
        headers = getattr(request, "headers", None)
        if headers is None or not hasattr(headers, "get"):
            return None
        raw = headers.get(OUTLET_HEADER_NAME) or headers.get(OUTLET_HEADER_NAME.lower())
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    except RuntimeError:
        return None
    return None


def _paired_tablet_profile() -> str | None:
    try:
        from ury.ury_pos import floor_pin

        device = floor_pin._device()
        if device and device[0] and isinstance(getattr(device[0], "name", None), str):
            return device[0].name
    except Exception:
        return None
    return None


def _get_cached_outlet(user: str | None = None) -> str | None:
    cache = getattr(frappe, "cache", None)
    if cache is None or not hasattr(cache, "get_value"):
        return None
    val = cache.get_value(_active_outlet_cache_key(user))
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="ignore")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def _clear_cached_outlet(user: str | None = None) -> None:
    cache = getattr(frappe, "cache", None)
    if cache is not None and hasattr(cache, "delete_value"):
        try:
            cache.delete_value(_active_outlet_cache_key(user))
        except Exception:
            pass


def _is_supervisor(user: str) -> bool:
    if not user:
        return False
    if user == "Administrator":
        return True
    try:
        roles = frappe.get_roles(user) or []
        return isinstance(roles, (list, tuple, set)) and "System Manager" in roles
    except Exception:
        return False


def _user_branch_memberships(user: str) -> set[str]:
    rows = frappe.db.sql(
        """
        SELECT DISTINCT b.name AS branch
        FROM `tabURY User` AS a
        INNER JOIN `tabBranch` AS b ON a.parent = b.name
        WHERE a.user = %s AND a.parenttype = 'Branch'
        """,
        (user,),
        as_dict=True,
    )
    if not isinstance(rows, (list, tuple)):
        return set()
    return {
        str(row.get("branch")).strip()
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("branch"), str) and row.get("branch").strip()
    }


def _opening_entry_has_business_date() -> bool:
    try:
        meta = frappe.get_meta("POS Opening Entry")
        return bool(meta and meta.has_field("custom_business_date"))
    except Exception:
        return False


def _has_perm_safe(doctype: str, ptype: str, doc=None, user: str | None = None) -> bool:
    try:
        if user:
            try:
                return bool(frappe.has_permission(doctype, ptype, doc=doc, user=user))
            except TypeError:
                pass
        if doc is not None:
            return bool(frappe.has_permission(doctype, ptype, doc))
        return bool(frappe.has_permission(doctype, ptype))
    except Exception:
        return False


def _user_has_report_role(report_name: str, user: str | None = None) -> bool:
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    if resolved_user == "Administrator":
        return True
    try:
        user_roles = set(frappe.get_roles(resolved_user) or [])
    except Exception:
        return False

    allowed_roles: list[str] = []
    try:
        from frappe.core.doctype.report.report import get_custom_allowed_roles

        custom_roles = get_custom_allowed_roles("report", report_name)
        if isinstance(custom_roles, (list, tuple, set)) and custom_roles:
            allowed_roles = [str(r) for r in custom_roles if r]
    except Exception:
        allowed_roles = []

    if not allowed_roles:
        try:
            custom_role_name = frappe.db.get_value("Custom Role", {"report": report_name}, "name")
            if custom_role_name:
                rows = frappe.get_all(
                    "Has Role",
                    filters={"parent": custom_role_name, "parenttype": "Custom Role"},
                    fields=["role"],
                    limit_page_length=0,
                )
                if isinstance(rows, (list, tuple)):
                    allowed_roles = [r.get("role") for r in rows if isinstance(r, dict) and r.get("role")]
        except Exception:
            pass

    if not allowed_roles:
        try:
            rows = frappe.get_all(
                "Has Role",
                filters={"parent": report_name},
                fields=["role"],
                limit_page_length=0,
            )
            if isinstance(rows, (list, tuple)):
                allowed_roles = [r.get("role") for r in rows if isinstance(r, dict) and r.get("role")]
        except Exception:
            pass

    if not allowed_roles:
        try:
            report_doc = frappe.get_doc("Report", report_name)
            if cint(getattr(report_doc, "disabled", 0)):
                return False
            if callable(getattr(report_doc, "is_permitted", None)):
                return bool(report_doc.is_permitted())
        except Exception:
            return True
        return True

    return bool(user_roles & set(allowed_roles))


def _shift_report_installed(user: str | None = None) -> bool:
    try:
        if not frappe.db.exists("Report", "Day Close Summary"):
            return False
        if cint(frappe.db.get_value("Report", "Day Close Summary", "disabled")):
            return False
        if not _has_perm_safe("Report", "read", doc="Day Close Summary", user=user):
            return False
        ref_doctype = frappe.db.get_value("Report", "Day Close Summary", "ref_doctype") or "POS Invoice"
        if not _has_perm_safe(str(ref_doctype), "report", user=user):
            return False
        if not _user_has_report_role("Day Close Summary", user=user):
            return False
        return True
    except Exception:
        return False


def _shift_z_print_format_installed() -> bool:
    try:
        return bool(frappe.db.exists("Print Format", "POS Shift Z Report"))
    except Exception:
        return False


def get_open_shifts_for_user(user: str | None = None) -> list[dict[str, Any]]:
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    if not resolved_user or resolved_user == "Guest":
        return []
    fields = ["name", "pos_profile", "company", "branch", "user", "period_start_date", "posting_date", "status"]
    if _opening_entry_has_business_date():
        fields.append("custom_business_date")
    rows = frappe.get_all(
        "POS Opening Entry",
        filters={"user": resolved_user, "docstatus": 1, "status": "Open"},
        fields=fields,
        order_by="period_start_date desc",
        limit_page_length=50,
    )
    return list(rows) if isinstance(rows, (list, tuple)) else []


def get_profile_active_shift(pos_profile: str, user: str | None = None) -> dict[str, Any] | None:
    if not pos_profile:
        return None
    fields = ["name", "pos_profile", "company", "branch", "user", "period_start_date", "posting_date", "status"]
    if _opening_entry_has_business_date():
        fields.append("custom_business_date")
    rows = frappe.get_all(
        "POS Opening Entry",
        filters={"pos_profile": pos_profile, "docstatus": 1, "status": "Open"},
        fields=fields,
        order_by="period_start_date desc",
        limit_page_length=20,
    )
    if not isinstance(rows, (list, tuple)) or not rows:
        return None
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    own = next((r for r in rows if r.get("user") == resolved_user), None)
    return own or rows[0]


def get_authorized_outlets(user: str | None = None) -> list[dict[str, Any]]:
    """Return every enabled POS Profile the user is authorized to use, fail-closed."""
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    if not resolved_user or resolved_user == "Guest":
        return []

    is_super = _is_supervisor(resolved_user)
    if not is_super and cint(frappe.db.get_value("User", resolved_user, "enabled")) != 1:
        return []

    profiles = frappe.get_all(
        "POS Profile",
        filters={"disabled": 0},
        fields=["name", "branch", "company", "restaurant", "currency"],
        limit_page_length=0,
    )
    if not isinstance(profiles, (list, tuple)) or not profiles:
        return []

    profile_names = [p.get("name") for p in profiles if isinstance(p.get("name"), str)]
    if not profile_names:
        return []

    user_rows = frappe.get_all(
        "POS Profile User",
        filters={"parent": ["in", profile_names], "parenttype": "POS Profile"},
        fields=["parent", "user"],
        limit_page_length=0,
    )
    profile_users: dict[str, set[str]] = {}
    if isinstance(user_rows, (list, tuple)):
        for row in user_rows:
            parent = row.get("parent")
            uname = row.get("user")
            if isinstance(parent, str) and isinstance(uname, str) and uname:
                profile_users.setdefault(parent, set()).add(uname)

    perms = _get_user_permissions_safe(resolved_user)
    company_perms = {p.get("doc") for p in (perms.get("Company") or []) if isinstance(p.get("doc"), str) and p.get("doc")}
    branch_perms = {p.get("doc") for p in (perms.get("Branch") or []) if isinstance(p.get("doc"), str) and p.get("doc")}
    profile_perms = {p.get("doc") for p in (perms.get("POS Profile") or []) if isinstance(p.get("doc"), str) and p.get("doc")}
    assigned_branches = _user_branch_memberships(resolved_user)

    open_shifts = get_open_shifts_for_user(resolved_user)
    open_by_profile = {row.get("pos_profile"): row for row in open_shifts if row.get("pos_profile")}
    cached_active = _get_cached_outlet(resolved_user)

    outlets: list[dict[str, Any]] = []
    for p in profiles:
        pname = p.get("name")
        branch = p.get("branch")
        company = p.get("company")
        if not isinstance(pname, str) or not pname:
            continue

        # Strict User Permission enforcement (fail-closed when restricted)
        if company_perms and company not in company_perms:
            continue
        if branch_perms and (not branch or branch not in branch_perms):
            continue
        if profile_perms and pname not in profile_perms:
            continue

        allowed_users = profile_users.get(pname, set())
        if not is_super:
            if allowed_users and resolved_user not in allowed_users:
                continue
            in_branch = bool(branch and branch in assigned_branches)
            in_profile = resolved_user in allowed_users
            if not (in_branch or in_profile):
                continue

        currency = p.get("currency")
        if not currency and company:
            currency = frappe.db.get_value("Company", company, "default_currency")

        shift = open_by_profile.get(pname)
        outlets.append(
            {
                "pos_profile": pname,
                "name": pname,
                "branch": branch or "",
                "company": company or "",
                "restaurant": p.get("restaurant") or "",
                "currency": currency or "",
                "has_open_shift": bool(shift),
                "open_entry": shift.get("name") if shift else None,
                "open_business_date": (
                    str(shift.get("custom_business_date"))
                    if shift and shift.get("custom_business_date")
                    else None
                ),
                "is_active": pname == cached_active,
            }
        )

    outlets.sort(key=lambda item: (item["branch"].lower(), item["pos_profile"].lower()))
    return outlets


@frappe.whitelist(methods=["GET"])
def get_available_outlets() -> list[dict[str, Any]]:
    """Return all enabled POS Profiles the session user is authorized to operate."""
    return get_authorized_outlets(frappe.session.user)


def assert_outlet_access(pos_profile: str, user: str | None = None) -> dict[str, Any]:
    """Verify `user` is authorized for `pos_profile`, else raise 403 PermissionError."""
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    if not isinstance(pos_profile, str) or not pos_profile.strip():
        frappe.throw(
            coded_message("OUTLET-NOT-FOUND", _("Select an outlet before continuing.")),
            frappe.ValidationError,
        )
    target = pos_profile.strip()
    outlets = get_authorized_outlets(resolved_user)
    matched = next((o for o in outlets if o["pos_profile"] == target), None)
    if not matched:
        frappe.throw(
            coded_message("OUTLET-FORBIDDEN", _("You are not permitted to access this outlet.")),
            frappe.PermissionError,
        )
    return matched


def resolve_active_pos_profile(
    pos_profile: str | None = None,
    *,
    user: str | None = None,
    allow_ambiguous: bool = False,
) -> dict[str, Any] | None:
    """Deterministic active outlet resolution (fail-closed at every step).

    Priority:
    1. Explicit `pos_profile` parameter or `X-URY-POS-Profile` request header (re-verified; 403 if denied).
    2. Device-paired floor tablet profile (re-verified).
    3. Session-cached active outlet `(user, sid)` (re-verified; evicted if permissions were revoked).
    4. User's open `POS Opening Entry` (re-verified).
    5. Single authorized outlet OR single default `Branch` `User Permission`.
    """
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    outlets = get_authorized_outlets(resolved_user)
    by_name = {o["pos_profile"]: o for o in outlets}

    # 1. Explicit param or X-URY-POS-Profile header
    explicit = (pos_profile.strip() if isinstance(pos_profile, str) and pos_profile.strip() else None) or _header_pos_profile()
    if explicit:
        if explicit not in by_name:
            frappe.throw(
                coded_message("OUTLET-FORBIDDEN", _("You are not permitted to access this outlet.")),
                frappe.PermissionError,
            )
        return by_name[explicit]

    if not outlets:
        return None

    # 2. Paired floor tablet profile
    paired = _paired_tablet_profile()
    if paired and paired in by_name:
        return by_name[paired]

    # 3. Session-cached active outlet (fail-closed re-verification against current permissions)
    cached = _get_cached_outlet(resolved_user)
    if cached:
        if cached in by_name:
            return by_name[cached]
        _clear_cached_outlet(resolved_user)

    # 4. User's open POS Opening Entry
    open_outlets = [o for o in outlets if o.get("has_open_shift")]
    if len(open_outlets) == 1:
        return open_outlets[0]

    # 5a. Single authorized outlet
    if len(outlets) == 1:
        return outlets[0]

    # 5b. Single default Branch User Permission resolving to one authorized outlet
    perms = _get_user_permissions_safe(resolved_user)
    authorized_branches = {o["branch"] for o in outlets if o.get("branch")}
    default_branches = {
        p.get("doc")
        for p in (perms.get("Branch") or [])
        if p.get("is_default") and p.get("doc") in authorized_branches
    }
    if len(default_branches) == 1:
        default_branch = next(iter(default_branches))
        branch_outlets = [o for o in outlets if o["branch"] == default_branch]
        if len(branch_outlets) == 1:
            return branch_outlets[0]

    if allow_ambiguous:
        return None

    frappe.throw(
        coded_message("BRANCH-AMBIGUOUS", _("Set a default Branch User Permission for an assigned branch.")),
        frappe.ValidationError,
    )


def resolve_session_selected_branch(pos_profile: str | None = None, user: str | None = None) -> str | None:
    """Return the branch of an explicitly selected / header / paired / session-cached / active-open-shift outlet.

    Returns None when no explicit, session, or single open-shift outlet selection exists so legacy
    `getBranch()` ambiguity checks remain untouched when a multi-branch user has not chosen an outlet.
    """
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    if not resolved_user or resolved_user == "Guest":
        return None

    explicit = (pos_profile.strip() if isinstance(pos_profile, str) and pos_profile.strip() else None) or _header_pos_profile()
    paired = _paired_tablet_profile() if not explicit else None
    cached = _get_cached_outlet(resolved_user) if not explicit and not paired else None

    candidate = explicit or paired or cached
    outlets = get_authorized_outlets(resolved_user)
    by_name = {o["pos_profile"]: o for o in outlets}
    if candidate:
        if candidate in by_name:
            return by_name[candidate]["branch"] or None
        if explicit:
            frappe.throw(
                coded_message("OUTLET-FORBIDDEN", _("You are not permitted to access this outlet.")),
                frappe.PermissionError,
            )
        if cached:
            _clear_cached_outlet(resolved_user)

    # Single open shift among authorized outlets resolves branch deterministically
    open_outlets = [o for o in outlets if o.get("has_open_shift")]
    if len(open_outlets) == 1:
        return open_outlets[0]["branch"] or None

    return None


def find_conflicting_open_till(pos_profile: str, user: str | None = None) -> dict[str, Any] | None:
    """Return an open shift owned by `user` on a DIFFERENT POS Profile, or None."""
    resolved_user = user or getattr(getattr(frappe, "session", None), "user", None)
    if not resolved_user or resolved_user == "Guest" or not pos_profile:
        return None
    for shift in get_open_shifts_for_user(resolved_user):
        other_profile = shift.get("pos_profile")
        if other_profile and other_profile != pos_profile:
            return shift
    return None


def assert_no_conflicting_open_till(pos_profile: str, user: str | None = None) -> None:
    """Refuse cashier settlement or opening a new till on Outlet B while Outlet A's shift is open."""
    other = find_conflicting_open_till(pos_profile, user=user)
    if other:
        frappe.throw(
            coded_message(
                "OUTLET-OPEN-TILL-ACTIVE",
                _(
                    "Close your open shift ({0}) on {1} before opening a till or settling bills on {2}."
                ).format(other.get("name"), other.get("pos_profile"), pos_profile),
            ),
            frappe.ValidationError,
        )


def get_business_date_and_report_meta(pos_profile: str, user: str | None = None) -> dict[str, Any]:
    """Return G44 active business date and Shift X/Z report availability (graceful when absent)."""
    active_shift = get_profile_active_shift(pos_profile, user=user)
    business_date = None
    if active_shift and active_shift.get("custom_business_date"):
        business_date = str(active_shift.get("custom_business_date"))
    return {
        "active_opening_entry": active_shift.get("name") if active_shift else None,
        "active_business_date": business_date,
        "shift_report_available": _shift_report_installed(user=user),
        "shift_z_print_format_available": _shift_z_print_format_installed(),
    }


@frappe.whitelist(methods=["POST"])
def set_active_outlet(pos_profile: str) -> dict[str, Any]:
    """Switch active POS Profile in session-scoped cache (zero User Permission mutation)."""
    outlet = assert_outlet_access(pos_profile, user=frappe.session.user)
    frappe.cache.set_value(
        _active_outlet_cache_key(frappe.session.user),
        outlet["pos_profile"],
        expires_in_sec=ACTIVE_OUTLET_TTL_SECONDS,
    )

    from ury.ury_pos.api import getPosProfile

    profile_payload = getPosProfile(pos_profile=outlet["pos_profile"])
    other_shift = find_conflicting_open_till(outlet["pos_profile"], user=frappe.session.user)
    return {
        **profile_payload,
        "active_outlet": outlet["pos_profile"],
        "available_outlets": get_authorized_outlets(frappe.session.user),
        "open_till_elsewhere": (
            {
                "pos_profile": other_shift.get("pos_profile"),
                "opening_entry": other_shift.get("name"),
                "branch": other_shift.get("branch"),
            }
            if other_shift
            else None
        ),
    }


@frappe.whitelist(methods=["GET"])
def get_shift_x_report(pos_profile: str | None = None, pos_opening_entry: str | None = None) -> dict[str, Any]:
    """Read-only G44 Shift X/Z summary backed by `Day Close Summary` when installed."""
    resolved = resolve_active_pos_profile(pos_profile=pos_profile, allow_ambiguous=False)
    if not resolved:
        frappe.throw(
            coded_message("OUTLET-NOT-FOUND", _("No authorized outlet is active for this session.")),
            frappe.PermissionError,
        )

    profile_name = resolved["pos_profile"]
    company = resolved["company"]
    currency = resolved["currency"]
    shift = None
    if pos_opening_entry:
        fields = ["name", "pos_profile", "company", "branch", "user", "period_start_date", "posting_date", "status"]
        if _opening_entry_has_business_date():
            fields.append("custom_business_date")
        shift = frappe.db.get_value("POS Opening Entry", pos_opening_entry, fields, as_dict=True)
        if not shift or shift.get("pos_profile") != profile_name:
            frappe.throw(
                coded_message("OUTLET-FORBIDDEN", _("Shift does not belong to the active outlet.")),
                frappe.PermissionError,
            )
    else:
        shift = get_profile_active_shift(profile_name, user=frappe.session.user)

    business_date = (
        str(shift.get("custom_business_date"))
        if shift and shift.get("custom_business_date")
        else None
    )

    if not _shift_report_installed(user=frappe.session.user):
        return {
            "available": False,
            "pos_profile": profile_name,
            "business_date": business_date,
        }

    if not shift:
        return {
            "available": True,
            "has_open_shift": False,
            "report_type": "X",
            "pos_profile": profile_name,
            "company": company,
            "currency": currency,
            "pos_opening_entry": None,
            "business_date": None,
            "columns": [],
            "rows": [],
            "report_summary": [],
            "message": "",
            "has_z_print_format": _shift_z_print_format_installed(),
        }

    from frappe.desk.query_report import run as run_query_report

    try:
        report = run_query_report(
            report_name="Day Close Summary",
            filters={"company": company, "pos_opening_entry": shift.get("name")},
        ) or {}
    except Exception as exc:
        if exc.__class__.__name__ == "ServerScriptNotEnabled":
            return {
                "available": False,
                "pos_profile": profile_name,
                "business_date": business_date,
            }
        raise

    return {
        "available": True,
        "has_open_shift": True,
        "report_type": "X" if shift.get("status") == "Open" else "Z",
        "pos_profile": profile_name,
        "company": company,
        "currency": currency,
        "pos_opening_entry": shift.get("name"),
        "business_date": business_date,
        "posting_date": str(shift.get("posting_date") or ""),
        "columns": report.get("columns") or [],
        "rows": report.get("result") or [],
        "report_summary": report.get("report_summary") or [],
        "message": report.get("message") or "",
        "has_z_print_format": _shift_z_print_format_installed(),
    }
