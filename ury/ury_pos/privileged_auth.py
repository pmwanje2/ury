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
from frappe.utils import cint, flt, now_datetime

from ury.brand import coded_message

DEFAULT_MANAGER_ROLES = frozenset({"URY Manager", "URY Admin"})

try:
    from ury.ury_pos import override as _IMPORTED_OVERRIDE
except ModuleNotFoundError:
    _IMPORTED_OVERRIDE = None


def _get_override_module():
    mod = sys.modules.get("ury.ury_pos.override")
    if mod is not None:
        return mod
    if _IMPORTED_OVERRIDE is not None:
        return _IMPORTED_OVERRIDE
    try:
        return importlib.import_module("ury.ury_pos.override")
    except ModuleNotFoundError:
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
    is_offline = bool(
        (flags and getattr(flags, "ury_offline_replay", False))
        or cint(append) == 1
        or captured_at is not None
        or cint(offline_printed) == 1
    )
    if is_offline:
        frappe.throw(
            coded_message(
                "POS-OFFLINE-PRIVILEGED-REFUSED",
                _("This privileged action requires an online connection and cannot be replayed offline."),
            ),
            frappe.PermissionError,
        )


def is_efris_fiscalised(doc) -> bool:
    """True when the invoice has been fiscalised with EFRIS (mirrors efris-ug@3)."""
    if not doc:
        return False
    status = (doc.get("efris_status") or "").strip()
    if doc.get("efris_irn") or (status and status != "Cancelled"):
        return True
    name = doc.get("name")
    if name and frappe.db.table_exists("E Invoice"):
        ref = frappe.db.get_value(
            "E Invoice",
            {"invoice": name},
            ["irn", "credit_note_application_ref_no"],
            as_dict=True,
        )
        if ref and (ref.get("irn") or ref.get("credit_note_application_ref_no")):
            return True
    return False


def assert_not_efris_fiscalised(doc) -> None:
    if is_efris_fiscalised(doc):
        frappe.throw(
            coded_message(
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


def list_active_reasons(privilege: str) -> list[dict[str, Any]]:
    """Return active `POS Reason` rows applicable to `privilege`."""
    if not frappe.db.table_exists("POS Reason"):
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
    if override is not None and pos_profile and hasattr(override, "requires_reason"):
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
                coded_message("POS-REASON-REQUIRED", _("Select a reason for this action.")),
                frappe.ValidationError,
            )
        return "", note

    if frappe.db.table_exists("POS Reason"):
        all_reasons = frappe.get_all(
            "POS Reason",
            fields=["code", "description", "applies_to", "requires_note", "disabled"],
        )
        matching_table = [
            r for r in (all_reasons or [])
            if cint(r.get("disabled")) == 0 and _reason_applies_to_privilege(r.get("applies_to"), privilege)
        ]
        # Match exact code or description
        found = None
        for r in all_reasons or []:
            if r.get("code") == cleaned or r.get("description") == cleaned:
                found = r
                break

        if found is not None:
            if cint(found.get("disabled")) == 1 or not _reason_applies_to_privilege(found.get("applies_to"), privilege):
                frappe.throw(
                    coded_message("POS-REASON-INVALID", _("Select an active reason configured for this action.")),
                    frappe.ValidationError,
                )
            if cint(found.get("requires_note")) == 1 and len(note) < 3:
                frappe.throw(
                    coded_message(
                        "POS-REASON-NOTE-REQUIRED",
                        _("Enter a note (at least 3 characters) for the selected reason."),
                    ),
                    frappe.ValidationError,
                )
            return str(found.get("code") or cleaned), note

        if matching_table:
            frappe.throw(
                coded_message("POS-REASON-INVALID", _("Select an active reason configured for this action.")),
                frappe.ValidationError,
            )

    if len(cleaned) < 3:
        frappe.throw(
            coded_message("POS-REASON-REQUIRED", _("Enter a reason of at least 3 characters.")),
            frappe.ValidationError,
        )
    return cleaned, note


def has_privilege_rules(company: str | None, privilege: str) -> bool:
    if not company or not frappe.db.table_exists("POS Privilege Rule"):
        return False
    rows = frappe.get_all(
        "POS Privilege Rule",
        filters={"company": company, "privilege": privilege},
        fields=["name"],
        limit=1,
    )
    return bool(rows)


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
    if not frappe.db.table_exists("POS Authorisation Log"):
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
            "event_time": now_datetime(),
            "claim_hash": claim_hash,
        }
    )
    doc.flags.pos_override_server_insert = True
    doc.insert(ignore_permissions=True)


def _profile_company(pos_profile: str | None) -> str | None:
    if not pos_profile:
        return None
    return frappe.db.get_value("POS Profile", pos_profile, "company")


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
    if not company or not frappe.db.table_exists("POS Privilege Rule"):
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
) -> dict[str, Any]:
    """Enforce privilege rules, PIN token consumption, reason validation, and audit logging.

    Returns ``{"approver": str, "via_pin": bool, "reason": str, "reason_note": str}``.
    """
    assert_not_offline_replay()
    validated_reason, validated_note = validate_pos_reason(
        privilege,
        pos_profile,
        reason,
        reason_note,
        required=require_reason,
    )

    user = getattr(getattr(frappe, "session", None), "user", None)
    company = _profile_company(pos_profile)
    override = _get_override_module()

    # Path 1: Manager approval token supplied -> consume atomically through B2a.
    if token:
        if override is None:
            frappe.throw(
                coded_message(
                    "POS-AUTH-NOT-ELIGIBLE",
                    _("Manager authorisation service is unavailable on this site."),
                ),
                frappe.PermissionError,
            )
        consumed = override.consume(token, privilege, pos_profile, reference_name, claim)
        return {
            "approver": consumed.get("approver"),
            "via_pin": True,
            "reason": validated_reason,
            "reason_note": validated_note,
        }

    # Path 2: Actions that ALWAYS require a second person (e.g. full comp, zeroing a priced item)
    # or explicitly escalated conditions (e.g. price below cost, over-HALO, re-split cap).
    if always_second_person or force_pin:
        frappe.throw(
            coded_message("POS-AUTH-REQUIRED", _("A manager must approve this action.")),
            frappe.PermissionError,
        )

    # Path 3: Company has explicit POS Privilege Rule rows -> B2a rules govern.
    if has_privilege_rules(company, privilege):
        if override is not None:
            if required_privilege_group > 0 and not _check_privilege_group(
                company, pos_profile, privilege, user, required_privilege_group
            ):
                frappe.throw(
                    coded_message("POS-AUTH-REQUIRED", _("A manager must approve this action.")),
                    frappe.PermissionError,
                )
            override.require(
                privilege,
                pos_profile,
                reference_name,
                claim,
                token=None,
                percent=percent,
                amount=amount,
            )
            _record_self_performed_log(
                privilege=privilege,
                pos_profile=pos_profile,
                company=company,
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
        frappe.throw(
            coded_message("POS-AUTH-REQUIRED", _("A manager must approve this action.")),
            frappe.PermissionError,
        )

    _record_self_performed_log(
        privilege=privilege,
        pos_profile=pos_profile,
        company=company,
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
