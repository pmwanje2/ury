"""Temporary B2a contract adapter stub for manager override PIN authorisation.

Removed immediately once feat/pos-manager-authorisation (B2a: ury/ury_pos/override.py)
is merged into this branch. Implements the exact interface specified in
pos-override-contract.md so B2b can be built and verified independently without
creating or editing B2a's file paths.
"""

import hashlib
import hmac
import json
import secrets

import frappe
from frappe import _
from frappe.utils import cint, flt

TOKEN_TTL_SECONDS = 120
LOCKOUT_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

PRIVILEGES = {
    "discount": ("Apply discount", True, ("URY Manager",), ("URY Manager",)),
    "complimentary": ("Complimentary check", True, (), ("URY Manager",)),
}


def _canonical_claim_hash(claim):
    payload = claim if isinstance(claim, dict) else (frappe.parse_json(claim) or {})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _refuse(code, message, exc=None):
    frappe.throw(f"{message} [{code}]", exc or frappe.PermissionError)


def _token_cache_key(token):
    digest = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
    return f"pos_override_token:{digest}"


def _lockout_cache_key(user):
    return f"pos_override_lock:{user}"


def _verify_pin(approver, pin):
    pin_str = str(pin or "").strip()
    if not pin_str:
        return False

    hooks = frappe.get_hooks("pos_override_pin_verifier") or []
    for path in hooks:
        verifier = frappe.get_attr(path)
        if callable(verifier):
            return bool(verifier(approver, pin_str))

    stored_hash = frappe.db.get_value("User", approver, "custom_override_pin_hash")
    if not stored_hash:
        _refuse("POS-AUTH-NO-PIN", _("This manager has not set an approval PIN yet."))

    secret = str(getattr(frappe.local, "conf", None) and frappe.local.conf.get("encryption_key") or "ury-override")
    expected = hmac.new(secret.encode("utf-8"), f"{approver}:{pin_str}".encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(stored_hash), expected)


_POLICY_FN = None
_LIMIT_FN = None


def _bind_discount_helpers(policy_fn, limit_fn):
    global _POLICY_FN, _LIMIT_FN
    _POLICY_FN = policy_fn
    _LIMIT_FN = limit_fn


def _discount_helpers():
    if _POLICY_FN is not None and _LIMIT_FN is not None:
        return _POLICY_FN, _LIMIT_FN
    from ury.ury_pos.discount import company_discount_policy, user_discount_limit
    return company_discount_policy, user_discount_limit


def _is_eligible_approver(approver, requester, pos_profile, privilege, reference_name=None, percent=None, amount=None):
    if not approver or approver == "Guest":
        return False
    if approver == requester:
        return False
    if not cint(frappe.db.get_value("User", approver, "enabled")):
        return False
    if reference_name:
        inv_owner = frappe.db.get_value("POS Invoice", reference_name, "owner")
        if inv_owner and approver == inv_owner:
            return False

    company_discount_policy, user_discount_limit = _discount_helpers()

    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    company = profile.get("company")
    roles = set(frappe.get_roles(approver) or [])

    if privilege in ("discount", "complimentary"):
        policy = company_discount_policy(company)
        limit = user_discount_limit(approver, company)
        req_pct = flt(percent) if percent is not None else 0.0
        approver_role = policy.get("approver_role")
        if approver_role and approver_role in roles:
            return True
        if limit > 0 and limit >= req_pct:
            return True
        if not policy.get("role_limits") and not approver_role and "URY Manager" in roles:
            return True
        return False

    return "URY Manager" in roles


def may_perform(user, pos_profile, privilege, percent=None, amount=None):
    if privilege == "complimentary":
        return False
    if privilege == "discount":
        _, user_discount_limit = _discount_helpers()

        profile = frappe.get_cached_doc("POS Profile", pos_profile)
        limit = user_discount_limit(user, profile.get("company"))
        req_pct = flt(percent) if percent is not None else 0.0
        if req_pct >= 100.0:
            return False
        return req_pct <= limit and req_pct >= 0
    return False


@frappe.whitelist()
def list_approvers(pos_profile, privilege, reference_name=None, percent=None, amount=None):
    requester = frappe.session.user
    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    candidates = []
    seen = set()
    for row in profile.get("applicable_for_users") or []:
        u = row.get("user") if isinstance(row, dict) else getattr(row, "user", None)
        if u and u not in seen:
            seen.add(u)
            candidates.append(u)
    result = []
    for user in candidates:
        if _is_eligible_approver(user, requester, pos_profile, privilege, reference_name, percent, amount):
            full_name = frappe.db.get_value("User", user, "full_name") or user
            result.append({"user": user, "full_name": full_name})
    return result


@frappe.whitelist()
def authorise(pos_profile, privilege, approver, pin, reference_doctype, reference_name, claim):
    requester = frappe.session.user
    if not approver or approver == requester:
        _refuse("POS-AUTH-SELF", _("You cannot approve your own request."))

    parsed_claim = frappe.parse_json(claim) if isinstance(claim, str) else (claim or {})
    percent = parsed_claim.get("percent") if isinstance(parsed_claim, dict) else None
    amount = parsed_claim.get("amount") if isinstance(parsed_claim, dict) else None

    if not _is_eligible_approver(approver, requester, pos_profile, privilege, reference_name, percent, amount):
        _refuse("POS-AUTH-NOT-ELIGIBLE", _("This person is not eligible to approve this action."))

    cache = frappe.cache()
    lock_key = _lockout_cache_key(approver)
    attempts = cint(cache.get_value(lock_key) or 0)
    if attempts >= LOCKOUT_ATTEMPTS:
        _refuse("POS-AUTH-LOCKED", _("Approval PIN is temporarily locked after too many failed tries."))

    if not _verify_pin(approver, pin):
        cache.set_value(lock_key, attempts + 1, expires_in_sec=LOCKOUT_MINUTES * 60)
        if attempts + 1 >= LOCKOUT_ATTEMPTS:
            _refuse("POS-AUTH-LOCKED", _("Approval PIN is temporarily locked after too many failed tries."))
        _refuse("POS-AUTH-PIN", _("Incorrect approval PIN."))

    cache.delete_value(lock_key)
    token = secrets.token_urlsafe(32)
    record = {
        "requester": requester,
        "approver": approver,
        "pos_profile": pos_profile,
        "privilege": privilege,
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "claim": parsed_claim,
        "claim_hash": _canonical_claim_hash(parsed_claim),
        "percent": percent,
        "amount": amount,
    }
    cache.set_value(_token_cache_key(token), json.dumps(record), expires_in_sec=TOKEN_TTL_SECONDS)
    approver_name = frappe.db.get_value("User", approver, "full_name") or approver
    return {
        "token": token,
        "approver": approver,
        "approver_name": approver_name,
        "expires_in": TOKEN_TTL_SECONDS,
    }


def consume(token, privilege, pos_profile, reference_name, claim):
    if not token or not isinstance(token, str):
        _refuse("POS-AUTH-REQUIRED", _("Manager approval is required."))

    cache = frappe.cache()
    key = _token_cache_key(token)
    raw = cache.get_value(key)
    if not raw:
        _refuse("POS-AUTH-EXPIRED", _("Manager approval has expired or was already used."))

    record = json.loads(raw) if isinstance(raw, str) else raw
    expected_hash = _canonical_claim_hash(claim)

    if (
        record.get("requester") != frappe.session.user
        or record.get("privilege") != privilege
        or record.get("pos_profile") != pos_profile
        or record.get("reference_name") != reference_name
        or record.get("claim_hash") != expected_hash
    ):
        _refuse("POS-AUTH-CHANGED", _("The discount changed after approval. Please approve again."))

    # Single-use removal once validated so replays can never succeed
    cache.delete_value(key)

    return frappe._dict(
        approver=record["approver"],
        percent=record.get("percent"),
        amount=record.get("amount"),
        claim=record.get("claim"),
    )


def require(privilege, pos_profile, reference_name, claim, token=None, percent=None, amount=None):
    if token:
        return consume(token, privilege, pos_profile, reference_name, claim)
    if may_perform(frappe.session.user, pos_profile, privilege, percent=percent, amount=amount):
        return None
    _refuse("POS-AUTH-REQUIRED", _("Manager approval is required for this discount."))
