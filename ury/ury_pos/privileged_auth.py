"""Shared privilege-check adapter over B2a's `ury.ury_pos.override`.

Never edits B2a's files. Delegates directly to `ury.ury_pos.override` when
present (once B2a is merged) and keeps a contract-identical fallback until
then. Also enforces:
- Fail-closed refusal on offline replays (`POS-OFFLINE-PRIVILEGED-REFUSED`)
- Active `POS Reason` validation (`POS-REASON-REQUIRED` / `INVALID` / `NOTE-REQUIRED`)
- Legacy fallback when a company has zero `POS Privilege Rule` rows for a
  privilege (so existing outlets never lose access before configuring rules)
- Mandatory audit logging (`POS Authorisation Log`) on every privileged use,
  including self-performed manager actions
- EFRIS fiscalised-document guard (`EFRIS_FISCALISED_USE_RETURN`)
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

DEFAULT_MANAGER_ROLES = frozenset({"URY Manager", "URY Admin"})


def _now_dt():
    fn = getattr(getattr(frappe, "utils", None), "now_datetime", None)
    if callable(fn):
        return fn()
    import datetime as _dt

    return _dt.datetime.now()


def _code(code: str, message: str) -> str:
    """Format canonical refusal code (`[POS-AUTH-REQUIRED] ...`) without doubling `POS-`."""
    return f"[{code}] {message}" if message else f"[{code}]"

try:
    from ury.ury_pos import override as _IMPORTED_OVERRIDE
except (ModuleNotFoundError, ImportError):
    _IMPORTED_OVERRIDE = None


def _get_override_module():
    mod = sys.modules.get("ury.ury_pos.override")
    if mod is not None:
        return mod
    if _IMPORTED_OVERRIDE is not None:
        return _IMPORTED_OVERRIDE
    try:
        return importlib.import_module("ury.ury_pos.override")
    except (ModuleNotFoundError, ImportError):
        return None


def _canonical_json(claim: dict[str, Any] | None) -> str:
    return json.dumps(claim or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def assert_not_offline_replay(
    *,
    append: Any = 0,
    captured_at: Any = None,
    offline_printed: Any = 0,
) -> None:
    """Refuse every privileged action during offline capture or offline outbox replay."""
    flags = getattr(frappe, "flags", None)
    try:
        flag_offline = getattr(flags, "ury_offline_replay", False) is True
    except RuntimeError:
        flag_offline = False
    is_offline = bool(
        flag_offline
        or cint(append) == 1
        or captured_at is not None
        or cint(offline_printed) == 1
    )
    if is_offline:
        frappe.throw(
            _code(
                "POS-OFFLINE-PRIVILEGED-REFUSED",
                _("This privileged action requires an online connection and cannot be replayed offline."),
            ),
            frappe.PermissionError,
        )


def _doc_get(doc, key: str, default: Any = None) -> Any:
    if doc is None:
        return default
    getter = getattr(doc, "get", None)
    if callable(getter):
        return getter(key, default)
    return getattr(doc, key, default)


def is_efris_fiscalised(doc) -> bool:
    """True when the invoice has been fiscalised with EFRIS (mirrors efris-ug@3)."""
    if not doc:
        return False
    status = (_doc_get(doc, "efris_status") or "").strip()
    if _doc_get(doc, "efris_irn") or (status and status != "Cancelled"):
        return True
    name = _doc_get(doc, "name")
    if name and _table_exists("E Invoice"):
        ref = frappe.db.get_value(
            "E Invoice",
            {"invoice": name},
            ["irn", "credit_note_application_ref_no"],
            as_dict=True,
        )
        if ref and (_doc_get(ref, "irn") or _doc_get(ref, "credit_note_application_ref_no")):
            return True
    return False


def assert_not_efris_fiscalised(doc) -> None:
    if is_efris_fiscalised(doc):
        frappe.throw(
            _code(
                "EFRIS_FISCALISED_USE_RETURN",
                _("This bill was sent to EFRIS and cannot be modified. Correct it with a return."),
            ),
            frappe.ValidationError,
        )


def _reason_applies_to_privilege(applies_to: str | None, privilege: str) -> bool:
    raw = (applies_to or "").strip()
    if not raw:
        return True
    tokens = [
        part.strip()
        for line in raw.splitlines()
        for part in line.split(",")
        if part.strip()
    ]
    return privilege in tokens


def _table_exists(doctype: str) -> bool:
    """True only when the DB genuinely reports the table exists (safe under bare MagicMock)."""
    db = getattr(frappe, "db", None)
    fn = getattr(db, "table_exists", None)
    if not callable(fn):
        return False
    res = fn(doctype)
    return res is True or res == 1


def _is_valid_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def list_active_reasons(privilege: str) -> list[dict[str, Any]]:
    """Return active `POS Reason` rows applicable to `privilege`."""
    if not _table_exists("POS Reason"):
        return []
    rows = frappe.get_all(
        "POS Reason",
        filters={"disabled": 0},
        fields=["code", "description", "applies_to", "requires_note", "sort_order"],
        order_by="sort_order asc, code asc",
    )
    return [
        {
            "code": r.get("code"),
            "description": r.get("description") or r.get("code"),
            "requires_note": cint(r.get("requires_note")),
        }
        for r in (rows or [])
        if _reason_applies_to_privilege(r.get("applies_to"), privilege)
    ]


def requires_reason_for(pos_profile: str | None, privilege: str) -> bool:
    override = _get_override_module()
    if override is not None and _is_valid_str(pos_profile) and hasattr(override, "requires_reason"):
        try:
            return bool(override.requires_reason(pos_profile, privilege))
        except Exception:
            pass
    return privilege in {
        "price_override",
        "complimentary",
        "tax_exempt",
        "service_charge_waive",
        "tip_adjust",
        "reprint_bill",
        "reprint_kot",
        "reservation_cancel",
    }


def validate_pos_reason(
    privilege: str,
    pos_profile: str | None,
    reason: str | None,
    reason_note: str | None = None,
    *,
    required: bool | None = None,
) -> tuple[str, str]:
    """Validate and normalize (reason_code_or_text, trimmed_note) against `POS Reason`."""
    must_have = requires_reason_for(pos_profile, privilege) if required is None else bool(required)
    cleaned = (reason or "").strip()
    note = (reason_note or "").strip()

    if not cleaned:
        if must_have:
            frappe.throw(
                _code("POS-REASON-REQUIRED", _("Select a reason for this action.")),
                frappe.ValidationError,
            )
        return "", note

    if _table_exists("POS Reason"):
        all_reasons = frappe.get_all(
            "POS Reason",
            fields=["code", "description", "applies_to", "requires_note", "disabled"],
        )
        matching_table = [
            r for r in (all_reasons or [])
            if cint(r.get("disabled")) == 0 and _reason_applies_to_privilege(r.get("applies_to"), privilege)
        ]
        # Match exact code or description, or ManagerApproval's "Description: note" / "Other: note" format
        found = None
        extracted_note = ""
        for r in all_reasons or []:
            if r.get("code") == cleaned or r.get("description") == cleaned:
                found = r
                break
        if found is None and ":" in cleaned:
            prefix, suffix = cleaned.split(":", 1)
            prefix_cf = prefix.strip().casefold()
            for r in all_reasons or []:
                code_cf = str(r.get("code") or "").strip().casefold()
                desc_cf = str(r.get("description") or "").strip().casefold()
                if prefix_cf in (code_cf, desc_cf) or (prefix_cf == "other" and code_cf == "other"):
                    found = r
                    extracted_note = suffix.strip()
                    break

        if found is not None:
            if cint(found.get("disabled")) == 1 or not _reason_applies_to_privilege(found.get("applies_to"), privilege):
                frappe.throw(
                    _code("POS-REASON-INVALID", _("Select an active reason configured for this action.")),
                    frappe.ValidationError,
                )
            effective_note = note or extracted_note
            if cint(found.get("requires_note")) == 1 and len(effective_note) < 3:
                frappe.throw(
                    _code(
                        "POS-REASON-NOTE-REQUIRED",
                        _("Enter a note (at least 3 characters) for the selected reason."),
                    ),
                    frappe.ValidationError,
                )
            return str(found.get("code") or cleaned), effective_note

        if matching_table:
            frappe.throw(
                _code("POS-REASON-INVALID", _("Select an active reason configured for this action.")),
                frappe.ValidationError,
            )

    if len(cleaned) < 3:
        frappe.throw(
            _code("POS-REASON-REQUIRED", _("Enter a reason of at least 3 characters.")),
            frappe.ValidationError,
        )
    return cleaned, note


def has_privilege_rules(company: str | None, privilege: str) -> bool:
    if not _is_valid_str(company) or not _table_exists("POS Privilege Rule"):
        return False
    rows = frappe.get_all(
        "POS Privilege Rule",
        filters={"company": company, "privilege": privilege},
        fields=["name"],
        limit=1,
    )
    return bool(rows)


def mark_verified_approval(doc, privilege: str, approver: str | None = None) -> None:
    """Attach an in-process verified-approval proof via B2a `override` when available."""
    override = _get_override_module()
    if override is not None and hasattr(override, "mark_verified_approval"):
        approval_obj = type("_Approval", (), {"approver": approver})() if approver else None
        override.mark_verified_approval(doc, privilege, approval=approval_obj)
    else:
        flags = getattr(doc, "flags", None)
        if flags is not None:
            setattr(flags, f"pos_override_verified_{privilege}", (privilege, str(doc.get("name") or ""), approver))


def has_verified_approval(doc, privilege: str) -> bool:
    """True only when `mark_verified_approval` attached a matching proof in-process."""
    override = _get_override_module()
    if override is not None and hasattr(override, "has_verified_approval"):
        if override.has_verified_approval(doc, privilege):
            return True
    flags = getattr(doc, "flags", None)
    marker = getattr(flags, f"pos_override_verified_{privilege}", None) if flags is not None else None
    return isinstance(marker, tuple) and len(marker) >= 2 and marker[0] == privilege and marker[1] == str(doc.get("name") or "")


def _record_self_performed_log(
    *,
    privilege: str,
    pos_profile: str | None,
    company: str | None,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any],
    reason: str,
) -> None:
    """Write a `POS Authorisation Log` entry for a self-performed privileged action."""
    if not _table_exists("POS Authorisation Log"):
        return
    user = getattr(getattr(frappe, "session", None), "user", None)
    claim_json = _canonical_json(claim)
    claim_hash = hashlib.sha256(claim_json.encode("utf-8")).hexdigest()
    doc = frappe.get_doc(
        {
            "doctype": "POS Authorisation Log",
            "requester": user,
            "approver": user,
            "privilege": privilege,
            "pos_profile": pos_profile,
            "company": company,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "claim_values": claim_json,
            "reason": (reason or "").strip()[:500],
            "outcome": "Approved",
            "refusal_code": "",
            "event_time": _now_dt(),
            "claim_hash": claim_hash,
        }
    )
    doc.flags.pos_override_server_insert = True
    doc.insert(ignore_permissions=True)


def _profile_company(pos_profile: str | None) -> str | None:
    if not _is_valid_str(pos_profile):
        return None
    val = frappe.db.get_value("POS Profile", pos_profile, "company")
    return val if _is_valid_str(val) else None


def _fallback_allows(user: str, fallback_roles: set[str] | frozenset[str] | None) -> bool:
    if not user or user == "Guest":
        return False
    roles = set(frappe.get_roles(user))
    target = set(fallback_roles) if fallback_roles is not None else set(DEFAULT_MANAGER_ROLES)
    return bool(roles & target)


def _check_privilege_group(
    company: str | None,
    pos_profile: str | None,
    privilege: str,
    user: str,
    required_group: int,
) -> bool:
    """Verify the acting user holds a rule with privilege_group == 0 or >= required_group."""
    if required_group <= 0:
        return True
    if not _is_valid_str(company) or not _table_exists("POS Privilege Rule"):
        return _fallback_allows(user, DEFAULT_MANAGER_ROLES)
    roles = set(frappe.get_roles(user))
    if not roles:
        return False
    rows = frappe.get_all(
        "POS Privilege Rule",
        filters={
            "company": company,
            "privilege": privilege,
            "role": ["in", sorted(roles)],
            "pos_profile": ["in", [pos_profile, "", None]],
        },
        fields=["pos_profile", "role", "may_perform", "privilege_group"],
    )
    if not rows:
        return False
    resolved: dict[str, dict] = {}
    for row in rows:
        role = row.get("role")
        if role not in resolved or (row.get("pos_profile") and not resolved[role].get("pos_profile")):
            resolved[role] = row
    for rule in resolved.values():
        if cint(rule.get("may_perform")) != 1:
            continue
        grp = cint(rule.get("privilege_group"))
        if grp == 0 or grp >= required_group:
            return True
    return False


def _attach_auth_challenge(
    *,
    privilege: str,
    pos_profile: str | None,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any],
    amount: float | None = None,
    percent: float | None = None,
) -> None:
    """Attach the exact server-computed claim to frappe.local.response for ManagerApproval parity."""
    local = getattr(frappe, "local", None)
    resp = getattr(local, "response", None) if local is not None else None
    if isinstance(resp, dict):
        resp["pos_auth_challenge"] = {
            "privilege": privilege,
            "pos_profile": pos_profile or "",
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "claim": claim,
            "amount": amount,
            "percent": percent,
        }


def authorize_or_perform(
    *,
    privilege: str,
    pos_profile: str | None,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any],
    token: str | None = None,
    percent: float | None = None,
    amount: float | None = None,
    reason: str | None = None,
    reason_note: str | None = None,
    require_reason: bool | None = None,
    fallback_roles: set[str] | frozenset[str] | None = None,
    always_second_person: bool = False,
    force_pin: bool = False,
    required_privilege_group: int = 0,
    allow_fallback_predicate: bool | None = None,
    company: str | None = None,
    deny_message: str | None = None,
    skip_profile_company_lookup: bool = False,
) -> dict[str, Any]:
    """Enforce privilege rules, PIN token consumption, reason validation, and audit logging.

    Returns ``{"approver": str, "via_pin": bool, "reason": str, "reason_note": str}``.
    """
    assert_not_offline_replay()
    challenge_kwargs = {
        "privilege": privilege,
        "pos_profile": pos_profile,
        "reference_doctype": reference_doctype,
        "reference_name": reference_name,
        "claim": claim,
        "amount": amount,
        "percent": percent,
    }
    try:
        validated_reason, validated_note = validate_pos_reason(
            privilege,
            pos_profile,
            reason,
            reason_note,
            required=require_reason,
        )
    except Exception:
        if not token:
            _attach_auth_challenge(**challenge_kwargs)
        raise

    user = getattr(getattr(frappe, "session", None), "user", None)
    resolved_company = company if _is_valid_str(company) else (
        None if skip_profile_company_lookup else _profile_company(pos_profile)
    )
    override = _get_override_module()
    auth_denied_msg = deny_message or _("A manager must approve this action.")

    # Path 1: Manager approval token supplied -> consume atomically through B2a.
    if token:
        if override is None:
            frappe.throw(
                _code(
                    "POS-AUTH-NOT-ELIGIBLE",
                    _("Manager authorisation service is unavailable on this site."),
                ),
                frappe.PermissionError,
            )
        raw_reason = (reason or "").strip()
        try:
            consumed = override.consume(token, privilege, pos_profile, reference_name, claim)
        except Exception as err:
            # B2a's ManagerApproval.tsx appends `reason = resolvedReasonText` to finalClaim
            # whenever its built-in reason picker is shown. Because override.consume rejects
            # POS-AUTH-CHANGED BEFORE inserting the Consumed log row, check the exact
            # claim+{reason: raw_reason} form (never loosening any non-reason field).
            if raw_reason and "POS-AUTH-CHANGED" in str(err) and "reason" not in claim:
                consumed = override.consume(
                    token,
                    privilege,
                    pos_profile,
                    reference_name,
                    {**claim, "reason": raw_reason},
                )
            else:
                raise
        return {
            "approver": consumed.get("approver"),
            "via_pin": True,
            "reason": validated_reason,
            "reason_note": validated_note,
        }

    # Path 2: Actions that ALWAYS require a second person (e.g. full comp, zeroing a priced item)
    # or explicitly escalated conditions (e.g. price below cost, over-HALO, re-split cap).
    if always_second_person or force_pin:
        _attach_auth_challenge(**challenge_kwargs)
        frappe.throw(
            _code("POS-AUTH-REQUIRED", auth_denied_msg),
            frappe.PermissionError,
        )

    # Path 3: Company has explicit POS Privilege Rule rows -> B2a rules govern.
    if has_privilege_rules(resolved_company, privilege):
        if override is not None:
            if required_privilege_group > 0 and not _check_privilege_group(
                resolved_company, pos_profile, privilege, user, required_privilege_group
            ):
                _attach_auth_challenge(**challenge_kwargs)
                frappe.throw(
                    _code("POS-AUTH-REQUIRED", auth_denied_msg),
                    frappe.PermissionError,
                )
            try:
                override.require(
                    privilege,
                    pos_profile,
                    reference_name,
                    claim,
                    token=None,
                    percent=percent,
                    amount=amount,
                )
            except frappe.PermissionError:
                _attach_auth_challenge(**challenge_kwargs)
                if deny_message:
                    frappe.throw(
                        _code("POS-AUTH-REQUIRED", auth_denied_msg),
                        frappe.PermissionError,
                    )
                raise
            _record_self_performed_log(
                privilege=privilege,
                pos_profile=pos_profile,
                company=resolved_company,
                reference_doctype=reference_doctype,
                reference_name=reference_name,
                claim=claim,
                reason=validated_reason,
            )
            return {
                "approver": user,
                "via_pin": False,
                "reason": validated_reason,
                "reason_note": validated_note,
            }

    # Path 4: No POS Privilege Rule rows configured yet -> keep the till usable
    # with today's outlet/manager roles; everyone else needs a manager PIN.
    allowed = (
        bool(allow_fallback_predicate)
        if allow_fallback_predicate is not None
        else _fallback_allows(user, fallback_roles)
    )
    if not allowed:
        _attach_auth_challenge(**challenge_kwargs)
        frappe.throw(
            _code("POS-AUTH-REQUIRED", auth_denied_msg),
            frappe.PermissionError,
        )

    _record_self_performed_log(
        privilege=privilege,
        pos_profile=pos_profile,
        company=resolved_company,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        claim=claim,
        reason=validated_reason,
    )
    return {
        "approver": user,
        "via_pin": False,
        "reason": validated_reason,
        "reason_note": validated_note,
    }


@frappe.whitelist(methods=["GET"])
def get_pos_reasons(privilege: str) -> list[dict[str, Any]]:
    """Return active configured POS Reason rows for the given privilege code."""
    return list_active_reasons(privilege)
