"""POS manager PIN authorisation core (Oracle Simphony / SAP POS parity + Amendment 1 + R2 hardening).

Model (Simphony): each role has explicit `may_perform` (act alone) and
`may_authorise` (approve for another operator with their personal PIN) rules in
`POS Privilege Rule`, plus `allow_self_approval`, `require_clocked_in`, and
`privilege_group` (0-3). Native Frappe DocPerms remain the hard ceiling.

Storage & Security Invariants (R2):
- Approval PINs live exclusively in Frappe's `__Auth` table via
  `frappe.utils.password.update_password` / `check_password`
  (`doctype="POS Override PIN"`, `fieldname="pin_hash"`), never on `User`.
- Password re-entry in `set_my_pin` catches `frappe.AuthenticationError` and
  refuses with `frappe.ValidationError` (`[POS-AUTH-PASSWORD]` / `[POS-AUTH-LOCKED]`)
  so a typo never clears session cookies or kills the live till session.
- Impersonated (`impersonated_by`), non-System-User, API-key, Bearer, and AI
  agent sessions are refused everywhere (authorise, consume, may_perform,
  list_approvers, enrolment).
- Zero-rules companies fail closed to manager roles only (`_MGR_ROLES`), never
  "everyone". Explicit `max_percent=0` on a discount rule is 0%, never fallback.
- `consume()` re-checks both approver and requester (enabled, outlet, roles,
  rules, caps, clock-in, check-owner) and records consumption in an isolated
  committed DB connection so caller rollbacks can never replay a spent token
  and caller business writes are never leaked.
- `requires_reason` is enforced server-side against active `POS Reason` rows
  (with mandatory note when `requires_note=1`).
- Lockout is DB-backed (`POS Authorisation Log`) + Redis-accelerated, scoped
  per `(approver, pos_profile)`, with a per-session rate limit at the till.

Refusal codes (plain sentence first, neutral wording):
- POS-AUTH-REQUIRED      (PermissionError): action needs manager approval
- POS-AUTH-SELF          (PermissionError): requester or check owner cannot self-approve
- POS-AUTH-NOT-ELIGIBLE  (PermissionError): approver/requester disabled, wrong outlet/company,
                         not clocked in, lacks rule/limit/group, Administrator/Guest,
                         or impersonated / non-interactive / AI session
- POS-AUTH-PIN           (ValidationError): wrong approval PIN
- POS-AUTH-PASSWORD      (ValidationError): wrong current account password during PIN enrolment
- POS-AUTH-LOCKED        (ValidationError): approver PIN, password enrolment, or till session locked
- POS-AUTH-NO-PIN        (ValidationError): approver has not enrolled an approval PIN
- POS-AUTH-REASON        (ValidationError): missing, inactive, wrong-privilege, or incomplete reason
- POS-AUTH-EXPIRED       (ValidationError): token missing, expired (>120s), or already used
- POS-AUTH-CHANGED       (ValidationError): claim, check, privilege, till, or session changed
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime


def _get_fullname(user: str) -> str:
    try:
        from frappe.utils import get_fullname

        return get_fullname(user) or user
    except ImportError:
        return user


def _get_login_attempt_tracker_cls():
    from frappe.auth import LoginAttemptTracker

    return LoginAttemptTracker


def _add_authentication_log(subject: str, user: str) -> None:
    from frappe.core.doctype.activity_log.activity_log import add_authentication_log

    add_authentication_log(subject, user)


def _get_user_permissions(user: str):
    from frappe.core.doctype.user_permission.user_permission import get_user_permissions

    return get_user_permissions(user)


def _check_password(user: str, pwd: str, **kwargs):
    from frappe.utils.password import check_password

    return check_password(user, pwd, **kwargs)


def _update_password(user: str, pwd: str, **kwargs):
    from frappe.utils.password import update_password

    return update_password(user, pwd, **kwargs)

TOKEN_TTL_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCK_MINUTES = 15
DEFAULT_PASSWORD_MAX_ATTEMPTS = 5
DEFAULT_PASSWORD_LOCK_MINUTES = 15
MIN_REASON_NOTE_LENGTH = 4
PIN_PATTERN = re.compile(r"^[0-9]{4,8}$")

PIN_AUTH_DOCTYPE = "POS Override PIN"
PIN_AUTH_FIELDNAME = "pin_hash"

AGENT_ROLES = frozenset({"Kamra Agent", "AI Agent", "HUF Agent"})
SECRET_FORM_KEYS = ("pin", "password", "current_password", "new_pin", "confirm_pin")
FORBIDDEN_CLAIM_KEYS = frozenset({"pin", "password", "pin_hash", "custom_override_pin_hash"})

_MGR_ROLES = ("URY Manager", "URY Admin", "Accounts Manager")
_FLOOR_AND_MGR = ("URY Captain", "URY Cashier", "URY Manager", "URY Admin", "Accounts Manager")
_CASHIER_AND_MGR = ("URY Cashier", "URY Manager", "URY Admin", "Accounts Manager")


class PrivilegeSpec(NamedTuple):
    label: str
    reason_required: bool
    default_perform_roles: tuple[str, ...]
    default_authorise_roles: tuple[str, ...]


# Final 35 privilege codes (Contract + Amendment 1; add only):
# code -> (label, reason_required default, default perform roles, default authorise roles)
PRIVILEGES: dict[str, PrivilegeSpec] = {
    "discount": PrivilegeSpec("Apply Discount", True, _CASHIER_AND_MGR, _MGR_ROLES),
    "discount_remove": PrivilegeSpec("Remove Discount", True, _MGR_ROLES, _MGR_ROLES),
    "complimentary": PrivilegeSpec("Complimentary Check", True, (), _MGR_ROLES),
    "void_sent_item": PrivilegeSpec("Void Sent Item", True, _MGR_ROLES, _MGR_ROLES),
    "reduce_sent_qty": PrivilegeSpec("Reduce Sent Quantity", True, _MGR_ROLES, _MGR_ROLES),
    "cancel_check": PrivilegeSpec("Void / Cancel Check", True, _MGR_ROLES, _MGR_ROLES),
    "return": PrivilegeSpec("Return / Refund Check", True, _MGR_ROLES, _MGR_ROLES),
    "reopen_check": PrivilegeSpec("Reopen Closed Check", True, _MGR_ROLES, _MGR_ROLES),
    "adjust_closed_check": PrivilegeSpec("Adjust Closed Check", True, _MGR_ROLES, _MGR_ROLES),
    "price_override": PrivilegeSpec("Price Override", True, _MGR_ROLES, _MGR_ROLES),
    "open_item": PrivilegeSpec("Open / Zero-Price Item", True, _MGR_ROLES, _MGR_ROLES),
    "service_charge_waive": PrivilegeSpec("Waive Service Charge", True, _MGR_ROLES, _MGR_ROLES),
    "tax_exempt": PrivilegeSpec("Tax Exempt Check", True, _MGR_ROLES, _MGR_ROLES),
    "transfer_check": PrivilegeSpec("Transfer Check / Table", False, _MGR_ROLES, _MGR_ROLES),
    "pickup_other_check": PrivilegeSpec("Pick Up Another Operator's Check", False, _MGR_ROLES, _MGR_ROLES),
    "merge_checks": PrivilegeSpec("Merge / Add Checks", False, _MGR_ROLES, _MGR_ROLES),
    "split_check": PrivilegeSpec("Split Check", False, _FLOOR_AND_MGR, _MGR_ROLES),
    "guest_count_change": PrivilegeSpec("Change Guest Count", False, _FLOOR_AND_MGR, _MGR_ROLES),
    "order_type_change": PrivilegeSpec("Change Order Type", False, _FLOOR_AND_MGR, _MGR_ROLES),
    "reprint_bill": PrivilegeSpec("Reprint Bill / Receipt", False, _CASHIER_AND_MGR, _MGR_ROLES),
    "reprint_kot": PrivilegeSpec("Reprint Kitchen Ticket", False, _CASHIER_AND_MGR, _MGR_ROLES),
    "no_sale": PrivilegeSpec("No Sale (Open Cash Drawer)", True, _MGR_ROLES, _MGR_ROLES),
    "paid_out": PrivilegeSpec("Paid Out", True, _MGR_ROLES, _MGR_ROLES),
    "cash_pickup": PrivilegeSpec("Cash Pickup / Safe Drop", True, _MGR_ROLES, _MGR_ROLES),
    "cash_loan": PrivilegeSpec("Cash Loan / Float Top-Up", True, _MGR_ROLES, _MGR_ROLES),
    "over_short_close": PrivilegeSpec("Close Till Over/Short Variance", True, _MGR_ROLES, _MGR_ROLES),
    "over_halo_tender": PrivilegeSpec("Over-Limit Tender", True, _MGR_ROLES, _MGR_ROLES),
    "on_account_over_limit": PrivilegeSpec("On-Account Over Credit Limit", True, _MGR_ROLES, _MGR_ROLES),
    "room_charge_over_limit": PrivilegeSpec("Room Charge Over Limit", True, _MGR_ROLES, _MGR_ROLES),
    "tip_adjust": PrivilegeSpec("Adjust Tip", True, _MGR_ROLES, _MGR_ROLES),
    "item_86_restore": PrivilegeSpec("Restore 86'd Menu Item", False, _MGR_ROLES, _MGR_ROLES),
    "reservation_cancel": PrivilegeSpec("Cancel Table Reservation", True, _MGR_ROLES, _MGR_ROLES),
    "offline_tender": PrivilegeSpec("Offline Tender Override", True, _MGR_ROLES, _MGR_ROLES),
    "close_other_till": PrivilegeSpec("Close Another Operator's Till", True, _MGR_ROLES, _MGR_ROLES),
    "reopen_till": PrivilegeSpec("Reopen Closed Till", True, _MGR_ROLES, _MGR_ROLES),
    "fx_rate_override": PrivilegeSpec("Override Foreign Currency Rate", True, _MGR_ROLES, _MGR_ROLES),
}


def _scrub_form_secrets() -> None:
    form_dict = getattr(frappe, "form_dict", None)
    if isinstance(form_dict, dict):
        for key in SECRET_FORM_KEYS:
            form_dict.pop(key, None)


def _refuse(code: str, message: str, exc=None) -> None:
    frappe.throw(f"{frappe._(message)} [{code}]", exc or frappe.PermissionError)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _validate_privilege(privilege: str) -> PrivilegeSpec:
    if not isinstance(privilege, str) or privilege not in PRIVILEGES:
        frappe.throw(frappe._("Unknown POS privilege."), frappe.ValidationError)
    return PRIVILEGES[privilege]


def _is_impersonated_session() -> bool:
    """True when the active Frappe session is impersonating another user (sudo)."""
    flags = getattr(frappe, "flags", None)
    if flags and getattr(flags, "impersonated_by", None):
        return True

    session = getattr(frappe, "session", None)
    if session is not None:
        if getattr(session, "impersonated_by", None):
            return True
        sdata = getattr(session, "data", None)
        if isinstance(sdata, dict):
            if sdata.get("impersonated_by") or sdata.get("impersonator") or sdata.get("is_impersonated"):
                return True
            user_type = sdata.get("user_type")
            if user_type and str(user_type) != "System User":
                return True

    local = getattr(frappe, "local", None)
    session_obj = getattr(local, "session_obj", None) if local is not None else None
    if session_obj is not None:
        obj_data = getattr(session_obj, "data", None)
        if isinstance(obj_data, dict):
            if obj_data.get("impersonated_by") or obj_data.get("impersonator") or obj_data.get("is_impersonated"):
                return True
            user_type = obj_data.get("user_type")
            if user_type and str(user_type) != "System User":
                return True

    return False


def _is_non_interactive_or_agent_session(user: str | None = None) -> bool:
    """True for AI agent calls, MCP, impersonated (sudo), or non-interactive API-key / OAuth sessions."""
    if _is_impersonated_session():
        return True

    flags = getattr(frappe, "flags", None)
    if flags:
        for flag_name in ("kamra_agent_call", "ury_agent_call", "mcp_call", "in_mcp", "api_key_auth"):
            if getattr(flags, flag_name, None):
                return True

    user = user or getattr(getattr(frappe, "session", None), "user", None)
    if user and user not in ("Administrator", "Guest"):
        if AGENT_ROLES & set(frappe.get_roles(user)):
            return True

    request = getattr(frappe, "request", None)
    if request is not None:
        try:
            headers = getattr(request, "headers", None) or {}
            cookies = getattr(request, "cookies", None) or {}
        except RuntimeError:
            # Unbound Werkzeug LocalProxy outside an HTTP request context.
            return False
        auth_header = headers.get("Authorization") or headers.get("authorization") or ""
        if str(auth_header).strip():
            return True
        sid = cookies.get("sid")
        if not sid or sid == "Guest":
            return True

    return False


def _has_real_isolated_db() -> bool:
    conf = getattr(frappe, "conf", None)
    if not conf or not callable(getattr(conf, "get", None)):
        return False
    return bool(conf.get("db_name"))


def _with_isolated_db(fn, *, commit: bool = False):
    """Run `fn(db)` on an independent DB connection so writes/reads neither leak nor depend on caller's tx."""
    if not _has_real_isolated_db():
        return fn(frappe.db)

    from frappe.database import get_db

    conf = frappe.local.conf
    aux = get_db(
        socket=conf.get("db_socket"),
        host=conf.get("db_host"),
        user=conf.get("db_user"),
        password=conf.get("db_password"),
        port=conf.get("db_port"),
        cur_db_name=conf.get("db_name"),
    )
    try:
        result = fn(aux)
        if commit:
            aux.commit()
        return result
    except Exception:
        try:
            aux.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            aux.close()
        except Exception:
            pass


def _has_override_pin(user: str) -> bool:
    if not user or user in ("Administrator", "Guest"):
        return False
    rows = frappe.db.sql(
        "SELECT name FROM `__Auth` WHERE doctype = %s AND name = %s AND fieldname = %s AND encrypted = 0 LIMIT 1",
        (PIN_AUTH_DOCTYPE, user, PIN_AUTH_FIELDNAME),
    )
    return bool(rows)


def _users_with_override_pin() -> set[str]:
    rows = frappe.db.sql(
        "SELECT name FROM `__Auth` WHERE doctype = %s AND fieldname = %s AND encrypted = 0",
        (PIN_AUTH_DOCTYPE, PIN_AUTH_FIELDNAME),
    )
    return {str(row[0]).casefold() for row in (rows or ()) if row and row[0]}


def _clear_override_pin(user: str) -> None:
    frappe.db.sql(
        "DELETE FROM `__Auth` WHERE doctype = %s AND name = %s AND fieldname = %s",
        (PIN_AUTH_DOCTYPE, user, PIN_AUTH_FIELDNAME),
    )


def _canonicalize_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            frappe.throw(frappe._("Claim values must be finite numbers."), frappe.ValidationError)
        try:
            dec = Decimal(str(value)).normalize()
        except InvalidOperation:
            frappe.throw(frappe._("Invalid numeric value in approval claim."), frappe.ValidationError)
        if dec == 0:
            return "0"
        return format(dec, "f")
    if isinstance(value, dict):
        out = {}
        for k in sorted(value.keys(), key=str):
            sk = str(k)
            if sk.casefold() in FORBIDDEN_CLAIM_KEYS:
                continue
            out[sk] = _canonicalize_value(value[k])
        return out
    if isinstance(value, (list, tuple)):
        return [_canonicalize_value(v) for v in value]
    return str(value)


def _parse_claim(claim: Any) -> dict[str, Any]:
    if claim is None:
        return {}
    if isinstance(claim, str):
        try:
            claim = json.loads(claim)
        except ValueError:
            frappe.throw(frappe._("Invalid approval claim payload."), frappe.ValidationError)
    if not isinstance(claim, dict):
        frappe.throw(frappe._("Approval claim must be an object."), frappe.ValidationError)
    return {
        str(k): v
        for k, v in claim.items()
        if str(k).casefold() not in FORBIDDEN_CLAIM_KEYS
    }


def _canonical_claim_json(claim: Any) -> str:
    parsed = _parse_claim(claim)
    normalized = _canonicalize_value(parsed)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_claim_hash(claim: Any) -> str:
    return hashlib.sha256(_canonical_claim_json(claim).encode("utf-8")).hexdigest()


def _parse_optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _get_profile(pos_profile: str):
    if not pos_profile or not frappe.db.exists("POS Profile", pos_profile):
        return None
    profile = frappe.get_doc("POS Profile", pos_profile)
    if cint(profile.get("disabled")):
        return None
    return profile


def _pin_policy(profile) -> tuple[int, int]:
    max_attempts = cint(profile.get("custom_override_max_attempts")) if profile else 0
    lock_minutes = cint(profile.get("custom_override_lock_minutes")) if profile else 0
    conf = getattr(frappe, "conf", None)
    if not max_attempts and callable(getattr(conf, "get", None)):
        max_attempts = cint(conf.get("pos_override_max_attempts"))
    if not lock_minutes and callable(getattr(conf, "get", None)):
        lock_minutes = cint(conf.get("pos_override_lock_minutes"))
    return (
        max(1, max_attempts or DEFAULT_MAX_ATTEMPTS),
        max(1, lock_minutes or DEFAULT_LOCK_MINUTES),
    )


def _session_rate_limit(max_attempts: int) -> int:
    conf = getattr(frappe, "conf", None)
    configured = cint(conf.get("pos_override_session_max_attempts")) if callable(getattr(conf, "get", None)) else 0
    if configured > 0:
        return configured
    return max(max_attempts * 2, 10)


def _pin_tracker(approver: str, profile) -> tuple[Any, int, int]:
    max_attempts, lock_minutes = _pin_policy(profile)
    scope = (profile.name if profile else "").casefold()
    tracker = _get_login_attempt_tracker_cls()(
        f"pos-override-pin:{approver.casefold()}:{scope}",
        max_consecutive_login_attempts=max(0, max_attempts - 1),
        lock_interval=lock_minutes * 60,
    )
    return tracker, max_attempts, lock_minutes


def _session_tracker(requester: str, profile, max_attempts: int, lock_minutes: int) -> tuple[Any, int]:
    session_cap = _session_rate_limit(max_attempts)
    scope = (profile.name if profile else "").casefold()
    tracker = _get_login_attempt_tracker_cls()(
        f"pos-override-session:{requester.casefold()}:{scope}",
        max_consecutive_login_attempts=max(0, session_cap - 1),
        lock_interval=lock_minutes * 60,
    )
    return tracker, session_cap


def _password_tracker(user: str) -> tuple[Any, int, int]:
    max_attempts = DEFAULT_PASSWORD_MAX_ATTEMPTS
    lock_minutes = DEFAULT_PASSWORD_LOCK_MINUTES
    tracker = _get_login_attempt_tracker_cls()(
        f"pos-override-pw:{user.casefold()}",
        max_consecutive_login_attempts=max(0, max_attempts - 1),
        lock_interval=lock_minutes * 60,
    )
    return tracker, max_attempts, lock_minutes


def _db_failed_pin_count(approver: str, pos_profile: str, lock_minutes: int) -> int:
    """Count recent Failed PIN attempts for `(approver, pos_profile)` in MariaDB since last reset."""
    window_start = now_datetime() - timedelta(minutes=lock_minutes)

    def _query(db):
        reset_rows = db.sql(
            """
            SELECT MAX(event_time)
            FROM `tabPOS Authorisation Log`
            WHERE approver = %s
              AND ((outcome = 'Approved' AND pos_profile = %s) OR outcome IN ('PIN Reset'))
              AND event_time >= %s
            """,
            (approver, pos_profile, window_start),
        )
        last_reset = reset_rows[0][0] if reset_rows and reset_rows[0] else None
        since = max(window_start, get_datetime(last_reset)) if last_reset else window_start
        cnt_rows = db.sql(
            """
            SELECT COUNT(*)
            FROM `tabPOS Authorisation Log`
            WHERE approver = %s
              AND pos_profile = %s
              AND outcome = 'Failed PIN'
              AND event_time > %s
            """,
            (approver, pos_profile, since),
        )
        return cint(cnt_rows[0][0]) if cnt_rows and cnt_rows[0] else 0

    return _with_isolated_db(_query, commit=False)


def _db_session_failed_pin_count(requester: str, pos_profile: str, lock_minutes: int) -> int:
    """Count recent Failed PIN attempts from `(requester, pos_profile)` across all approvers."""
    window_start = now_datetime() - timedelta(minutes=lock_minutes)

    def _query(db):
        cnt_rows = db.sql(
            """
            SELECT COUNT(*)
            FROM `tabPOS Authorisation Log`
            WHERE requester = %s
              AND pos_profile = %s
              AND outcome = 'Failed PIN'
              AND event_time > %s
            """,
            (requester, pos_profile, window_start),
        )
        return cint(cnt_rows[0][0]) if cnt_rows and cnt_rows[0] else 0

    return _with_isolated_db(_query, commit=False)


def _db_failed_password_count(user: str, lock_minutes: int) -> int:
    """Count recent Failed Password attempts for `set_my_pin` since last PIN Reset."""
    window_start = now_datetime() - timedelta(minutes=lock_minutes)

    def _query(db):
        reset_rows = db.sql(
            """
            SELECT MAX(event_time)
            FROM `tabPOS Authorisation Log`
            WHERE approver = %s
              AND outcome = 'PIN Reset'
              AND event_time >= %s
            """,
            (user, window_start),
        )
        last_reset = reset_rows[0][0] if reset_rows and reset_rows[0] else None
        since = max(window_start, get_datetime(last_reset)) if last_reset else window_start
        cnt_rows = db.sql(
            """
            SELECT COUNT(*)
            FROM `tabPOS Authorisation Log`
            WHERE approver = %s
              AND outcome = 'Failed Password'
              AND event_time > %s
            """,
            (user, since),
        )
        return cint(cnt_rows[0][0]) if cnt_rows and cnt_rows[0] else 0

    return _with_isolated_db(_query, commit=False)


def _external_pin_verifier():
    hooks = frappe.get_hooks("pos_override_pin_verifier") or []
    if isinstance(hooks, str):
        hooks = [hooks]
    valid = [h for h in hooks if isinstance(h, str) and h.strip()]
    if not valid:
        return None
    return frappe.get_attr(valid[-1])


def _external_pin_manager():
    """Resolve manager(action, *, user=None, pin=None, caller=None, pos_profile=None).

    Status returns enrollment flags. Set and clear persist changes and raise on failure.
    """
    hooks = frappe.get_hooks("pos_override_pin_manager") or []
    if isinstance(hooks, str):
        hooks = [hooks]
    valid = [h for h in hooks if isinstance(h, str) and h.strip()]
    if not valid:
        return None
    return frappe.get_attr(valid[-1])


def _external_pin_status(manager, user: str) -> dict[str, bool]:
    status = manager("status", user=user)
    return {
        "has_pin": bool(status.get("has_pin")),
        "must_reset": bool(status.get("must_reset", False)),
        "locked": bool(status.get("locked", False)),
    }


def _refuse_external_pin_management():
    frappe.throw(
        frappe._("Approval PINs on this site are managed by the property's cashier PIN store."),
        frappe.ValidationError,
    )


def _is_outlet_member(user: str, profile) -> bool:
    """Enabled, non-Admin/Guest user assigned to this POS Profile/branch/company."""
    if not user or user in ("Administrator", "Guest") or not profile:
        return False
    if cint(frappe.db.get_value("User", user, "enabled")) != 1:
        return False

    perms = _get_user_permissions(user) or {}
    company_perms = {p.get("doc") for p in perms.get("Company", []) if p.get("doc")}
    if company_perms and profile.company not in company_perms:
        return False

    branch = profile.get("branch")
    branch_perms = {p.get("doc") for p in perms.get("Branch", []) if p.get("doc")}
    if branch and branch_perms and branch not in branch_perms:
        return False

    profile_perms = {p.get("doc") for p in perms.get("POS Profile", []) if p.get("doc")}
    if profile_perms and profile.name not in profile_perms:
        return False

    applicable_users = {
        row.get("user") for row in (profile.get("applicable_for_users") or []) if row.get("user")
    }
    in_profile = user in applicable_users
    in_branch = bool(
        branch
        and frappe.db.exists("URY User", {"parent": branch, "parenttype": "Branch", "user": user})
    )

    if in_profile or in_branch:
        return True

    if not branch and not applicable_users:
        if company_perms and profile.company in company_perms:
            return True
        if frappe.db.exists("Employee", {"user_id": user, "company": profile.company}):
            return True

    return False


def _is_clocked_in_if_attendance_active(user: str, company: str) -> bool:
    """Simphony Opt 182: delegate to `ury_pos.timeclock.is_user_clocked_in` when available."""
    try:
        from ury.ury_pos import timeclock

        return timeclock.is_user_clocked_in(
            user,
            company=company,
            allow_if_no_history_without_hrms=True,
        )
    except Exception:
        if not frappe.db.table_exists("Employee Checkin"):
            return True
        employee = frappe.db.get_value(
            "Employee", {"user_id": user, "company": company, "status": "Active"}, "name"
        )
        if not employee:
            return True
        if not frappe.db.exists("Employee Checkin", {"employee": employee}):
            return True
        latest = frappe.db.get_value(
            "Employee Checkin",
            {"employee": employee},
            "log_type",
            order_by="time desc",
        )
        return str(latest or "").upper() == "IN"


def _effective_rules_for_roles(company: str, pos_profile: str, privilege: str, roles: set[str]) -> dict[str, Any]:
    """Resolve POS Privilege Rule per role; narrower POS Profile row wins over company-wide row."""
    if not company or not roles:
        return {}
    rows = frappe.get_all(
        "POS Privilege Rule",
        filters={
            "company": company,
            "privilege": privilege,
            "role": ["in", sorted(roles)],
            "pos_profile": ["in", [pos_profile, "", None]],
        },
        fields=[
            "name", "pos_profile", "role", "may_perform", "may_authorise",
            "max_percent", "max_amount", "requires_reason",
            "allow_self_approval", "require_clocked_in", "privilege_group",
        ],
    )
    resolved: dict[str, Any] = {}
    for row in rows:
        role = row.get("role")
        is_profile_row = bool(row.get("pos_profile"))
        existing = resolved.get(role)
        if existing is None or (is_profile_row and not existing.get("pos_profile")):
            resolved[role] = row
    return resolved


def _parse_role_limits(raw: str | None) -> dict[str, float]:
    """Parse Kamra/B1 'Role: %' textarea; ignores malformed and non-finite values."""
    limits: dict[str, float] = {}
    for line in str(raw or "").splitlines():
        if ":" not in line:
            continue
        role_part, pct_part = line.split(":", 1)
        role_name = role_part.strip()
        if not role_name:
            continue
        try:
            val = float(pct_part.strip().rstrip("%"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(val) or val < 0 or val > 100:
            continue
        if role_name not in limits or val > limits[role_name]:
            limits[role_name] = val
    return limits


def _parse_base_discount_limit(raw: Any) -> float:
    """At the till, a blank base discount limit is 0% (Kamra #32 / B1 / Amendment 1)."""
    text = str(raw if raw is not None else "").strip().rstrip("%")
    if not text:
        return 0.0
    try:
        val = float(text)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(val) or val < 0 or val > 100:
        return 0.0
    return val


def _approval_limit_doc(company: str):
    if not company or not frappe.db.table_exists("Approval Limit"):
        return None
    if not frappe.db.exists("Approval Limit", company):
        return None
    return frappe.db.get_value(
        "Approval Limit",
        company,
        ["max_document_discount_percent", "discount_limits_by_role", "discount_approver_role"],
        as_dict=True,
    )


def _discount_limit_for_user(company: str, user_roles: set[str]) -> tuple[float, str | None, bool]:
    """Return (user_percent_limit, discount_approver_role, has_approval_limit_record)."""
    al = _approval_limit_doc(company)
    if not al:
        return 0.0, None, False
    role_limits = _parse_role_limits(al.get("discount_limits_by_role"))
    matched = [pct for role, pct in role_limits.items() if role in user_roles]
    if matched:
        limit = max(matched)
    else:
        limit = _parse_base_discount_limit(al.get("max_document_discount_percent"))
    approver_role = (al.get("discount_approver_role") or "").strip() or None
    return limit, approver_role, True


def _rule_matches_group_and_clock(rule, user: str, company: str, privilege_group: int = 0) -> bool:
    req_pg = cint(privilege_group)
    rule_pg = cint(rule.get("privilege_group"))
    if req_pg > 0 and rule_pg > 0 and rule_pg != req_pg:
        return False
    if cint(rule.get("require_clocked_in")) == 1 and not _is_clocked_in_if_attendance_active(user, company):
        return False
    return True


def _rule_covers_limits(rule, percent: float | None, amount: float | None) -> bool:
    max_pct = flt(rule.get("max_percent"))
    max_amt = flt(rule.get("max_amount"))
    if percent is not None and max_pct > 0 and percent > max_pct + 1e-9:
        return False
    if amount is not None and max_amt > 0 and amount > max_amt + 1e-9:
        return False
    return True


def _check_owners(
    reference_doctype: str | None,
    reference_name: str | None,
    *,
    profile=None,
) -> set[str]:
    if not reference_name:
        return set()
    dt = reference_doctype or "POS Invoice"
    owners: set[str] = set()
    fields = ["owner", "waiter", "company", "branch", "pos_profile"] if dt == "POS Invoice" else ["owner"]
    row = frappe.db.get_value(dt, reference_name, fields, as_dict=True)
    if isinstance(row, dict):
        if profile is not None and dt == "POS Invoice":
            if row.get("company") and row.get("company") != profile.company:
                return set()
            if profile.get("branch") and row.get("branch") and row.get("branch") != profile.get("branch"):
                return set()
        for field in ("owner", "waiter"):
            val = (row.get(field) or "").strip()
            if val:
                owners.add(val.casefold())
    elif isinstance(row, str) and row.strip():
        owners.add(row.strip().casefold())
    return owners


def is_policy_active(company: str | None) -> bool:
    """True when `company` has at least one POS Privilege Rule row.

    NOTE (P1 #5): Consumers must NOT skip authorisation when this returns False;
    `may_perform`/`require` fail closed to manager roles only when no rules exist.
    """
    if not isinstance(company, str) or not company.strip():
        return False
    return bool(frappe.db.exists("POS Privilege Rule", {"company": company}))


def on_company_after_insert(doc, method=None) -> None:
    """Auto-seed POS Privilege Rules and POS Reasons when a new Company is created (P1 #5)."""
    if getattr(frappe.flags, "in_install", False) or getattr(frappe.flags, "in_migrate", False):
        return
    if not doc or not getattr(doc, "name", None):
        return
    try:
        from ury.patches.v2_0 import setup_pos_manager_authorisation

        setup_pos_manager_authorisation.seed_default_rules(company=doc.name)
        setup_pos_manager_authorisation.seed_default_reasons()
    except Exception:
        # Never block Company creation if POS tables aren't synced yet during bootstrap.
        pass


class _VerifiedApprovalProof:
    """In-process proof that an override check succeeded; cannot be forged from JSON."""

    __slots__ = ("approver", "docname", "privilege")

    def __init__(self, privilege: str, docname: str, approver: str | None = None):
        self.privilege = privilege
        self.docname = docname
        self.approver = approver


def mark_verified_approval(doc, privilege: str, approval=None) -> None:
    """Attach an unforgeable in-process approval proof onto `doc.flags`."""
    _validate_privilege(privilege)
    approver = getattr(approval, "approver", None) if approval is not None else None
    doc.flags[f"pos_override_verified_{privilege}"] = _VerifiedApprovalProof(
        privilege=privilege,
        docname=str(getattr(doc, "name", "") or ""),
        approver=approver,
    )


def has_verified_approval(doc, privilege: str) -> bool:
    """True only when `mark_verified_approval` attached a matching proof in-process."""
    proof = getattr(doc, "flags", {}).get(f"pos_override_verified_{privilege}")
    if not isinstance(proof, _VerifiedApprovalProof):
        return False
    return proof.privilege == privilege and proof.docname == str(getattr(doc, "name", "") or "")


def requires_reason(pos_profile: str, privilege: str) -> bool:
    """Whether an operator reason is required for this privilege at this till."""
    spec = _validate_privilege(privilege)
    profile = _get_profile(pos_profile)
    if not profile:
        return spec.reason_required
    rows = frappe.get_all(
        "POS Privilege Rule",
        filters={
            "company": profile.company,
            "privilege": privilege,
            "pos_profile": ["in", [profile.name, "", None]],
        },
        fields=["pos_profile", "requires_reason"],
    )
    if not rows:
        return spec.reason_required
    profile_rows = [r for r in rows if r.get("pos_profile")]
    target_rows = profile_rows or rows
    return any(cint(r.get("requires_reason")) == 1 for r in target_rows)


def _validate_reason(pos_profile: str, privilege: str, parsed_claim: dict[str, Any]) -> str:
    """Enforce `requires_reason` server-side against active `POS Reason` rows (P1 #7)."""
    raw_reason = str(parsed_claim.get("reason") or "").strip()
    raw_code = str(parsed_claim.get("reason_code") or "").strip()
    raw_note = str(parsed_claim.get("note") or "").strip()

    if not requires_reason(pos_profile, privilege):
        return raw_reason or raw_code

    # Discounts & complimentary checks use siteops Discount Reason (Amendment 1 #4), not POS Reason.
    if privilege in ("discount", "complimentary"):
        candidate = raw_reason or raw_code
        if not candidate:
            _refuse("POS-AUTH-REASON", "Select a reason for this discount.", frappe.ValidationError)
        return candidate

    if privilege == "discount_remove":
        return raw_reason or raw_code or "Remove discount"

    if privilege == "service_charge_waive" and str(parsed_claim.get("sc_action") or "").strip().casefold() == "restore":
        return raw_reason or raw_code or "Restore automatic service charge"

    active_reasons = list_reasons(privilege)
    if not active_reasons:
        candidate = raw_reason or raw_code
        if len(candidate) < MIN_REASON_NOTE_LENGTH:
            _refuse(
                "POS-AUTH-REASON",
                "Enter a reason (at least 4 characters) for this action.",
                frappe.ValidationError,
            )
        return candidate

    matched = None
    extracted_note = raw_note

    if raw_code:
        code_cf = raw_code.casefold()
        for row in active_reasons:
            if str(row.get("code") or "").casefold() == code_cf:
                matched = row
                break
        if matched and not extracted_note and raw_reason:
            desc_prefix = f"{matched.get('description') or ''}:"
            code_prefix = f"{matched.get('code') or ''}:"
            if raw_reason.casefold().startswith(desc_prefix.casefold()):
                extracted_note = raw_reason[len(desc_prefix):].strip()
            elif raw_reason.casefold().startswith(code_prefix.casefold()):
                extracted_note = raw_reason[len(code_prefix):].strip()

    if matched is None and raw_reason:
        reason_cf = raw_reason.casefold()
        for row in active_reasons:
            code_s = str(row.get("code") or "").strip()
            desc_s = str(row.get("description") or "").strip()
            if (code_s and reason_cf == code_s.casefold()) or (desc_s and reason_cf == desc_s.casefold()):
                matched = row
                break
            for prefix in (code_s, desc_s):
                if prefix and reason_cf.startswith(f"{prefix.casefold()}:"):
                    matched = row
                    tail = raw_reason[len(prefix) + 1:].strip()
                    if tail and not extracted_note:
                        extracted_note = tail
                    break
            if matched is not None:
                break

    if matched is None:
        _refuse(
            "POS-AUTH-REASON",
            "Choose an active reason for this action.",
            frappe.ValidationError,
        )

    if cint(matched.get("requires_note")) == 1 and len(extracted_note.strip()) < MIN_REASON_NOTE_LENGTH:
        _refuse(
            "POS-AUTH-REASON",
            "Add a short note (at least 4 characters) explaining the reason.",
            frappe.ValidationError,
        )

    return raw_reason or str(matched.get("description") or matched.get("code") or "")


def may_perform(
    user: str,
    pos_profile: str,
    privilege: str,
    percent: float | None = None,
    amount: float | None = None,
    privilege_group: int = 0,
) -> bool:
    """True when `user` may perform `privilege` alone at `pos_profile`."""
    _validate_privilege(privilege)
    if not user or user in ("Administrator", "Guest"):
        return False
    # P0 #3: impersonated / agent / non-interactive sessions cannot perform privileged overrides.
    if _is_non_interactive_or_agent_session(user):
        return False
    profile = _get_profile(pos_profile)
    if not profile or not _is_outlet_member(user, profile):
        return False

    pct = _parse_optional_number(percent)
    amt = _parse_optional_number(amount)

    # Full comp (100%) always requires a second person, whatever the role.
    if privilege == "complimentary":
        return False
    if privilege == "discount" and pct is not None and pct >= 100.0 - 1e-9:
        return False

    roles = set(frappe.get_roles(user))
    if not roles:
        return False

    # P1 #5: Fail closed when a company has zero rules configured:
    # ONLY today's manager roles may act alone (never cashiers, never "everyone");
    # discounts remain 0% alone without explicit rules + Approval Limit.
    if not is_policy_active(profile.company):
        if privilege == "discount":
            return pct is not None and pct <= 0.0
        return bool(roles & set(_MGR_ROLES))

    effective = _effective_rules_for_roles(profile.company, profile.name, privilege, roles)

    if privilege == "discount":
        # Amendment 1 #3 + P1 #5: every role starts at 0% alone; explicit 0% rule is 0%,
        # NEVER falls back to Approval Limit! min(Approval Limit, rule) wins.
        al_limit, _approver_role, has_al = _discount_limit_for_user(profile.company, roles)
        if not has_al:
            return pct is not None and pct <= 0.0
        matching_perform_rules = [
            rule
            for rule in effective.values()
            if cint(rule.get("may_perform")) == 1
            and _rule_matches_group_and_clock(rule, user, profile.company, privilege_group)
        ]
        if not matching_perform_rules:
            return False
        best_effective_pct = 0.0
        for rule in matching_perform_rules:
            raw_rule_pct = rule.get("max_percent")
            rule_cap = max(0.0, flt(raw_rule_pct)) if raw_rule_pct is not None else 0.0
            cap = min(al_limit, rule_cap)
            if amt is not None and flt(rule.get("max_amount")) > 0 and amt > flt(rule.get("max_amount")) + 1e-9:
                continue
            if cap > best_effective_pct:
                best_effective_pct = cap
        if pct is None:
            return best_effective_pct > 0.0
        return pct <= best_effective_pct + 1e-9

    # Non-discount privilege: no rule for a role => may not act alone.
    return any(
        cint(rule.get("may_perform")) == 1
        and _rule_matches_group_and_clock(rule, user, profile.company, privilege_group)
        and _rule_covers_limits(rule, pct, amt)
        for rule in effective.values()
    )


def _matching_authorise_rules(
    approver: str,
    profile,
    privilege: str,
    percent: float | None = None,
    amount: float | None = None,
    privilege_group: int = 0,
) -> list[Any]:
    if not approver or approver in ("Administrator", "Guest"):
        return []
    if not _is_outlet_member(approver, profile):
        return []

    roles = set(frappe.get_roles(approver))
    if not roles or (AGENT_ROLES & roles):
        return []

    pct = _parse_optional_number(percent)
    amt = _parse_optional_number(amount)

    # P1 #5: Zero-rules company fallback allows ONLY manager roles to authorise via PIN.
    if not is_policy_active(profile.company):
        if roles & set(_MGR_ROLES):
            return [frappe._dict(allow_self_approval=0, max_percent=0, max_amount=0)]
        return []

    effective = _effective_rules_for_roles(profile.company, profile.name, privilege, roles)

    if privilege == "discount":
        al_limit, approver_role, has_al = _discount_limit_for_user(profile.company, roles)
        matched = []
        for rule in effective.values():
            if cint(rule.get("may_authorise")) != 1:
                continue
            if not _rule_matches_group_and_clock(rule, approver, profile.company, privilege_group):
                continue
            if amt is not None and flt(rule.get("max_amount")) > 0 and amt > flt(rule.get("max_amount")) + 1e-9:
                continue
            if has_al:
                is_top_approver = bool(approver_role and approver_role in roles)
                if is_top_approver:
                    matched.append(rule)
                    continue
                rule_cap = max(0.0, flt(rule.get("max_percent")))
                cap = min(al_limit, rule_cap)
                if pct is not None:
                    if cap >= pct - 1e-9 and cap > 0.0:
                        matched.append(rule)
                elif cap > 0.0 or not approver_role:
                    matched.append(rule)
            else:
                if _rule_covers_limits(rule, pct, amt):
                    matched.append(rule)
        if not matched and has_al and approver_role and approver_role in roles:
            matched.append(frappe._dict(allow_self_approval=0))
        return matched

    if privilege == "complimentary":
        al_limit, approver_role, has_al = _discount_limit_for_user(profile.company, roles)
        if has_al:
            effective_approver_role = approver_role
            if not effective_approver_role or effective_approver_role in (
                "All", "Guest", "Desk User", "Administrator", "System Manager"
            ):
                effective_approver_role = "Owner Approver"
            is_top_approver = effective_approver_role in roles
            has_full_comp_authority = is_top_approver or al_limit >= 100.0 - 1e-9
            if not has_full_comp_authority:
                return []
            matched = [
                rule
                for rule in effective.values()
                if cint(rule.get("may_authorise")) == 1
                and _rule_matches_group_and_clock(rule, approver, profile.company, privilege_group)
                and _rule_covers_limits(rule, 100.0, amt)
            ]
            if not matched and is_top_approver:
                matched.append(frappe._dict(allow_self_approval=0))
            return matched
        return [
            rule
            for rule in effective.values()
            if cint(rule.get("may_authorise")) == 1
            and _rule_matches_group_and_clock(rule, approver, profile.company, privilege_group)
            and _rule_covers_limits(rule, 100.0, amt)
        ]

    return [
        rule
        for rule in effective.values()
        if cint(rule.get("may_authorise")) == 1
        and _rule_matches_group_and_clock(rule, approver, profile.company, privilege_group)
        and _rule_covers_limits(rule, pct, amt)
    ]


def _allows_check_owner_self_approval(privilege: str, rules: list[Any], percent: float | None = None) -> bool:
    if privilege == "complimentary":
        return False
    if privilege == "discount" and percent is not None and percent >= 100.0 - 1e-9:
        return False
    return any(cint(r.get("allow_self_approval")) == 1 for r in rules)


def _write_log(
    *,
    requester: str | None,
    approver: str | None,
    privilege: str,
    pos_profile: str | None,
    company: str | None,
    reference_doctype: str | None,
    reference_name: str | None,
    claim_json: str,
    reason: str,
    outcome: str,
    refusal_code: str = "",
    token_hash: str | None = None,
    consumed_token_hash: str | None = None,
    claim_hash: str | None = None,
    expires_at=None,
    isolated: bool = True,
):
    """Write an insert-only POS Authorisation Log row.

    When `isolated=True` (default), writes and commits on an independent MariaDB
    connection so failed attempts, lockouts, and token consumptions survive caller
    rollbacks WITHOUT prematurely committing the caller's uncommitted business work.
    """
    now_ts = now_datetime()
    clean_reason = (reason or "").strip()[:500]
    actor = getattr(getattr(frappe, "session", None), "user", None) or requester or "Administrator"

    if isolated and _has_real_isolated_db():
        log_name = frappe.generate_hash(length=10)

        def _insert(db):
            db.sql(
                """
                INSERT INTO `tabPOS Authorisation Log` (
                    `name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`, `idx`,
                    `requester`, `approver`, `privilege`, `outcome`, `refusal_code`, `event_time`,
                    `pos_profile`, `company`, `reference_doctype`, `reference_name`, `reason`,
                    `claim_values`, `token_hash`, `consumed_token_hash`, `claim_hash`, `expires_at`
                ) VALUES (
                    %s, %s, %s, %s, %s, 0, 0,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                """,
                (
                    log_name, now_ts, now_ts, actor, actor,
                    requester, approver, privilege, outcome, refusal_code, now_ts,
                    pos_profile, company, reference_doctype or None, reference_name or None, clean_reason,
                    claim_json, token_hash, consumed_token_hash, claim_hash, expires_at,
                ),
            )
            return log_name

        _with_isolated_db(_insert, commit=True)
        return frappe._dict(name=log_name, outcome=outcome)

    doc = frappe.get_doc(
        {
            "doctype": "POS Authorisation Log",
            "requester": requester,
            "approver": approver,
            "privilege": privilege,
            "pos_profile": pos_profile,
            "company": company,
            "reference_doctype": reference_doctype or None,
            "reference_name": reference_name or None,
            "claim_values": claim_json,
            "reason": clean_reason,
            "outcome": outcome,
            "refusal_code": refusal_code,
            "event_time": now_ts,
            "token_hash": token_hash,
            "consumed_token_hash": consumed_token_hash,
            "claim_hash": claim_hash,
            "expires_at": expires_at,
        }
    )
    doc.flags.pos_override_server_insert = True
    if isolated and hasattr(doc, "_store"):
        doc.flags.ignore_permissions = True
        doc._store.insert_doc(doc, isolated=True)
    else:
        doc.insert(ignore_permissions=True, ignore_links=True)
    return doc


@frappe.whitelist(methods=["GET"])
def privilege_policy(
    pos_profile: str,
    privilege: str,
    percent: float | None = None,
    amount: float | None = None,
    privilege_group: int = 0,
) -> dict[str, Any]:
    """Return UI metadata for a privilege at this till (label, may_perform, requires_reason)."""
    spec = _validate_privilege(privilege)
    return {
        "privilege": privilege,
        "label": frappe._(spec.label),
        "may_perform": may_perform(
            frappe.session.user, pos_profile, privilege,
            percent=percent, amount=amount, privilege_group=privilege_group,
        ),
        "requires_reason": requires_reason(pos_profile, privilege),
    }


@frappe.whitelist(methods=["GET"])
def list_reasons(privilege: str | None = None) -> list[dict[str, Any]]:
    """Return active POS Reasons (sorted by sort_order) matching `privilege`."""
    if privilege is not None and privilege != "":
        _validate_privilege(privilege)
    if not frappe.db.table_exists("POS Reason"):
        return []
    rows = frappe.get_all(
        "POS Reason",
        filters={"disabled": 0},
        fields=["code", "description", "applies_to", "requires_note", "sort_order"],
        order_by="sort_order asc, code asc",
    )
    out = []
    for row in rows:
        raw_applies = row.get("applies_to") or ""
        targets = [part.strip() for part in re.split(r"[\n,]+", str(raw_applies)) if part.strip()]
        if privilege and targets and privilege not in targets:
            continue
        out.append({
            "code": row.get("code"),
            "description": row.get("description"),
            "requires_note": cint(row.get("requires_note")),
            "applies_to": targets,
        })
    return out


@frappe.whitelist(methods=["GET"])
def list_approvers(
    pos_profile: str,
    privilege: str,
    reference_name: str | None = None,
    percent: float | None = None,
    amount: float | None = None,
    privilege_group: int = 0,
) -> list[dict[str, str]]:
    """List eligible approvers for this till/privilege as [{user, full_name}] only.

    P1 #9: refuses callers who are not active members of `pos_profile`'s outlet/company
    (and impersonated/non-interactive sessions), preventing cross-company enumeration.
    """
    _validate_privilege(privilege)
    requester = frappe.session.user
    if not requester or requester in ("Administrator", "Guest") or _is_non_interactive_or_agent_session(requester):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "Manager approval is only available in an active interactive till session.",
            frappe.PermissionError,
        )

    profile = _get_profile(pos_profile)
    if not profile or not _is_outlet_member(requester, profile):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "You are not assigned to this till profile.",
            frappe.PermissionError,
        )

    hard_excluded = {requester.casefold(), "administrator", "guest"}
    owners = _check_owners("POS Invoice", reference_name, profile=profile)
    pct = _parse_optional_number(percent)

    use_external = _external_pin_verifier() is not None
    enrolled_users = None if use_external else _users_with_override_pin()

    candidates = frappe.get_all(
        "User",
        filters={"enabled": 1, "user_type": "System User"},
        fields=["name", "full_name"],
        order_by="full_name asc",
    )

    result: list[dict[str, str]] = []
    for row in candidates:
        uid = row.get("name")
        if not uid or uid.casefold() in hard_excluded:
            continue
        if enrolled_users is not None and uid.casefold() not in enrolled_users:
            continue
        rules = _matching_authorise_rules(
            uid, profile, privilege, percent=percent, amount=amount, privilege_group=privilege_group
        )
        if not rules:
            continue
        if uid.casefold() in owners and not _allows_check_owner_self_approval(privilege, rules, pct):
            continue
        result.append({"user": uid, "full_name": row.get("full_name") or _get_fullname(uid) or uid})
    return result


def _verify_approver_pin(approver: str, pin: Any, profile) -> tuple[bool, str | None]:
    verifier = _external_pin_verifier()
    if verifier is not None:
        if not isinstance(pin, str) or not pin.strip():
            return False, "POS-AUTH-PIN"
        try:
            outcome = verifier(approver, pin, profile.name)
        except Exception as err:
            text = str(err)
            for code in (
                "POS-AUTH-LOCKED",
                "POS-AUTH-NO-PIN",
                "POS-AUTH-NOT-ELIGIBLE",
                "POS-AUTH-SELF",
                "POS-AUTH-PIN",
            ):
                if code in text:
                    return False, code
            if "PIN_LOCKED" in text:
                return False, "POS-AUTH-LOCKED"
            if "PIN_NOT_SET" in text or "PIN_MUST_RESET" in text:
                return False, "POS-AUTH-NO-PIN"
            return False, "POS-AUTH-PIN"
        if isinstance(outcome, dict):
            if outcome.get("ok"):
                return True, None
            return False, outcome.get("code") or "POS-AUTH-PIN"
        return (True, None) if bool(outcome) else (False, "POS-AUTH-PIN")

    if not _has_override_pin(approver):
        return False, "POS-AUTH-NO-PIN"
    if not isinstance(pin, str) or not PIN_PATTERN.fullmatch(pin):
        return False, "POS-AUTH-PIN"
    try:
        _check_password(
            approver,
            pin,
            doctype=PIN_AUTH_DOCTYPE,
            fieldname=PIN_AUTH_FIELDNAME,
            delete_tracker_cache=False,
        )
        return True, None
    except frappe.AuthenticationError:
        return False, "POS-AUTH-PIN"


@frappe.whitelist(methods=["POST"])
def authorise(
    pos_profile: str,
    privilege: str,
    approver: str,
    pin: str,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any] | str,
) -> dict[str, Any]:
    """Verify an eligible manager's PIN and mint a single-use 120 s approval token."""
    _scrub_form_secrets()

    _validate_privilege(privilege)
    parsed_claim = _parse_claim(claim)
    claim_json = _canonical_claim_json(parsed_claim)
    claim_hash = hashlib.sha256(claim_json.encode("utf-8")).hexdigest()
    pct = _parse_optional_number(parsed_claim.get("percent"))
    amt = _parse_optional_number(parsed_claim.get("amount"))
    pg = cint(parsed_claim.get("privilege_group") or 0)

    requester = frappe.session.user
    if not requester or requester == "Guest":
        _refuse("POS-AUTH-NOT-ELIGIBLE", "Sign in to an active till session first.", frappe.PermissionError)

    if _is_non_interactive_or_agent_session(requester):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "Manager PIN approval is only available in an interactive till session.",
            frappe.PermissionError,
        )

    profile = _get_profile(pos_profile)
    if not profile or not _is_outlet_member(requester, profile):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "This till profile is unavailable for manager approval.",
            frappe.PermissionError,
        )

    # P1 #7: Enforce server-side reason validation BEFORE verifying PIN or minting tokens.
    reason = _validate_reason(profile.name, privilege, parsed_claim)

    approver_input = (approver or "").strip()
    canonical = frappe.db.exists("User", approver_input) if approver_input else None
    if not canonical:
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "Choose an eligible manager to approve this action.",
            frappe.PermissionError,
        )
    approver_user = str(canonical)

    # A manager acting alone uses may_perform, never self-approval via PIN.
    if approver_user.casefold() == requester.casefold():
        _write_log(
            requester=requester, approver=approver_user, privilege=privilege,
            pos_profile=profile.name, company=profile.company,
            reference_doctype=reference_doctype, reference_name=reference_name,
            claim_json=claim_json, reason=reason, outcome="Refused",
            refusal_code="POS-AUTH-SELF", isolated=True,
        )
        _refuse(
            "POS-AUTH-SELF",
            "You cannot approve your own request. Ask another manager.",
            frappe.PermissionError,
        )

    matching_rules = _matching_authorise_rules(
        approver_user, profile, privilege, percent=pct, amount=amt, privilege_group=pg
    )

    if approver_user.casefold() in _check_owners(reference_doctype, reference_name, profile=profile):
        if not matching_rules or not _allows_check_owner_self_approval(privilege, matching_rules, pct):
            _write_log(
                requester=requester, approver=approver_user, privilege=privilege,
                pos_profile=profile.name, company=profile.company,
                reference_doctype=reference_doctype, reference_name=reference_name,
                claim_json=claim_json, reason=reason, outcome="Refused",
                refusal_code="POS-AUTH-SELF", isolated=True,
            )
            _refuse(
                "POS-AUTH-SELF",
                "You cannot approve an override on your own check. Ask another manager.",
                frappe.PermissionError,
            )

    if not matching_rules:
        _write_log(
            requester=requester, approver=approver_user, privilege=privilege,
            pos_profile=profile.name, company=profile.company,
            reference_doctype=reference_doctype, reference_name=reference_name,
            claim_json=claim_json, reason=reason, outcome="Refused",
            refusal_code="POS-AUTH-NOT-ELIGIBLE", isolated=True,
        )
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "This person cannot approve this action at this till.",
            frappe.PermissionError,
        )

    # P1 #8: Per-session rate limit checked FIRST so a rogue cashier cannot lock out other managers.
    tracker, max_attempts, lock_minutes = _pin_tracker(approver_user, profile)
    sess_tracker, session_cap = _session_tracker(requester, profile, max_attempts, lock_minutes)
    if not sess_tracker.is_user_allowed() or _db_session_failed_pin_count(requester, profile.name, lock_minutes) >= session_cap:
        _write_log(
            requester=requester, approver=approver_user, privilege=privilege,
            pos_profile=profile.name, company=profile.company,
            reference_doctype=reference_doctype, reference_name=reference_name,
            claim_json=claim_json, reason=reason, outcome="Locked",
            refusal_code="POS-AUTH-LOCKED", isolated=True,
        )
        _refuse(
            "POS-AUTH-LOCKED",
            f"Too many failed approval attempts from this till session. Try again in {lock_minutes} minutes.",
            frappe.ValidationError,
        )

    # P1 #8: Per-(approver, pos_profile) lockout checked in BOTH Redis AND MariaDB.
    if not tracker.is_user_allowed() or _db_failed_pin_count(approver_user, profile.name, lock_minutes) >= max_attempts:
        _write_log(
            requester=requester, approver=approver_user, privilege=privilege,
            pos_profile=profile.name, company=profile.company,
            reference_doctype=reference_doctype, reference_name=reference_name,
            claim_json=claim_json, reason=reason, outcome="Locked",
            refusal_code="POS-AUTH-LOCKED", isolated=True,
        )
        _refuse(
            "POS-AUTH-LOCKED",
            f"Approval PIN is locked after too many wrong attempts. Try again in {lock_minutes} minutes.",
            frappe.ValidationError,
        )

    ok, code = _verify_approver_pin(approver_user, pin, profile)
    if not ok:
        if code == "POS-AUTH-NO-PIN":
            _write_log(
                requester=requester, approver=approver_user, privilege=privilege,
                pos_profile=profile.name, company=profile.company,
                reference_doctype=reference_doctype, reference_name=reference_name,
                claim_json=claim_json, reason=reason, outcome="Refused",
                refusal_code="POS-AUTH-NO-PIN", isolated=True,
            )
            _refuse(
                "POS-AUTH-NO-PIN",
                "This manager has not set an approval PIN yet.",
                frappe.ValidationError,
            )

        # P1 #9 (Hydrogen #11): Pass external verifier non-PIN refusal codes through without a PIN strike!
        if code == "POS-AUTH-NOT-ELIGIBLE":
            _write_log(
                requester=requester, approver=approver_user, privilege=privilege,
                pos_profile=profile.name, company=profile.company,
                reference_doctype=reference_doctype, reference_name=reference_name,
                claim_json=claim_json, reason=reason, outcome="Refused",
                refusal_code="POS-AUTH-NOT-ELIGIBLE", isolated=True,
            )
            _refuse(
                "POS-AUTH-NOT-ELIGIBLE",
                "This person cannot approve this action at this till.",
                frappe.PermissionError,
            )

        if code == "POS-AUTH-SELF":
            _write_log(
                requester=requester, approver=approver_user, privilege=privilege,
                pos_profile=profile.name, company=profile.company,
                reference_doctype=reference_doctype, reference_name=reference_name,
                claim_json=claim_json, reason=reason, outcome="Refused",
                refusal_code="POS-AUTH-SELF", isolated=True,
            )
            _refuse(
                "POS-AUTH-SELF",
                "You cannot approve this action yourself. Ask another manager.",
                frappe.PermissionError,
            )

        if code == "POS-AUTH-LOCKED":
            _write_log(
                requester=requester, approver=approver_user, privilege=privilege,
                pos_profile=profile.name, company=profile.company,
                reference_doctype=reference_doctype, reference_name=reference_name,
                claim_json=claim_json, reason=reason, outcome="Locked",
                refusal_code="POS-AUTH-LOCKED", isolated=True,
            )
            _refuse(
                "POS-AUTH-LOCKED",
                f"Approval PIN is locked after too many wrong attempts. Try again in {lock_minutes} minutes.",
                frappe.ValidationError,
            )

        if code and code != "POS-AUTH-PIN":
            _write_log(
                requester=requester, approver=approver_user, privilege=privilege,
                pos_profile=profile.name, company=profile.company,
                reference_doctype=reference_doctype, reference_name=reference_name,
                claim_json=claim_json, reason=reason, outcome="Refused",
                refusal_code=code, isolated=True,
            )
            _refuse(code, "Manager approval could not be verified.", frappe.ValidationError)

        # Genuine wrong PIN -> record Failed PIN durably first, then check both DB + Redis lockouts.
        _write_log(
            requester=requester, approver=approver_user, privilege=privilege,
            pos_profile=profile.name, company=profile.company,
            reference_doctype=reference_doctype, reference_name=reference_name,
            claim_json=claim_json, reason=reason, outcome="Failed PIN",
            refusal_code="POS-AUTH-PIN", isolated=True,
        )
        tracker.add_failure_attempt()
        sess_tracker.add_failure_attempt()

        approver_locked = (
            not tracker.is_user_allowed()
            or _db_failed_pin_count(approver_user, profile.name, lock_minutes) >= max_attempts
        )
        if approver_locked:
            _write_log(
                requester=requester, approver=approver_user, privilege=privilege,
                pos_profile=profile.name, company=profile.company,
                reference_doctype=reference_doctype, reference_name=reference_name,
                claim_json=claim_json, reason=reason, outcome="Locked",
                refusal_code="POS-AUTH-LOCKED", isolated=True,
            )
            _refuse(
                "POS-AUTH-LOCKED",
                f"Approval PIN is locked after too many wrong attempts. Try again in {lock_minutes} minutes.",
                frappe.ValidationError,
            )

        _refuse(
            "POS-AUTH-PIN",
            "That approval PIN did not match.",
            frappe.ValidationError,
        )

    tracker.add_success_attempt()

    token = secrets.token_urlsafe(32)
    token_hash = _token_digest(token)
    expires_at = now_datetime() + timedelta(seconds=TOKEN_TTL_SECONDS)

    _write_log(
        requester=requester,
        approver=approver_user,
        privilege=privilege,
        pos_profile=profile.name,
        company=profile.company,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        claim_json=claim_json,
        reason=reason,
        outcome="Approved",
        token_hash=token_hash,
        claim_hash=claim_hash,
        expires_at=expires_at,
        isolated=True,
    )

    return {
        "token": token,
        "approver": approver_user,
        "approver_name": _get_fullname(approver_user) or approver_user,
        "expires_in": TOKEN_TTL_SECONDS,
    }


def _read_approved_and_consumed(token_hash: str) -> tuple[Any, bool]:
    """Read Approved and Consumed log state from a fresh isolated DB snapshot."""
    if not _has_real_isolated_db():
        approved = frappe.db.get_value(
            "POS Authorisation Log",
            {"token_hash": token_hash, "outcome": "Approved"},
            [
                "name", "requester", "approver", "privilege", "pos_profile", "company",
                "reference_doctype", "reference_name", "claim_hash", "expires_at", "reason",
            ],
            as_dict=True,
        )
        already_consumed = bool(
            frappe.db.get_value("POS Authorisation Log", {"consumed_token_hash": token_hash}, "name")
        )
        return approved, already_consumed

    def _query(db):
        rows = db.sql(
            """
            SELECT `name`, `requester`, `approver`, `privilege`, `pos_profile`, `company`,
                   `reference_doctype`, `reference_name`, `claim_hash`, `expires_at`, `reason`
            FROM `tabPOS Authorisation Log`
            WHERE `token_hash` = %s AND `outcome` = 'Approved'
            LIMIT 1
            """,
            (token_hash,),
            as_dict=True,
        )
        approved_row = frappe._dict(rows[0]) if rows else None
        consumed_rows = db.sql(
            "SELECT `name` FROM `tabPOS Authorisation Log` WHERE `consumed_token_hash` = %s LIMIT 1",
            (token_hash,),
        )
        return approved_row, bool(consumed_rows)

    return _with_isolated_db(_query, commit=False)


def consume(
    token: str,
    privilege: str,
    pos_profile: str,
    reference_name: str,
    claim: dict[str, Any] | str,
):
    """Atomically consume a single-use approval token bound to session, check, and claim.

    Re-checks both approver and requester eligibility (enabled, outlet, role, rule, caps,
    clock-in, check-owner) at consumption time and commits the Consumed log row on an
    isolated DB connection so caller rollbacks never allow replay and caller work is never leaked.
    """
    _validate_privilege(privilege)
    current_user = frappe.session.user or ""
    if not current_user or current_user == "Guest" or _is_non_interactive_or_agent_session(current_user):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "Manager PIN approval cannot be used from a non-interactive, impersonated, or API session.",
            frappe.PermissionError,
        )

    if not isinstance(token, str) or not token.strip():
        _refuse(
            "POS-AUTH-EXPIRED",
            "This manager approval is missing or has expired. Ask a manager to approve again.",
            frappe.ValidationError,
        )

    token_hash = _token_digest(token.strip())
    parsed_claim = _parse_claim(claim)
    claim_json = _canonical_claim_json(parsed_claim)
    expected_claim_hash = hashlib.sha256(claim_json.encode("utf-8")).hexdigest()
    pct = _parse_optional_number(parsed_claim.get("percent"))
    amt = _parse_optional_number(parsed_claim.get("amount"))
    pg = cint(parsed_claim.get("privilege_group") or 0)

    approved, already_consumed = _read_approved_and_consumed(token_hash)
    if not approved:
        _refuse(
            "POS-AUTH-EXPIRED",
            "This manager approval is missing or has expired. Ask a manager to approve again.",
            frappe.ValidationError,
        )

    if already_consumed:
        _refuse(
            "POS-AUTH-EXPIRED",
            "This manager approval has already been used. Ask a manager to approve again.",
            frappe.ValidationError,
        )

    if approved.get("expires_at") and now_datetime() > get_datetime(approved.get("expires_at")):
        _refuse(
            "POS-AUTH-EXPIRED",
            "This manager approval has expired. Ask a manager to approve again.",
            frappe.ValidationError,
        )

    if (approved.get("requester") or "").casefold() != current_user.casefold():
        _refuse(
            "POS-AUTH-CHANGED",
            "This manager approval belongs to a different till session.",
            frappe.ValidationError,
        )

    if (
        approved.get("privilege") != privilege
        or approved.get("pos_profile") != pos_profile
        or (approved.get("reference_name") or "") != (reference_name or "")
    ):
        _refuse(
            "POS-AUTH-CHANGED",
            "The check or action changed after manager approval. Ask a manager to approve again.",
            frappe.ValidationError,
        )

    if not hmac.compare_digest(str(approved.get("claim_hash") or ""), expected_claim_hash):
        _refuse(
            "POS-AUTH-CHANGED",
            "The approved values changed after manager approval. Ask a manager to approve again.",
            frappe.ValidationError,
        )

    # P1 #6: Re-check profile, requester, AND approver (enabled, outlet, roles, rules, caps, clock-in, owner) NOW!
    profile = _get_profile(pos_profile)
    if not profile or not _is_outlet_member(current_user, profile):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "Your till session is no longer eligible to apply this approval.",
            frappe.PermissionError,
        )

    approver_user = approved.get("approver")
    if not approver_user or approver_user.casefold() == current_user.casefold():
        _refuse(
            "POS-AUTH-SELF",
            "You cannot approve your own request. Ask another manager.",
            frappe.PermissionError,
        )

    matching_rules = _matching_authorise_rules(
        approver_user, profile, privilege, percent=pct, amount=amt, privilege_group=pg
    )
    if not matching_rules:
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "The approving manager is no longer eligible for this action at this till.",
            frappe.PermissionError,
        )

    ref_doctype = approved.get("reference_doctype") or "POS Invoice"
    if approver_user.casefold() in _check_owners(ref_doctype, reference_name, profile=profile):
        if not _allows_check_owner_self_approval(privilege, matching_rules, pct):
            _refuse(
                "POS-AUTH-SELF",
                "The approving manager cannot approve an override on their own check.",
                frappe.PermissionError,
            )

    # P1 #7: Re-validate reason at consumption time.
    validated_reason = _validate_reason(profile.name, privilege, parsed_claim)

    duplicate_exc = getattr(frappe, "DuplicateEntryError", None)
    try:
        _write_log(
            requester=current_user,
            approver=approver_user,
            privilege=privilege,
            pos_profile=pos_profile,
            company=approved.get("company") or profile.company,
            reference_doctype=ref_doctype,
            reference_name=reference_name,
            claim_json=claim_json,
            reason=validated_reason or str(approved.get("reason") or ""),
            outcome="Consumed",
            token_hash=None,
            consumed_token_hash=token_hash,
            claim_hash=expected_claim_hash,
            isolated=True,
        )
    except Exception as err:
        is_dup = (
            isinstance(err, frappe.UniqueValidationError)
            or (duplicate_exc is not None and isinstance(err, duplicate_exc))
            or frappe.db.is_unique_key_violation(err)
            or (getattr(err, "args", None) and err.args[0] == 1062)
        )
        if is_dup:
            _refuse(
                "POS-AUTH-EXPIRED",
                "This manager approval has already been used. Ask a manager to approve again.",
                frappe.ValidationError,
            )
        raise

    return frappe._dict(
        approver=approver_user,
        percent=pct,
        amount=amt,
        claim=parsed_claim,
    )


def require(
    privilege: str,
    pos_profile: str,
    reference_name: str,
    claim: dict[str, Any] | str,
    token: str | None = None,
    percent: float | None = None,
    amount: float | None = None,
    privilege_group: int = 0,
):
    """Return None when caller may act alone, consumed approval with token, or raise POS-AUTH-REQUIRED."""
    _validate_privilege(privilege)
    if token:
        return consume(token, privilege, pos_profile, reference_name, claim)

    parsed_claim = _parse_claim(claim)
    eff_pct = percent if percent is not None else _parse_optional_number(parsed_claim.get("percent"))
    eff_amt = amount if amount is not None else _parse_optional_number(parsed_claim.get("amount"))
    eff_pg = privilege_group or cint(parsed_claim.get("privilege_group") or 0)

    if may_perform(
        frappe.session.user, pos_profile, privilege,
        percent=eff_pct, amount=eff_amt, privilege_group=eff_pg,
    ):
        # P1 #7: Even when a manager acts alone, enforce server-side reason validation.
        _validate_reason(pos_profile, privilege, parsed_claim)
        return None

    _refuse(
        "POS-AUTH-REQUIRED",
        "A manager must approve this action.",
        frappe.PermissionError,
    )


# --- PIN enrolment & manager clear ---


@frappe.whitelist(methods=["GET"])
def my_pin_status() -> dict[str, bool]:
    user = frappe.session.user
    if not user or user in ("Administrator", "Guest") or _is_non_interactive_or_agent_session(user):
        _refuse("POS-AUTH-NOT-ELIGIBLE", "Approval PINs are for named staff accounts in interactive sessions.", frappe.PermissionError)
    manager = _external_pin_manager()
    if manager is not None:
        return {
            **_external_pin_status(manager, user),
            "external_verifier": True,
            "can_manage_here": True,
        }
    if _external_pin_verifier() is not None:
        return {"has_pin": True, "external_verifier": True, "can_manage_here": False}
    return {
        "has_pin": _has_override_pin(user),
        "must_reset": False,
        "locked": False,
        "external_verifier": False,
        "can_manage_here": True,
    }


@frappe.whitelist(methods=["POST"])
def set_my_pin(password: str, pin: str) -> dict[str, Any]:
    """Enrol or update the signed-in user's own approval PIN after password re-entry.

    P0 #2: Never allows `AuthenticationError` (401) to escape and terminate the live till session;
    counts failed password checks durably in DB + Redis and raises `ValidationError` (417).
    """
    _scrub_form_secrets()
    user = frappe.session.user
    if not user or user in ("Administrator", "Guest"):
        _refuse("POS-AUTH-NOT-ELIGIBLE", "Approval PINs are for named staff accounts only.", frappe.PermissionError)
    if _is_non_interactive_or_agent_session(user):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "Approval PINs can only be set in an interactive till session.",
            frappe.PermissionError,
        )
    if cint(frappe.db.get_value("User", user, "enabled")) != 1:
        _refuse("POS-AUTH-NOT-ELIGIBLE", "This account is disabled.", frappe.PermissionError)
    manager = _external_pin_manager()
    if manager is None and _external_pin_verifier() is not None:
        _refuse_external_pin_management()
    if not isinstance(password, str) or not password:
        frappe.throw(frappe._("Enter your account password to confirm."), frappe.ValidationError)

    pw_tracker, pw_max, pw_lock_min = _password_tracker(user)
    if not pw_tracker.is_user_allowed() or _db_failed_password_count(user, pw_lock_min) >= pw_max:
        _refuse(
            "POS-AUTH-LOCKED",
            f"Too many wrong password attempts. Try again in {pw_lock_min} minutes.",
            frappe.ValidationError,
        )

    try:
        _check_password(user, password, delete_tracker_cache=False)
    except frappe.AuthenticationError:
        _write_log(
            requester=user,
            approver=user,
            privilege="pin_enrolment",
            pos_profile=None,
            company=None,
            reference_doctype="User",
            reference_name=user,
            claim_json="{}",
            reason="",
            outcome="Failed Password",
            refusal_code="POS-AUTH-PASSWORD",
            isolated=True,
        )
        pw_tracker.add_failure_attempt()
        if not pw_tracker.is_user_allowed() or _db_failed_password_count(user, pw_lock_min) >= pw_max:
            _refuse(
                "POS-AUTH-LOCKED",
                f"Too many wrong password attempts. Try again in {pw_lock_min} minutes.",
                frappe.ValidationError,
            )
        _refuse(
            "POS-AUTH-PASSWORD",
            "Your current password did not match.",
            frappe.ValidationError,
        )

    if not isinstance(pin, str) or not PIN_PATTERN.fullmatch(pin):
        frappe.throw(
            frappe._("Enter an approval PIN of 4 to 8 digits."),
            frappe.ValidationError,
        )

    if manager is not None:
        manager("set", user=user, pin=pin)
        log_pin_reset = True
    else:
        log_pin_reset = (
            _has_override_pin(user)
            or _db_failed_password_count(user, pw_lock_min) > 0
            or bool(frappe.db.exists("POS Authorisation Log", {"approver": user, "outcome": "Failed PIN"}))
        )
        _update_password(
            user,
            pin,
            doctype=PIN_AUTH_DOCTYPE,
            fieldname=PIN_AUTH_FIELDNAME,
            logout_all_sessions=False,
        )
    pw_tracker.add_success_attempt()
    if log_pin_reset:
        _write_log(
            requester=user,
            approver=user,
            privilege="pin_enrolment",
            pos_profile=None,
            company=None,
            reference_doctype="User",
            reference_name=user,
            claim_json="{}",
            reason="",
            outcome="PIN Reset",
            isolated=True,
        )
    _add_authentication_log(f"Approval PIN updated for {user}", user)
    return {"ok": True, "has_pin": True}


def _can_manage_pin_for(caller: str, target: str, pos_profile: str | None = None) -> bool:
    if not caller or caller == "Guest":
        return False
    if caller.casefold() == target.casefold():
        return True
    if pos_profile:
        profile = _get_profile(pos_profile)
        if not profile:
            return False
        if not frappe.has_permission("POS Profile", "write", profile):
            return False
        return _is_outlet_member(target, profile)
    return bool(frappe.has_permission("POS Privilege Rule", "write"))


@frappe.whitelist(methods=["POST"])
def clear_user_pin(user: str, pos_profile: str | None = None) -> dict[str, Any]:
    """Allow a manager (or the owner) to clear a staff member's approval PIN. Never reads or sets."""
    caller = frappe.session.user
    if not caller or caller == "Guest":
        _refuse("POS-AUTH-NOT-ELIGIBLE", "Sign in to clear an approval PIN.", frappe.PermissionError)
    if _is_non_interactive_or_agent_session(caller):
        _refuse("POS-AUTH-NOT-ELIGIBLE", "PIN management requires an interactive session.", frappe.PermissionError)

    canonical = frappe.db.exists("User", (user or "").strip()) if user else None
    if not canonical or str(canonical) in ("Administrator", "Guest"):
        _refuse("POS-AUTH-NOT-ELIGIBLE", "Select a valid staff user.", frappe.PermissionError)
    target = str(canonical)

    if not _can_manage_pin_for(caller, target, pos_profile=pos_profile):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "Only a manager for this till can clear another user's approval PIN.",
            frappe.PermissionError,
        )

    manager = _external_pin_manager()
    if manager is not None:
        manager("clear", user=target, caller=caller, pos_profile=pos_profile)
    else:
        if _external_pin_verifier() is not None:
            _refuse_external_pin_management()
        _clear_override_pin(target)

    if pos_profile:
        profile = _get_profile(pos_profile)
        if profile:
            tracker, _, _ = _pin_tracker(target, profile)
            tracker.add_success_attempt()
    pw_tracker, _, _ = _password_tracker(target)
    pw_tracker.add_success_attempt()
    _write_log(
        requester=caller,
        approver=target,
        privilege="pin_enrolment",
        pos_profile=pos_profile or None,
        company=None,
        reference_doctype="User",
        reference_name=target,
        claim_json="{}",
        reason="",
        outcome="PIN Reset",
        isolated=True,
    )
    _add_authentication_log(f"Approval PIN cleared for {target} by {caller}", caller)
    if manager is not None:
        return {"ok": True, "has_pin": False, "must_reset": True}
    return {"ok": True, "user": target, "has_pin": False}


@frappe.whitelist(methods=["GET"])
def list_pin_users(pos_profile: str) -> list[dict[str, Any]]:
    """List outlet staff and whether each has an approval PIN set (for manager clear UI)."""
    caller = frappe.session.user
    if not caller or caller == "Guest" or _is_non_interactive_or_agent_session(caller):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "PIN management requires an interactive session.",
            frappe.PermissionError,
        )
    profile = _get_profile(pos_profile)
    if not profile or not frappe.has_permission("POS Profile", "write", profile):
        _refuse(
            "POS-AUTH-NOT-ELIGIBLE",
            "Only a manager for this till can view staff approval PIN status.",
            frappe.PermissionError,
        )
    manager = _external_pin_manager()
    enrolled_users = _users_with_override_pin() if manager is None else set()
    users = frappe.get_all(
        "User",
        filters={"enabled": 1, "user_type": "System User"},
        fields=["name", "full_name"],
        order_by="full_name asc",
    )
    out = []
    for row in users:
        uid = row.get("name")
        if not uid or uid in ("Administrator", "Guest"):
            continue
        if _is_outlet_member(uid, profile):
            status = (
                _external_pin_status(manager, uid) if manager is not None
                else {"has_pin": uid.casefold() in enrolled_users}
            )
            out.append({
                "user": uid,
                "full_name": row.get("full_name") or _get_fullname(uid) or uid,
                **status,
            })
    return out
