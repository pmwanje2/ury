"""POS void, waste, return/refund, and reopen/adjust engine (B5a — Simphony/SAP parity).

Matrix rows 5-10, tracker G8 & G34:
- Sent-item void / qty reduction / whole-check cancel with mandatory POS Reason,
  manager PIN override (via B2a `ury.ury_pos.override`), cancel-KOT reason link,
  and native ERPNext Stock Entry (Material Issue / Wastage) for waste reasons.
- Unsent (cart / held) items stay free (Simphony Role Opt 135 Error Correct).
- Closed checks cannot be cancelled directly; voiding a closed check goes through
  a native POS return (or same-day non-fiscal / credit-and-rebill reopen/adjust).
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal, ROUND_HALF_UP
import json
import sys
from typing import Any

import frappe as _default_frappe
try:
    from frappe.utils import cint as _frappe_cint
except Exception:
    _frappe_cint = None
try:
    from frappe.utils import flt as _frappe_flt
except Exception:
    _frappe_flt = None


def cint(value: Any, default: int = 0) -> int:
    if callable(_frappe_cint):
        try:
            return int(_frappe_cint(value))
        except Exception:
            return default
    try:
        return int(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def flt(value: Any, precision: int | None = None) -> float:
    if callable(_frappe_flt):
        try:
            return float(_frappe_flt(value, precision) if precision is not None else _frappe_flt(value))
        except Exception:
            return 0.0
    try:
        num = float(value) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0
    return round(num, precision) if precision is not None else num

# Neutral refusal codes (no vendor/brand names).
CODE_REASON_REQUIRED = "POS-REASON-REQUIRED"
CODE_REASON_NOTE_REQUIRED = "POS-REASON-NOTE-REQUIRED"
CODE_REASON_INVALID = "POS-REASON-INVALID"
CODE_AUTH_REQUIRED = "POS-AUTH-REQUIRED"
CODE_OUTLET_MISMATCH = "POS-OUTLET-MISMATCH"
CODE_VOID_CLOSED_USE_RETURN = "POS-VOID-CLOSED-USE-RETURN"
CODE_VOID_FISCALISED_USE_RETURN = "POS-VOID-FISCALISED-USE-RETURN"
CODE_VOID_PARTIAL_PAYMENT = "POS-VOID-PARTIAL-PAYMENT"
CODE_VOID_OFFLINE_REFUSED = "POS-VOID-OFFLINE-REFUSED"
CODE_OFFLINE_NOT_ALLOWED = "POS-OFFLINE-NOT-ALLOWED"
CODE_RETURN_NOT_CLOSED = "POS-RETURN-NOT-CLOSED"
CODE_RETURN_OF_RETURN = "POS-RETURN-OF-RETURN"
CODE_RETURN_CONSOLIDATED = "POS-RETURN-CONSOLIDATED"
CODE_RETURN_AFTER_DAY_CLOSE = "POS-RETURN-AFTER-DAY-CLOSE"
CODE_RETURN_MERGED = "POS-RETURN-MERGED"
CODE_RETURN_OVER_QTY = "POS-RETURN-OVER-QTY"
CODE_RETURN_INVALID_LINE = "POS-RETURN-INVALID-LINE"
CODE_RETURN_ROOM_CHARGE = "POS-RETURN-ROOM-CHARGE"
CODE_RETURN_DIFFERENT_TENDER = "POS-RETURN-DIFFERENT-TENDER"
CODE_RETURN_OVER_PAID = "POS-RETURN-OVER-PAID"
CODE_RETURN_TENDER_MISMATCH = "POS-RETURN-TENDER-MISMATCH"
CODE_RETURN_OWN_CHECK = "POS-RETURN-OWN-CHECK"
CODE_RETURN_UNREFERENCED_NOT_ALLOWED = "POS-RETURN-UNREFERENCED-NOT-ALLOWED"
CODE_REOPEN_NOT_CLOSED = "POS-REOPEN-NOT-CLOSED"
CODE_REOPEN_OF_RETURN = "POS-REOPEN-OF-RETURN"
CODE_REOPEN_PREVIOUS_DAY = "POS-REOPEN-PREVIOUS-DAY"
CODE_REOPEN_SHIFT_CLOSED = "POS-REOPEN-SHIFT-CLOSED"
CODE_REOPEN_CONSOLIDATED = "POS-REOPEN-CONSOLIDATED"
CODE_REOPEN_ALREADY_RETURNED = "POS-REOPEN-ALREADY-RETURNED"
CODE_REOPEN_FISCALISED = "POS-REOPEN-FISCALISED"
CODE_ADJUST_ITEMS_FORBIDDEN = "POS-ADJUST-ITEMS-FORBIDDEN"
CODE_ADJUST_TOTAL_MISMATCH = "POS-ADJUST-TOTAL-MISMATCH"

ZERO_DECIMAL_CURRENCIES = frozenset({"UGX", "RWF", "BIF", "JPY", "KRW", "VND", "TZS", "XAF", "XOF"})
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})

EFRIS_REASON_MAP = {
    "RET-QUALITY": "101:Return of products due to expiry or damage, etc.",
    "RET-OVERCHARGE": "103:Invoice amount wrongly stated due to miscalculation of price, tax, or discounts, etc.",
    "REOPEN-TENDER": "102:Cancellation of the purchase",
    "REOPEN-SPLIT": "102:Cancellation of the purchase",
}
DEFAULT_EFRIS_CREDIT_REASON = "102:Cancellation of the purchase"


def _f():
    """Return the active `frappe` module (stays isolated across site-less test suites)."""
    return sys.modules.get("frappe") or _default_frappe


def _tr(message: str) -> str:
    fn = getattr(_f(), "_", None)
    return fn(message) if callable(fn) else message


def _now_dt():
    utils = sys.modules.get("frappe.utils")
    fn = getattr(utils, "now_datetime", None) if utils else None
    if callable(fn):
        res = fn()
        if res is not None:
            return res
    return _dt.datetime.now()


def _today_date() -> str:
    utils = sys.modules.get("frappe.utils")
    fn = getattr(utils, "nowdate", None) if utils else None
    if callable(fn):
        res = fn()
        if isinstance(res, str) and res:
            return res
    return _now_dt().strftime("%Y-%m-%d")


def _now_time() -> str:
    utils = sys.modules.get("frappe.utils")
    fn = getattr(utils, "nowtime", None) if utils else None
    if callable(fn):
        res = fn()
        if isinstance(res, str) and res:
            return res
    return _now_dt().strftime("%H:%M:%S")


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field without triggering `.get()` method calls on MagicMock doubles."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _refuse(code: str, message: str, exc=None) -> None:
    fr = _f()
    fr.throw(f"{_tr(message)} [{code}]", exc or fr.ValidationError)


def _override_module():
    """Return `ury.ury_pos.override` when present (B2a or test stub), else None."""
    mod = sys.modules.get("ury.ury_pos.override")
    if mod is not None:
        return mod
    try:
        from ury.ury_pos import override as ov
        return ov
    except ImportError:
        return None


def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        meta = _f().get_meta(doctype)
        return bool(meta and meta.has_field(fieldname) is True)
    except Exception:
        return False


def _user_roles(user: str | None = None) -> set[str]:
    fr = _f()
    try:
        raw = fr.get_roles(user) if user else fr.get_roles()
    except TypeError:
        raw = fr.get_roles()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {str(r) for r in raw if isinstance(r, str)}
    return set()


def _safe_profile_name(pos_profile: Any) -> str | None:
    if isinstance(pos_profile, str) and pos_profile.strip():
        return pos_profile.strip()
    return None


def is_fiscalised_invoice(doc) -> bool:
    """True when the invoice has been fiscalised (EFRIS FDN / IRN or active E Invoice)."""
    if not doc:
        return False
    before = None
    if getattr(type(doc), "__module__", "") != "unittest.mock":
        get_before = getattr(doc, "get_doc_before_save", None)
        if callable(get_before):
            before = get_before()
    for candidate in (before, doc):
        if candidate is None:
            continue
        irn = _field(candidate, "efris_irn")
        if isinstance(irn, str) and irn.strip():
            return True
        status = _field(candidate, "efris_status")
        if isinstance(status, str) and status.strip() not in ("", "Cancelled"):
            return True
    name = _field(doc, "name")
    fr = _f()
    if isinstance(name, str) and name and fr.db.exists("DocType", "E Invoice") is True:
        ref = fr.db.get_value(
            "E Invoice",
            {"invoice": name},
            ["irn", "credit_note_application_ref_no"],
            as_dict=True,
        )
        if ref and (_field(ref, "irn") or _field(ref, "credit_note_application_ref_no")):
            return True
    return False


def received_tender_total(invoice) -> float:
    """Sum of confirmed server-marked received tenders on an open check."""
    payments = _field(invoice, "payments")
    if not isinstance(payments, (list, tuple)):
        return 0.0
    total = 0.0
    for row in payments:
        if cint(_field(row, "custom_tender_received", 0)) == 1:
            amt = flt(_field(row, "amount", 0))
            if amt > 0:
                total += amt
    return total


def has_sent_kot_for_invoice(invoice_name: str | None) -> bool:
    """True if an original kitchen/bar ticket (New Order / Order Modified) exists for this check."""
    if not isinstance(invoice_name, str) or not invoice_name:
        return False
    found = _f().db.exists(
        "URY KOT",
        {
            "invoice": invoice_name,
            "type": ["in", ["New Order", "Order Modified"]],
            "docstatus": ["in", [1, 2]],
        },
    )
    return isinstance(found, str) and bool(found)


def has_sent_kitchen_activity(invoice) -> bool:
    """True when an open check has already been sent to kitchen/bar or printed."""
    name = _field(invoice, "name")
    if has_sent_kot_for_invoice(name):
        return True
    printed = _field(invoice, "invoice_printed", 0)
    if isinstance(printed, (int, float, bool)) and cint(printed) == 1:
        return True
    items = _field(invoice, "items")
    if isinstance(items, (list, tuple)):
        positive = [r for r in items if flt(_field(r, "qty", 0)) > 0]
        if positive and any(cint(_field(r, "ury_held", 0)) == 0 for r in positive):
            return True
    return False


def _is_waste_reason(code: str, description: str, is_waste: Any = 0) -> bool:
    if cint(is_waste) == 1:
        return True
    code_up = (code or "").strip().upper()
    if code_up.startswith("WASTE"):
        return True
    desc_low = (description or "").strip().lower()
    return "(waste)" in desc_low or desc_low.startswith("waste")


def _map_wastage_reason(code: str, description: str, note: str = "") -> str:
    """Map POS waste reason to ERPNext/restaurant-controls custom_wastage_reason options."""
    haystack = f"{code} {description} {note}".lower()
    if "broken" in haystack or "break" in haystack or ("drop" in haystack and "spoil" not in haystack):
        return "Broken"
    if "spoil" in haystack or "burn" in haystack or "contaminated" in haystack or "drop" in haystack:
        return "Spoiled"
    if "expir" in haystack:
        return "Expired"
    if "theft" in haystack or "stolen" in haystack:
        return "Theft"
    return "Other"


def _active_pos_reasons(privilege: str) -> list[dict[str, Any]]:
    ov = _override_module()
    if ov is not None and hasattr(ov, "list_reasons"):
        try:
            rows = ov.list_reasons(privilege)
            if isinstance(rows, list):
                valid = [r for r in rows if isinstance(_field(r, "code"), str) and _field(r, "code").strip()]
                if valid:
                    return valid
        except Exception:
            pass
    fr = _f()
    try:
        if fr.db.table_exists("POS Reason") is not True:
            return []
        raw = fr.get_all(
            "POS Reason",
            filters={"disabled": 0},
            fields=["code", "description", "applies_to", "requires_note", "sort_order"],
            order_by="sort_order asc, code asc",
        )
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for row in raw:
        code = _field(row, "code")
        if not isinstance(code, str) or not code.strip():
            continue
        targets = [
            p.strip()
            for p in str(_field(row, "applies_to") or "").replace(",", "\n").splitlines()
            if p.strip()
        ]
        if privilege and targets and privilege not in targets:
            continue
        out.append({
            "code": code,
            "description": _field(row, "description"),
            "requires_note": cint(_field(row, "requires_note", 0)),
            "applies_to": targets,
        })
    return out


def resolve_pos_reason(
    privilege: str,
    pos_profile: str | None,
    reason: Any,
    note: Any = None,
    is_waste: Any = 0,
    *,
    fallback_validator=None,
    invoice_for_fallback=None,
    fallback_privileges: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Validate and normalise a POS Reason (with graceful legacy fallback when no POS Reason rows exist)."""
    fr = _f()
    reasons = _active_pos_reasons(privilege)
    clean_note = note.strip() if isinstance(note, str) else ""

    # Honour POS Privilege Rule `requires_reason=0` when caller supplied no reason.
    if (not isinstance(reason, str) or not reason.strip()) and not clean_note and cint(is_waste) == 0:
        ov = _override_module()
        profile_name = _safe_profile_name(pos_profile)
        if ov is not None and profile_name and callable(getattr(ov, "requires_reason", None)):
            try:
                privs_to_check = (privilege, *fallback_privileges)
                if any(ov.requires_reason(profile_name, p) is False for p in privs_to_check):
                    return {
                        "code": "",
                        "description": "",
                        "note": "",
                        "display": "",
                        "stored": "",
                        "is_waste": False,
                    }
            except Exception:
                pass

    if reasons:
        if not isinstance(reason, str) or not reason.strip():
            _refuse(CODE_REASON_REQUIRED, "Choose a reason before continuing.", fr.ValidationError)
        candidate = reason.strip()

        # Support legacy "Other: <note>" syntax cleanly when OTHER reason exists.
        if candidate.startswith("Other: ") and not clean_note:
            other_row = next((r for r in reasons if str(r.get("code") or "").upper() == "OTHER"), None)
            if other_row is not None:
                clean_note = candidate[len("Other: "):].strip()
                candidate = other_row["code"]

        matched = None
        for row in reasons:
            if candidate == row.get("code") or candidate == row.get("description"):
                matched = row
                break
        if matched is None:
            cand_low = candidate.lower()
            prefix_matches = [
                row
                for row in reasons
                if str(row.get("description") or "").lower().startswith(cand_low + " /")
            ]
            if len(prefix_matches) == 1:
                matched = prefix_matches[0]

        if matched is None:
            _refuse(CODE_REASON_INVALID, "Choose an active configured reason.", fr.ValidationError)

        if cint(matched.get("requires_note")) == 1 and len(clean_note) < 4:
            _refuse(
                CODE_REASON_NOTE_REQUIRED,
                "Enter a note of at least 4 characters for this reason.",
                fr.ValidationError,
            )

        code = str(matched["code"])
        description = str(matched.get("description") or code)
        waste_flag = _is_waste_reason(code, description, is_waste=is_waste)
        text = f"{description}: {clean_note}" if clean_note else description
        display = f"[WASTE] {text}" if waste_flag else text
        stored = f"Other: {clean_note}" if code.upper() == "OTHER" and clean_note else text
        return {
            "code": code,
            "description": description,
            "note": clean_note,
            "display": display,
            "stored": stored,
            "is_waste": waste_flag,
        }

    # No POS Reason rows configured on this site yet: keep existing POS Profile / 4-char rule usable.
    if fallback_validator is not None and invoice_for_fallback is not None:
        stored = fallback_validator(invoice_for_fallback, reason)
    else:
        if not isinstance(reason, str) or len(reason.strip()) < 4:
            _refuse(CODE_REASON_REQUIRED, "Enter a reason (at least 4 characters).", fr.ValidationError)
        stored = reason.strip()
        if clean_note:
            stored = f"{stored}: {clean_note}"

    waste_flag = _is_waste_reason(stored, stored, is_waste=is_waste)
    display = f"[WASTE] {stored}" if waste_flag else stored
    return {
        "code": stored,
        "description": stored,
        "note": clean_note,
        "display": display,
        "stored": stored,
        "is_waste": waste_flag,
    }


def write_authorisation_log(
    *,
    requester: str | None,
    approver: str | None,
    privilege: str,
    pos_profile: str | None,
    company: str | None,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any],
    reason: str = "",
    outcome: str = "Consumed",
    refusal_code: str = "",
) -> None:
    """Record a server-managed POS Authorisation Log entry (insert-only, never leaks PINs)."""
    fr = _f()
    try:
        if fr.db.table_exists("POS Authorisation Log") is not True:
            return
    except Exception:
        return
    doc = fr.get_doc(
        {
            "doctype": "POS Authorisation Log",
            "requester": requester,
            "approver": approver,
            "privilege": privilege,
            "pos_profile": pos_profile,
            "company": company,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "claim_values": json.dumps(claim or {}, sort_keys=True, separators=(",", ":"), default=str),
            "reason": str(reason or "").strip()[:500],
            "outcome": outcome,
            "refusal_code": refusal_code,
            "event_time": _now_dt(),
        }
    )
    doc.flags.pos_override_server_insert = True
    doc.insert(ignore_permissions=True)


def is_override_policy_active(company: str | None) -> bool:
    """True when `ury.ury_pos.override` is present and active rules exist for `company`."""
    if not isinstance(company, str) or not company.strip():
        return False
    ov = _override_module()
    if ov is None:
        return False
    fr = _f()
    try:
        if fr.db.table_exists("POS Privilege Rule") is not True:
            return False
    except Exception:
        return False
    return bool(ov.is_policy_active(company))


def can_perform_pos_privilege(
    user: str | None,
    pos_profile: str | None,
    privilege: str,
    *,
    percent: float | None = None,
    amount: float | None = None,
    privilege_group: int = 0,
    fallback_privileges: tuple[str, ...] = (),
) -> bool:
    """True if `user` can perform `privilege` alone under POS Privilege Rules (via `ury.ury_pos.override`)."""
    fr = _f()
    profile_name = _safe_profile_name(pos_profile)
    actor = user or getattr(getattr(fr, "session", None), "user", None) or ""
    ov = _override_module()
    if ov is None or not profile_name or not callable(getattr(ov, "may_perform", None)):
        return False
    if ov.may_perform(
        actor,
        profile_name,
        privilege,
        percent=percent,
        amount=amount,
        privilege_group=privilege_group,
    ):
        return True
    for alt_priv in fallback_privileges:
        if ov.may_perform(
            actor,
            profile_name,
            alt_priv,
            percent=percent,
            amount=amount,
            privilege_group=privilege_group,
        ):
            return True
    return False


class _VerifiedNewReturnProof:
    """In-process proof for newly inserted POS Return docs whose name is assigned during `insert()`."""

    __slots__ = ("doc", "privilege")

    def __init__(self, doc: Any, privilege: str):
        self.doc = doc
        self.privilege = privilege


def mark_verified_pos_approval(doc, privilege: str, approval=None) -> None:
    """Attach B2a's unforgeable in-process approval proof to `doc` when available."""
    if doc is not None and hasattr(doc, "flags"):
        setattr(doc.flags, f"_pos_verified_{privilege}_proof", _VerifiedNewReturnProof(doc, privilege))
    ov = _override_module()
    if ov is not None and hasattr(ov, "mark_verified_approval") and doc is not None:
        ov.mark_verified_approval(doc, privilege, approval)


def _select_token_claim_and_privilege(
    ov,
    token: str,
    privilege: str,
    claim: dict[str, Any],
    fallback_privileges: tuple[str, ...],
    alt_claim: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Choose between B5a's claim/privilege and B2a's compat claim without weakening hash checks."""
    if not fallback_privileges and alt_claim is None:
        return privilege, claim
    hash_fn = getattr(ov, "_token_digest", None) or getattr(ov, "_hash_token", None)
    if not callable(hash_fn):
        return privilege, claim
    fr = _f()
    try:
        if fr.db.table_exists("POS Authorisation Log") is not True:
            return privilege, claim
        row = fr.db.get_value(
            "POS Authorisation Log",
            {"token_hash": hash_fn(str(token).strip()), "outcome": "Approved"},
            ["privilege", "claim_values"],
            as_dict=True,
        )
    except Exception:
        return privilege, claim
    if not row:
        return privilege, claim
    stored_priv = _field(row, "privilege")
    chosen_priv = stored_priv if stored_priv in (privilege, *fallback_privileges) else privilege
    chosen_claim = claim
    if alt_claim is not None:
        raw_cv = _field(row, "claim_values") or ""
        if isinstance(raw_cv, str) and '"action":' in raw_cv:
            chosen_claim = alt_claim
    return chosen_priv, chosen_claim


def require_pos_override(
    privilege: str,
    pos_profile: str | None,
    reference_doctype: str,
    reference_name: str,
    claim: dict[str, Any],
    *,
    approval_token: str | None = None,
    percent: float | None = None,
    amount: float | None = None,
    privilege_group: int = 0,
    forbid_solo: bool = False,
    doc=None,
    fallback_privileges: tuple[str, ...] = (),
    alt_claim: dict[str, Any] | None = None,
) -> str:
    """Enforce Simphony/SAP privilege or single-use manager PIN token and always log use."""
    fr = _f()
    ov = _override_module()
    profile_name = _safe_profile_name(pos_profile)
    company = (
        fr.db.get_value("POS Profile", profile_name, "company")
        if profile_name
        else None
    )
    if not isinstance(company, str):
        company = None
    user = getattr(getattr(fr, "session", None), "user", None) or ""

    if approval_token:
        if ov is None:
            _refuse(CODE_AUTH_REQUIRED, "Manager approval service is unavailable.", fr.PermissionError)
        chosen_priv, chosen_claim = _select_token_claim_and_privilege(
            ov, approval_token, privilege, claim, fallback_privileges, alt_claim
        )
        consumed = ov.consume(approval_token, chosen_priv, profile_name, reference_name, chosen_claim)
        if doc is not None:
            mark_verified_pos_approval(doc, chosen_priv, consumed)
            if chosen_priv != privilege:
                mark_verified_pos_approval(doc, privilege, consumed)
        return str(consumed.approver)

    if forbid_solo:
        _refuse(CODE_AUTH_REQUIRED, "A different manager must approve this action.", fr.PermissionError)

    if ov is None or not profile_name or not callable(getattr(ov, "may_perform", None)):
        _refuse(CODE_AUTH_REQUIRED, "A manager must approve this action.", fr.PermissionError)

    allowed_priv = None
    if ov.may_perform(
        user,
        profile_name,
        privilege,
        percent=percent,
        amount=amount,
        privilege_group=privilege_group,
    ):
        allowed_priv = privilege
    else:
        for alt_priv in fallback_privileges:
            if ov.may_perform(
                user,
                profile_name,
                alt_priv,
                percent=percent,
                amount=amount,
                privilege_group=privilege_group,
            ):
                allowed_priv = alt_priv
                break
    if allowed_priv is None:
        if callable(getattr(ov, "require", None)):
            ov.require(
                privilege,
                profile_name,
                reference_name,
                claim,
                token=None,
                percent=percent,
                amount=amount,
                privilege_group=privilege_group,
            )
            allowed_priv = privilege
        else:
            _refuse(CODE_AUTH_REQUIRED, "A manager must approve this action.", fr.PermissionError)
    if doc is not None:
        mark_verified_pos_approval(doc, allowed_priv)
        if allowed_priv != privilege:
            mark_verified_pos_approval(doc, privilege)
    write_authorisation_log(
        requester=user,
        approver=user,
        privilege=allowed_priv,
        pos_profile=profile_name,
        company=company,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        claim=claim,
        reason=str(claim.get("reason") or ""),
        outcome="Consumed",
    )
    return user


def _expand_stock_waste_items(waste_items: list[dict[str, Any]], default_warehouse: str | None) -> list[dict[str, Any]]:
    """Expand Product Bundles and filter to stock items (`is_stock_item == 1`) only."""
    fr = _f()
    expanded: list[dict[str, Any]] = []
    for entry in waste_items:
        item_code = entry.get("item_code")
        qty = flt(entry.get("qty"))
        if not item_code or qty <= 0:
            continue
        warehouse = entry.get("warehouse") or default_warehouse

        bundle_name = fr.db.exists("Product Bundle", {"new_item_code": item_code})
        if isinstance(bundle_name, str) and bundle_name:
            components = fr.get_all(
                "Product Bundle Item",
                filters={"parent": bundle_name, "parenttype": "Product Bundle"},
                fields=["item_code", "qty", "uom"],
                order_by="idx",
            ) or []
            for comp in components:
                comp_code = _field(comp, "item_code")
                comp_info = fr.db.get_value(
                    "Item", comp_code, ["is_stock_item", "stock_uom"], as_dict=True
                )
                if comp_info is not None and cint(_field(comp_info, "is_stock_item", 0)) == 1:
                    expanded.append({
                        "item_code": comp_code,
                        "qty": qty * flt(_field(comp, "qty") or 1),
                        "uom": _field(comp, "uom") or _field(comp_info, "stock_uom"),
                        "warehouse": warehouse,
                    })
            continue

        item_info = fr.db.get_value("Item", item_code, ["is_stock_item", "stock_uom"], as_dict=True)
        if item_info is not None and cint(_field(item_info, "is_stock_item", 0)) == 1:
            expanded.append({
                "item_code": item_code,
                "qty": qty,
                "uom": entry.get("uom") or _field(item_info, "stock_uom"),
                "warehouse": warehouse,
            })
    return expanded


def _resolve_waste_expense_account(company: str | None) -> str | None:
    if not company:
        return None
    fr = _f()
    accounts = fr.get_all(
        "Account",
        filters={
            "account_name": "Wastage and Spoilage",
            "company": company,
            "is_group": 0,
            "root_type": "Expense",
        },
        pluck="name",
        limit_page_length=2,
    )
    if isinstance(accounts, list) and len(accounts) == 1:
        return accounts[0]
    return fr.db.get_value("Company", company, "stock_adjustment_account")


def _resolve_waste_cost_center(pos_profile, company: str | None, branch: str | None) -> str | None:
    fr = _f()
    cc = _field(pos_profile, "cost_center") if pos_profile else None
    if cc:
        return cc
    if branch:
        cc = fr.db.get_value("Cost Center", {"branch": branch, "is_group": 0}, "name")
        if cc:
            return cc
    if company:
        return fr.db.get_value("Company", company, "cost_center")
    return None


def post_waste_stock_entry(
    pos_invoice,
    pos_profile,
    waste_items: list[dict[str, Any]],
    reason_info: dict[str, Any],
    *,
    cancel_kot_names: list[str] | None = None,
    approver: str | None = None,
) -> str | None:
    """Post a native ERPNext Stock Entry (Material Issue / Wastage) for wasted sent items.

    Skips non-stock items cleanly. Links the Stock Entry onto created cancel KOTs and
    onto the invoice when the link fields exist.
    """
    fr = _f()
    default_wh = _field(pos_profile, "warehouse") if pos_profile else None
    stock_lines = _expand_stock_waste_items(waste_items, default_wh)
    if not stock_lines:
        return None

    company = _field(pos_invoice, "company") or (_field(pos_profile, "company") if pos_profile else None)
    branch = _field(pos_invoice, "branch") or (_field(pos_profile, "branch") if pos_profile else None)

    use_wastage_type = (
        bool(fr.db.exists("Stock Entry Type", "Wastage"))
        and fr.db.get_value("Stock Entry Type", "Wastage", "purpose") == "Material Issue"
    )
    entry_type = "Wastage" if use_wastage_type else "Material Issue"
    expense_account = _resolve_waste_expense_account(company)
    cost_center = _resolve_waste_cost_center(pos_profile, company, branch)

    se = fr.new_doc("Stock Entry")
    se.stock_entry_type = entry_type
    se.purpose = "Material Issue"
    se.company = company
    if branch:
        se.branch = branch
    if default_wh:
        se.from_warehouse = default_wh
    se.posting_date = _today_date()
    se.posting_time = _now_time()

    if _has_field("Stock Entry", "custom_wastage_reason"):
        se.custom_wastage_reason = _map_wastage_reason(
            reason_info.get("code", ""),
            reason_info.get("description", ""),
            reason_info.get("note", ""),
        )

    kot_ref = ", ".join(cancel_kot_names or [])
    requester = getattr(getattr(fr, "session", None), "user", None) or ""
    se.remarks = (
        f"POS Waste | Check: {pos_invoice.name}"
        + (f" | Cancel KOT: {kot_ref}" if kot_ref else "")
        + f" | Reason: {reason_info.get('stored') or reason_info.get('code')}"
        + f" | By: {requester}"
        + (f" | Approved by: {approver}" if approver else "")
    )

    for line in stock_lines:
        row = {
            "item_code": line["item_code"],
            "qty": flt(line["qty"]),
            "s_warehouse": line["warehouse"] or default_wh,
            "allow_zero_valuation_rate": 1,
        }
        if line.get("uom"):
            row["uom"] = line["uom"]
        if expense_account:
            row["expense_account"] = expense_account
        if cost_center:
            row["cost_center"] = cost_center
        se.append("items", row)

    se.insert(ignore_permissions=True)
    se.submit()

    # Link the posted Stock Entry back onto the cancel KOT(s) and invoice when fields exist.
    if cancel_kot_names and _has_field("URY KOT", "custom_waste_stock_entry"):
        for kot_name in cancel_kot_names:
            try:
                kot_doc = fr.get_doc("URY KOT", kot_name)
                kot_doc.custom_waste_stock_entry = se.name
            except Exception:
                pass
            fr.db.set_value("URY KOT", kot_name, "custom_waste_stock_entry", se.name, update_modified=False)

    inv_name = _field(pos_invoice, "name")
    if _has_field("POS Invoice", "custom_waste_stock_entry") and isinstance(inv_name, str) and inv_name:
        if hasattr(pos_invoice, "db_set"):
            pos_invoice.db_set("custom_waste_stock_entry", se.name, update_modified=False)
        else:
            fr.db.set_value("POS Invoice", inv_name, "custom_waste_stock_entry", se.name, update_modified=False)

    return se.name


# ---------------------------------------------------------------------------
# Slice 2: POS Return / Refund at the till (Matrix row 9, tracker G34)
# ---------------------------------------------------------------------------


def _currency_precision(currency: str | None) -> int:
    code = (currency or "").strip().upper()
    if code:
        try:
            frac = _f().db.get_value("Currency", code, "fraction_units")
            if isinstance(frac, int) and not isinstance(frac, bool):
                if frac <= 1:
                    return 0
                if frac == 1000:
                    return 3
                if frac == 100:
                    return 2
        except Exception:
            pass
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def _dec(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0))


def _q(value: Decimal, precision: int) -> Decimal:
    quantum = Decimal("1") if precision <= 0 else Decimal(f"1e-{precision}")
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def _parse_json_arg(val: Any) -> Any:
    if isinstance(val, str):
        return json.loads(val)
    return val


def _assert_same_outlet(doc_branch: str | None) -> None:
    fr = _f()
    actor = getattr(getattr(fr, "session", None), "user", None) or ""
    if actor == "Administrator":
        return
    if not isinstance(doc_branch, str) or not doc_branch.strip():
        return
    from ury.ury_pos.api import getBranch

    user_branch = getBranch()
    if isinstance(user_branch, str) and user_branch.strip() and user_branch.strip() != doc_branch.strip():
        _refuse(
            CODE_OUTLET_MISMATCH,
            "Not permitted to process checks from another outlet.",
            fr.PermissionError,
        )


def _is_room_charge_mode(mode_of_payment: str | None) -> bool:
    if not isinstance(mode_of_payment, str) or not mode_of_payment.strip():
        return False
    low = mode_of_payment.strip().lower()
    if "room" in low or "folio" in low or "pms" in low:
        return True
    mop_type = _f().db.get_value("Mode of Payment", mode_of_payment, "type")
    return isinstance(mop_type, str) and mop_type.strip().lower() in ("room charge", "folio")


def _is_room_charge_invoice(inv) -> bool:
    room = _field(inv, "room_number")
    if isinstance(room, str) and room.strip():
        return True
    for p in _field(inv, "payments") or []:
        if flt(_field(p, "amount", 0)) > 0 and _is_room_charge_mode(_field(p, "mode_of_payment")):
            return True
    return False


def _unreferenced_return_roles(pos_profile_doc) -> set[str]:
    raw = _field(pos_profile_doc, "custom_unreferenced_return_roles") or ""
    if not isinstance(raw, str):
        return set()
    return {part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip()}


def _assert_not_consolidated_or_day_closed(orig) -> None:
    fr = _f()
    orig_name = _field(orig, "name")
    if _field(orig, "consolidated_invoice") or str(_field(orig, "status") or "") == "Consolidated":
        _refuse(
            CODE_RETURN_CONSOLIDATED,
            "This check has already been consolidated. Issue a credit note against the consolidated invoice instead.",
            fr.ValidationError,
        )
    if isinstance(orig_name, str) and orig_name:
        if fr.db.table_exists("POS Invoice Reference") is True:
            merge_rows = fr.db.sql(
                """
                SELECT piml.name
                FROM `tabPOS Invoice Merge Log` piml
                INNER JOIN `tabPOS Invoice Reference` pir
                    ON pir.parent = piml.name AND pir.parenttype = 'POS Invoice Merge Log'
                WHERE piml.docstatus = 1 AND pir.pos_invoice = %s
                LIMIT 1
                """,
                (orig_name,),
            )
            if merge_rows:
                _refuse(
                    CODE_RETURN_CONSOLIDATED,
                    "This check is included in a submitted consolidation log and cannot be returned at the till.",
                    fr.ValidationError,
                )
            close_rows = fr.db.sql(
                """
                SELECT pce.name
                FROM `tabPOS Closing Entry` pce
                INNER JOIN `tabPOS Invoice Reference` pir
                    ON pir.parent = pce.name AND pir.parenttype = 'POS Closing Entry'
                WHERE pce.docstatus = 1 AND pir.pos_invoice = %s
                LIMIT 1
                """,
                (orig_name,),
            )
            if close_rows:
                _refuse(
                    CODE_RETURN_AFTER_DAY_CLOSE,
                    "This check belongs to a closed shift/day and cannot be returned at the till.",
                    fr.ValidationError,
                )


def _load_prior_returns(orig_name: str, *, for_update: bool = False) -> list[Any]:
    fr = _f()
    if for_update:
        rows = fr.db.sql(
            """
            SELECT name FROM `tabPOS Invoice`
            WHERE return_against = %s AND is_return = 1 AND docstatus = 1
            ORDER BY creation ASC, name ASC
            FOR UPDATE
            """,
            (orig_name,),
        )
        names = [r[0] if isinstance(r, (list, tuple)) else _field(r, "name") for r in (rows or [])]
    else:
        names = fr.get_all(
            "POS Invoice",
            filters={"return_against": orig_name, "is_return": 1, "docstatus": 1},
            pluck="name",
        ) or []
    return [fr.get_doc("POS Invoice", n) for n in names if isinstance(n, str) and n]


def _prior_returned_qty_by_line(orig, prior_returns: list[Any]) -> dict[str, Decimal]:
    """Return already-returned positive quantities keyed by original `POS Invoice Item.name`."""
    returned: dict[str, Decimal] = {}
    orig_items = list(_field(orig, "items") or [])
    by_name = {_field(r, "name"): r for r in orig_items if _field(r, "name")}
    by_code: dict[str, list[Any]] = {}
    for r in orig_items:
        by_code.setdefault(_field(r, "item_code"), []).append(r)

    for ret in prior_returns:
        for row in _field(ret, "items") or []:
            qty_abs = abs(_dec(_field(row, "qty", 0)))
            if qty_abs <= 0:
                continue
            ref = _field(row, "pos_invoice_item")
            if ref and ref in by_name:
                returned[ref] = returned.get(ref, Decimal("0")) + qty_abs
                continue
            # Fallback when ERPNext/legacy return omitted pos_invoice_item: allocate FIFO by item_code.
            remaining = qty_abs
            for cand in by_code.get(_field(row, "item_code"), []):
                cname = _field(cand, "name")
                cap = _dec(_field(cand, "qty", 0)) - returned.get(cname, Decimal("0"))
                if cap <= 0:
                    continue
                take = min(remaining, cap)
                returned[cname] = returned.get(cname, Decimal("0")) + take
                remaining -= take
                if remaining <= 0:
                    break
    return returned


def _net_original_payments(orig, precision: int) -> list[dict[str, Any]]:
    """Return original positive payment modes net of `change_amount`."""
    raw_payments = [
        {"mode_of_payment": _field(p, "mode_of_payment"), "amount": _dec(_field(p, "amount", 0))}
        for p in (_field(orig, "payments") or [])
        if _dec(_field(p, "amount", 0)) > 0 and _field(p, "mode_of_payment")
    ]
    change = _dec(_field(orig, "change_amount", 0))
    if change > 0 and raw_payments:
        # Deduct change from Cash tender first (standard POS convention), then last tender.
        remaining_change = change
        for p in raw_payments:
            mop_type = _f().db.get_value("Mode of Payment", p["mode_of_payment"], "type")
            if mop_type == "Cash" or p["mode_of_payment"].strip().lower() == "cash":
                deduct = min(p["amount"], remaining_change)
                p["amount"] -= deduct
                remaining_change -= deduct
                if remaining_change <= 0:
                    break
        if remaining_change > 0:
            for p in reversed(raw_payments):
                deduct = min(p["amount"], remaining_change)
                p["amount"] -= deduct
                remaining_change -= deduct
                if remaining_change <= 0:
                    break

    # Aggregate by mode_of_payment preserving original order
    agg: dict[str, Decimal] = {}
    order: list[str] = []
    for p in raw_payments:
        amt = _q(p["amount"], precision)
        if amt <= 0:
            continue
        mop = p["mode_of_payment"]
        if mop not in agg:
            order.append(mop)
            agg[mop] = Decimal("0")
        agg[mop] += amt
    return [{"mode_of_payment": mop, "amount": agg[mop]} for mop in order]


def _default_refund_payments_for_amount(
    orig,
    prior_returns: list[Any],
    target_refund: Decimal,
    precision: int,
) -> list[dict[str, float]]:
    if target_refund <= 0:
        return []
    orig_net = _net_original_payments(orig, precision)
    prior_by_mop: dict[str, Decimal] = {}
    for ret in prior_returns:
        for p in _field(ret, "payments") or []:
            mop = _field(p, "mode_of_payment")
            amt = abs(_dec(_field(p, "amount", 0)))
            if mop and amt > 0:
                prior_by_mop[mop] = prior_by_mop.get(mop, Decimal("0")) + amt

    available: list[dict[str, Any]] = []
    for entry in orig_net:
        rem = entry["amount"] - prior_by_mop.get(entry["mode_of_payment"], Decimal("0"))
        if rem > 0:
            available.append({"mode_of_payment": entry["mode_of_payment"], "amount": rem})

    out: list[dict[str, float]] = []
    left = target_refund
    for entry in available:
        if left <= 0:
            break
        take = _q(min(left, entry["amount"]), precision)
        if take > 0:
            out.append({"mode_of_payment": entry["mode_of_payment"], "amount": float(take)})
            left -= take
    return out


def _compute_return_plan(
    orig,
    prior_returns: list[Any],
    items_req: Any,
    refund_payments_req: Any = None,
    allow_different_tender: int = 0,
    pos_profile_doc=None,
) -> dict[str, Any]:
    """Compute exact proportional discount/tax/refund tender plan with cumulative-remainder closure."""
    fr = _f()
    parsed_items = _parse_json_arg(items_req)
    if not isinstance(parsed_items, list) or not parsed_items:
        _refuse(CODE_RETURN_INVALID_LINE, "Select at least one item to return.", fr.ValidationError)

    currency = _field(orig, "currency") or "UGX"
    precision = _currency_precision(currency)
    orig_items = list(_field(orig, "items") or [])
    orig_by_name = {_field(r, "name"): r for r in orig_items if _field(r, "name")}
    prior_qty = _prior_returned_qty_by_line(orig, prior_returns)

    # Aggregate requested qty per original row so duplicate payload entries cannot bypass caps.
    req_by_row: dict[str, Decimal] = {}
    req_order: list[str] = []
    for entry in parsed_items:
        if not isinstance(entry, dict):
            _refuse(CODE_RETURN_INVALID_LINE, "Each return line must be an object.", fr.ValidationError)
        row_name = entry.get("item_row_name") or entry.get("pos_invoice_item") or entry.get("name")
        if not isinstance(row_name, str) or row_name not in orig_by_name:
            _refuse(CODE_RETURN_INVALID_LINE, "Return item line was not found on the original check.", fr.ValidationError)
        qty_d = _dec(entry.get("qty", 0))
        if qty_d <= 0:
            _refuse(CODE_RETURN_INVALID_LINE, "Return quantity must be greater than zero.", fr.ValidationError)
        if row_name not in req_by_row:
            req_order.append(row_name)
            req_by_row[row_name] = Decimal("0")
        req_by_row[row_name] += qty_d

    selected_lines: list[dict[str, Any]] = []
    this_gross = Decimal("0")
    for row_name in req_order:
        orig_row = orig_by_name[row_name]
        sold = _dec(_field(orig_row, "qty", 0))
        already = prior_qty.get(row_name, Decimal("0"))
        remaining = sold - already
        req_qty = req_by_row[row_name]
        if req_qty > remaining + Decimal("1e-9"):
            _refuse(
                CODE_RETURN_OVER_QTY,
                f"Cannot return {req_qty} of {_field(orig_row, 'item_name') or _field(orig_row, 'item_code')}; only {remaining} remaining.",
                fr.ValidationError,
            )
        rate = _dec(_field(orig_row, "rate", 0))
        line_gross = req_qty * rate
        this_gross += line_gross
        selected_lines.append({
            "item_row_name": row_name,
            "item_code": _field(orig_row, "item_code"),
            "item_name": _field(orig_row, "item_name") or _field(orig_row, "item_code"),
            "qty": float(req_qty),
            "rate": float(rate),
            "gross": float(_q(line_gross, precision)),
            "orig_row": orig_row,
        })

    orig_gross = sum(_dec(_field(r, "qty", 0)) * _dec(_field(r, "rate", 0)) for r in orig_items)
    if orig_gross <= 0:
        _refuse(CODE_RETURN_INVALID_LINE, "Original check has no returnable line total.", fr.ValidationError)

    prior_gross = Decimal("0")
    prior_discount = Decimal("0")
    prior_excl_tax = Decimal("0")
    prior_total = Decimal("0")
    prior_paid_refunded = Decimal("0")
    for ret in prior_returns:
        prior_gross += sum(abs(_dec(_field(r, "qty", 0))) * _dec(_field(r, "rate", 0)) for r in (_field(ret, "items") or []))
        prior_discount += abs(_dec(_field(ret, "discount_amount", 0)))
        prior_excl_tax += sum(
            abs(_dec(_field(t, "tax_amount", 0)))
            for t in (_field(ret, "taxes") or [])
            if cint(_field(t, "included_in_print_rate", 0)) == 0
        )
        prior_total += abs(_dec(_field(ret, "rounded_total") or _field(ret, "grand_total") or 0))
        prior_paid_refunded += sum(abs(_dec(_field(p, "amount", 0))) for p in (_field(ret, "payments") or []))

    # Cumulative-remainder proportional discount allocation (zero drift in UGX whole shillings!).
    orig_discount = abs(_dec(_field(orig, "discount_amount", 0)))
    cum_gross = prior_gross + this_gross
    if orig_discount > 0:
        cum_discount = _q(min(orig_discount, orig_discount * cum_gross / orig_gross), precision)
        this_discount = max(Decimal("0"), cum_discount - prior_discount)
    else:
        this_discount = Decimal("0")

    # Cumulative-remainder proportional exclusive tax allocation.
    orig_taxes = list(_field(orig, "taxes") or [])
    orig_excl_tax = sum(
        _dec(_field(t, "tax_amount", 0))
        for t in orig_taxes
        if cint(_field(t, "included_in_print_rate", 0)) == 0
    )
    orig_net = orig_gross - orig_discount
    this_net = _q(this_gross, precision) - this_discount
    prior_net = _q(prior_gross, precision) - prior_discount

    if orig_excl_tax > 0 and orig_net > 0:
        cum_net = prior_net + this_net
        cum_excl_tax = _q(min(orig_excl_tax, orig_excl_tax * cum_net / orig_net), precision)
        this_excl_tax = max(Decimal("0"), cum_excl_tax - prior_excl_tax)
    else:
        this_excl_tax = Decimal("0")

    orig_grand = _q(_dec(_field(orig, "rounded_total") or _field(orig, "grand_total") or (orig_net + orig_excl_tax)), precision)
    # Check if this return exhausts every remaining line on the check -> guarantee exact grand_total closure.
    is_final_return = all(
        (prior_qty.get(_field(r, "name"), Decimal("0")) + req_by_row.get(_field(r, "name"), Decimal("0"))) >= _dec(_field(r, "qty", 0)) - Decimal("1e-9")
        for r in orig_items
        if _dec(_field(r, "qty", 0)) > 0
    )
    if is_final_return:
        refund_total = max(Decimal("0"), orig_grand - prior_total)
        # Reconcile any 1-unit rounding remainder cleanly into tax/discount.
        computed = this_net + this_excl_tax
        diff = refund_total - computed
        if diff != 0:
            if orig_excl_tax > 0:
                this_excl_tax += diff
            else:
                this_discount -= diff
    else:
        refund_total = max(Decimal("0"), min(orig_grand - prior_total, this_net + this_excl_tax))

    # Proportional per-tax-row amounts for native ERPNext return taxes child table.
    tax_rows_out: list[dict[str, Any]] = []
    excl_rows = [t for t in orig_taxes if cint(_field(t, "included_in_print_rate", 0)) == 0]
    if excl_rows and this_excl_tax > 0 and orig_excl_tax > 0:
        allocated = Decimal("0")
        for idx, t in enumerate(excl_rows):
            if idx == len(excl_rows) - 1:
                amt = this_excl_tax - allocated
            else:
                amt = _q(this_excl_tax * _dec(_field(t, "tax_amount", 0)) / orig_excl_tax, precision)
                allocated += amt
            tax_rows_out.append({
                "charge_type": _field(t, "charge_type") or "Actual",
                "account_head": _field(t, "account_head"),
                "description": _field(t, "description") or _field(t, "account_head"),
                "rate": float(_field(t, "rate") or 0),
                "tax_amount": -float(amt),
                "included_in_print_rate": 0,
            })

    # Refund payment cap: never hand back more cash/tender than was actually paid!
    orig_net_payments = _net_original_payments(orig, precision)
    orig_net_paid = sum(p["amount"] for p in orig_net_payments)
    remaining_paid = max(Decimal("0"), orig_net_paid - prior_paid_refunded)
    max_cash_refundable = min(refund_total, remaining_paid)
    unpaid_credit_reduction = max(Decimal("0"), refund_total - max_cash_refundable)

    default_payments = _default_refund_payments_for_amount(orig, prior_returns, max_cash_refundable, precision)
    is_room_charge = _is_room_charge_invoice(orig)

    # Validate caller-supplied refund_payments if given; else use default same-tender payments.
    if refund_payments_req is not None:
        parsed_pay = _parse_json_arg(refund_payments_req)
        if not isinstance(parsed_pay, list):
            _refuse(CODE_RETURN_TENDER_MISMATCH, "Refund payments must be a list.", fr.ValidationError)
        orig_modes = {p["mode_of_payment"] for p in orig_net_payments}
        profile_modes = (
            {_field(p, "mode_of_payment") for p in (_field(pos_profile_doc, "payments") or []) if _field(p, "mode_of_payment")}
            if pos_profile_doc is not None
            else set()
        )
        normalized_payments: list[dict[str, float]] = []
        pay_sum = Decimal("0")
        for p in parsed_pay:
            mop = p.get("mode_of_payment") if isinstance(p, dict) else None
            amt = _q(_dec(p.get("amount", 0) if isinstance(p, dict) else 0), precision)
            if not isinstance(mop, str) or not mop.strip() or amt <= 0:
                _refuse(CODE_RETURN_TENDER_MISMATCH, "Each refund tender must have a valid payment method and positive amount.", fr.ValidationError)
            mop = mop.strip()
            if is_room_charge and not _is_room_charge_mode(mop):
                _refuse(
                    CODE_RETURN_ROOM_CHARGE,
                    "A room charge check can only be refunded back to the room folio, not cash or card.",
                    fr.ValidationError,
                )
            if mop not in orig_modes:
                if cint(allow_different_tender) != 1:
                    _refuse(
                        CODE_RETURN_DIFFERENT_TENDER,
                        "Refund must use the original payment method unless a manager approves a different tender.",
                        fr.ValidationError,
                    )
                if profile_modes and mop not in profile_modes:
                    _refuse(
                        CODE_RETURN_DIFFERENT_TENDER,
                        f"Payment method '{mop}' is not enabled on this till.",
                        fr.ValidationError,
                    )
            pay_sum += amt
            normalized_payments.append({"mode_of_payment": mop, "amount": float(amt)})

        if pay_sum > max_cash_refundable + Decimal("1e-9"):
            _refuse(
                CODE_RETURN_OVER_PAID,
                f"Refund tenders ({pay_sum}) cannot exceed the paid amount available to refund ({max_cash_refundable}).",
                fr.ValidationError,
            )
        if abs(pay_sum - max_cash_refundable) > Decimal("1e-9"):
            _refuse(
                CODE_RETURN_TENDER_MISMATCH,
                f"Refund tenders ({pay_sum}) must equal the refundable paid amount ({max_cash_refundable}).",
                fr.ValidationError,
            )
        final_payments = normalized_payments
    else:
        final_payments = default_payments

    return {
        "currency": currency,
        "precision": precision,
        "items": selected_lines,
        "subtotal": float(_q(this_gross, precision)),
        "discount_amount": float(this_discount),
        "tax_amount": float(this_excl_tax),
        "refund_total": float(refund_total),
        "max_cash_refundable": float(max_cash_refundable),
        "unpaid_credit_reduction": float(unpaid_credit_reduction),
        "default_refund_payments": default_payments,
        "refund_payments": final_payments,
        "tax_rows": tax_rows_out,
        "is_room_charge": is_room_charge,
        "is_fiscalised": is_fiscalised_invoice(orig),
    }


def _resolve_efris_credit_reason(reason_code: str, explicit_code: str | None = None) -> str:
    if isinstance(explicit_code, str) and explicit_code.strip():
        return explicit_code.strip()
    return EFRIS_REASON_MAP.get((reason_code or "").strip().upper(), DEFAULT_EFRIS_CREDIT_REASON)


@_default_frappe.whitelist()
def get_returnable_check(invoice_id: str) -> dict[str, Any]:
    """Return a closed check's remaining returnable lines, tenders, and flags for the till UI."""
    fr = _f()
    orig = fr.get_doc("POS Invoice", invoice_id)
    _assert_same_outlet(_field(orig, "branch"))
    if cint(_field(orig, "docstatus", 0)) != 1:
        _refuse(CODE_RETURN_NOT_CLOSED, "Only closed checks can be returned.", fr.ValidationError)
    if cint(_field(orig, "is_return", 0)) == 1:
        _refuse(CODE_RETURN_OF_RETURN, "A return check cannot be returned again.", fr.ValidationError)
    if _field(orig, "custom_merged_pos_invoice"):
        _refuse(CODE_RETURN_MERGED, "Legacy linked merged checks cannot be returned individually.", fr.ValidationError)
    _assert_not_consolidated_or_day_closed(orig)

    prior_returns = _load_prior_returns(orig.name, for_update=False)
    prior_qty = _prior_returned_qty_by_line(orig, prior_returns)
    currency = _field(orig, "currency") or "UGX"
    precision = _currency_precision(currency)
    orig_net_payments = _net_original_payments(orig, precision)
    orig_net_paid = sum(p["amount"] for p in orig_net_payments)
    prior_paid_refunded = sum(
        abs(_dec(_field(p, "amount", 0)))
        for ret in prior_returns
        for p in (_field(ret, "payments") or [])
    )

    items_out = []
    for row in _field(orig, "items") or []:
        rname = _field(row, "name")
        sold = _dec(_field(row, "qty", 0))
        returned = prior_qty.get(rname, Decimal("0"))
        rem = max(Decimal("0"), sold - returned)
        items_out.append({
            "item_row_name": rname,
            "item_code": _field(row, "item_code"),
            "item_name": _field(row, "item_name") or _field(row, "item_code"),
            "sold_qty": float(sold),
            "returned_qty": float(returned),
            "remaining_qty": float(rem),
            "rate": float(_field(row, "rate", 0)),
            "amount": float(_field(row, "amount", 0)),
        })

    profile_name = _safe_profile_name(_field(orig, "pos_profile"))
    profile_doc = fr.get_doc("POS Profile", profile_name) if profile_name and fr.db.exists("POS Profile", profile_name) else None
    profile_modes = [
        _field(p, "mode_of_payment")
        for p in (_field(profile_doc, "payments") or [])
        if _field(p, "mode_of_payment")
    ]
    return {
        "invoice": orig.name,
        "pos_profile": profile_name,
        "branch": _field(orig, "branch"),
        "customer": _field(orig, "customer"),
        "currency": currency,
        "precision": precision,
        "grand_total": float(_field(orig, "rounded_total") or _field(orig, "grand_total") or 0),
        "discount_amount": float(_field(orig, "discount_amount") or 0),
        "total_taxes_and_charges": float(_field(orig, "total_taxes_and_charges") or 0),
        "net_paid_amount": float(orig_net_paid),
        "already_refunded_amount": float(prior_paid_refunded),
        "remaining_refundable_amount": float(max(Decimal("0"), orig_net_paid - prior_paid_refunded)),
        "original_payments": [{"mode_of_payment": p["mode_of_payment"], "amount": float(p["amount"])} for p in orig_net_payments],
        "available_modes": profile_modes,
        "is_room_charge": _is_room_charge_invoice(orig),
        "room_number": _field(orig, "room_number"),
        "is_fiscalised": is_fiscalised_invoice(orig),
        "items": items_out,
        "reasons": _active_pos_reasons("return"),
    }


@_default_frappe.whitelist()
def preview_pos_return(
    original_invoice: str,
    items: Any,
    refund_payments: Any = None,
    allow_different_tender: int = 0,
) -> dict[str, Any]:
    """Read-only calculation of proportional discount, tax, and default refund tenders."""
    fr = _f()
    orig = fr.get_doc("POS Invoice", original_invoice)
    _assert_same_outlet(_field(orig, "branch"))
    if cint(_field(orig, "docstatus", 0)) != 1:
        _refuse(CODE_RETURN_NOT_CLOSED, "Only closed checks can be returned.", fr.ValidationError)
    if cint(_field(orig, "is_return", 0)) == 1:
        _refuse(CODE_RETURN_OF_RETURN, "A return check cannot be returned again.", fr.ValidationError)
    if _field(orig, "custom_merged_pos_invoice"):
        _refuse(CODE_RETURN_MERGED, "Legacy linked merged checks cannot be returned individually.", fr.ValidationError)
    _assert_not_consolidated_or_day_closed(orig)

    profile_name = _safe_profile_name(_field(orig, "pos_profile"))
    profile_doc = fr.get_doc("POS Profile", profile_name) if profile_name and fr.db.exists("POS Profile", profile_name) else None
    prior_returns = _load_prior_returns(orig.name, for_update=False)
    plan = _compute_return_plan(
        orig,
        prior_returns,
        items,
        refund_payments_req=refund_payments,
        allow_different_tender=cint(allow_different_tender),
        pos_profile_doc=profile_doc,
    )
    return {
        "original_invoice": orig.name,
        "currency": plan["currency"],
        "precision": plan["precision"],
        "items": [{k: v for k, v in ln.items() if k != "orig_row"} for ln in plan["items"]],
        "subtotal": plan["subtotal"],
        "discount_amount": plan["discount_amount"],
        "tax_amount": plan["tax_amount"],
        "refund_total": plan["refund_total"],
        "max_cash_refundable": plan["max_cash_refundable"],
        "unpaid_credit_reduction": plan["unpaid_credit_reduction"],
        "default_refund_payments": plan["default_refund_payments"],
        "refund_payments": plan["refund_payments"],
        "is_room_charge": plan["is_room_charge"],
        "is_fiscalised": plan["is_fiscalised"],
    }


@_default_frappe.whitelist()
def create_pos_return(
    original_invoice: str,
    items: Any,
    reason: Any,
    note: Any = None,
    refund_payments: Any = None,
    allow_different_tender: int = 0,
    approval_token: str | None = None,
    efris_reason_code: str | None = None,
    offline_captured: int = 0,
) -> dict[str, Any]:
    """Create and submit a native ERPNext POS Return against a closed check (Matrix row 9)."""
    fr = _f()
    if cint(offline_captured) == 1:
        _refuse(CODE_OFFLINE_NOT_ALLOWED, "Returns and refunds cannot be processed offline.", fr.ValidationError)

    # Lock original check and prior returns in deterministic order to block concurrent double-return
    # and concurrent day-close consolidation races.
    orig = fr.get_doc("POS Invoice", original_invoice, for_update=True)
    _assert_same_outlet(_field(orig, "branch"))
    if cint(_field(orig, "docstatus", 0)) != 1:
        _refuse(CODE_RETURN_NOT_CLOSED, "Only closed checks can be returned.", fr.ValidationError)
    if cint(_field(orig, "is_return", 0)) == 1:
        _refuse(CODE_RETURN_OF_RETURN, "A return check cannot be returned again.", fr.ValidationError)
    if _field(orig, "custom_merged_pos_invoice"):
        _refuse(CODE_RETURN_MERGED, "Legacy linked merged checks cannot be returned individually.", fr.ValidationError)

    # Re-check consolidation & day close AFTER acquiring FOR UPDATE lock.
    _assert_not_consolidated_or_day_closed(orig)
    prior_returns = _load_prior_returns(orig.name, for_update=True)

    profile_name = _safe_profile_name(_field(orig, "pos_profile"))
    profile_doc = fr.get_doc("POS Profile", profile_name) if profile_name and fr.db.exists("POS Profile", profile_name) else None

    plan = _compute_return_plan(
        orig,
        prior_returns,
        items,
        refund_payments_req=refund_payments,
        allow_different_tender=cint(allow_different_tender),
        pos_profile_doc=profile_doc,
    )

    reason_info = resolve_pos_reason("return", profile_name, reason, note=note)

    session_user = getattr(getattr(fr, "session", None), "user", None) or ""
    check_actors = {
        a for a in (_field(orig, "owner"), _field(orig, "cashier"), _field(orig, "waiter"))
        if isinstance(a, str) and a.strip()
    }
    is_own_check = bool(session_user and session_user in check_actors)

    claim = {
        "original_invoice": orig.name,
        "items": sorted(
            [
                {
                    "item_row_name": ln["item_row_name"],
                    "item_code": ln["item_code"],
                    "qty": float(ln["qty"]),
                }
                for ln in plan["items"]
            ],
            key=lambda x: (x["item_row_name"], x["item_code"]),
        ),
        "refund_total": float(plan["refund_total"]),
        "currency": plan["currency"],
        "refund_payments": sorted(
            [{"mode_of_payment": p["mode_of_payment"], "amount": float(p["amount"])} for p in plan["refund_payments"]],
            key=lambda x: x["mode_of_payment"],
        ),
        "allow_different_tender": 1 if cint(allow_different_tender) == 1 else 0,
        "reason": reason_info["code"],
        "note": reason_info["note"],
    }

    # Four-eyes: never return own check alone; even an approving manager cannot be an original check actor.
    approver = require_pos_override(
        "return",
        profile_name,
        "POS Invoice",
        orig.name,
        claim,
        approval_token=approval_token,
        amount=float(plan["refund_total"]),
        forbid_solo=is_own_check,
    )
    if approval_token and approver in check_actors:
        _refuse(
            CODE_RETURN_OWN_CHECK,
            "The person who served or settled this check cannot approve its return.",
            fr.PermissionError,
        )

    ret = fr.new_doc("POS Invoice")
    ret.is_return = 1
    ret.return_against = orig.name
    ret.company = _field(orig, "company")
    ret.branch = _field(orig, "branch")
    ret.pos_profile = profile_name
    ret.customer = _field(orig, "customer")
    ret.customer_name = _field(orig, "customer_name") or _field(orig, "customer")
    ret.currency = plan["currency"]
    ret.selling_price_list = _field(orig, "selling_price_list")
    ret.posting_date = _today_date()
    ret.posting_time = _now_time()
    ret.set_posting_time = 1
    ret.invoice_printed = 0
    ret.restaurant_table = None
    ret.cashier = session_user
    ret.waiter = session_user
    ret.update_billed_amount_in_sales_order = 0
    ret.update_billed_amount_in_delivery_note = 1
    if plan["is_room_charge"] and _field(orig, "room_number"):
        ret.room_number = _field(orig, "room_number")

    for ln in plan["items"]:
        orow = ln["orig_row"]
        ret.append("items", {
            "item_code": ln["item_code"],
            "item_name": ln["item_name"],
            "qty": -float(ln["qty"]),
            "rate": float(ln["rate"]),
            "uom": _field(orow, "uom") or "Nos",
            "warehouse": _field(orow, "warehouse") or (_field(profile_doc, "warehouse") if profile_doc else None),
            "income_account": _field(orow, "income_account"),
            "cost_center": _field(orow, "cost_center") or (_field(profile_doc, "cost_center") if profile_doc else None),
            "pos_invoice_item": ln["item_row_name"],
            "ury_held": 0,
        })

    if plan["discount_amount"] > 0:
        ret.apply_discount_on = _field(orig, "apply_discount_on") or "Grand Total"
        ret.discount_amount = -float(plan["discount_amount"])

    for trow in plan["tax_rows"]:
        ret.append("taxes", trow)

    for prow in plan["refund_payments"]:
        ret.append("payments", {
            "mode_of_payment": prow["mode_of_payment"],
            "amount": -float(prow["amount"]),
        })

    efris_code = None
    if plan["is_fiscalised"] or _has_field("POS Invoice", "efris_creditnote_reasoncode"):
        efris_code = _resolve_efris_credit_reason(reason_info["code"], efris_reason_code)
        if _has_field("POS Invoice", "efris_creditnote_reasoncode"):
            ret.efris_creditnote_reasoncode = efris_code
        if _has_field("POS Invoice", "efris_creditnote_reason"):
            ret.efris_creditnote_reason = reason_info["stored"]

    if _has_field("POS Invoice", "custom_return_approver"):
        ret.custom_return_approver = approver
    if _has_field("POS Invoice", "cancel_reason"):
        ret.cancel_reason = reason_info["stored"]

    mark_verified_pos_approval(ret, "return")
    ret.insert(ignore_permissions=True)
    ret.submit()

    return {
        "return_invoice": ret.name,
        "original_invoice": orig.name,
        "currency": plan["currency"],
        "subtotal": plan["subtotal"],
        "discount_amount": plan["discount_amount"],
        "tax_amount": plan["tax_amount"],
        "refund_total": plan["refund_total"],
        "max_cash_refundable": plan["max_cash_refundable"],
        "unpaid_credit_reduction": plan["unpaid_credit_reduction"],
        "refund_payments": plan["refund_payments"],
        "approver": approver,
        "reason": reason_info["code"],
        "is_room_charge": plan["is_room_charge"],
        "is_fiscalised": plan["is_fiscalised"],
        "efris_creditnote_reasoncode": efris_code,
    }


@_default_frappe.whitelist()
def create_unreferenced_return(
    pos_profile: str,
    customer: str,
    items: Any,
    refund_payments: Any,
    reason: Any,
    note: Any = None,
    approval_token: str | None = None,
    offline_captured: int = 0,
) -> dict[str, Any]:
    """Create a standalone return without an original check — default DENIED unless dedicated roles configured."""
    fr = _f()
    if cint(offline_captured) == 1:
        _refuse(CODE_OFFLINE_NOT_ALLOWED, "Unreferenced returns cannot be processed offline.", fr.ValidationError)

    profile_name = _safe_profile_name(pos_profile)
    if not profile_name or not fr.db.exists("POS Profile", profile_name):
        _refuse(CODE_RETURN_INVALID_LINE, "Select a valid POS Profile.", fr.ValidationError)
    profile_doc = fr.get_doc("POS Profile", profile_name)
    _assert_same_outlet(_field(profile_doc, "branch"))

    # Dedicated privilege gate: default-empty `custom_unreferenced_return_roles` denies EVERYONE!
    allowed_roles = _unreferenced_return_roles(profile_doc)
    if not allowed_roles:
        _refuse(
            CODE_RETURN_UNREFERENCED_NOT_ALLOWED,
            "Unreferenced returns are disabled on this till. Configure dedicated approval roles on the POS Profile.",
            fr.PermissionError,
        )

    parsed_items = _parse_json_arg(items)
    if not isinstance(parsed_items, list) or not parsed_items:
        _refuse(CODE_RETURN_INVALID_LINE, "Add at least one item to return.", fr.ValidationError)

    currency = _field(profile_doc, "currency") or "UGX"
    precision = _currency_precision(currency)
    price_list = _field(profile_doc, "selling_price_list") or "Standard Selling"

    normalized_items = []
    total_refund = Decimal("0")
    for entry in parsed_items:
        code = entry.get("item_code") if isinstance(entry, dict) else None
        qty_d = _dec(entry.get("qty", 0) if isinstance(entry, dict) else 0)
        if not isinstance(code, str) or not code.strip() or qty_d <= 0:
            _refuse(CODE_RETURN_INVALID_LINE, "Each unreferenced return line needs a valid item and positive quantity.", fr.ValidationError)
        code = code.strip()
        item_info = fr.db.get_value("Item", code, ["item_name", "stock_uom"], as_dict=True)
        if not item_info:
            _refuse(CODE_RETURN_INVALID_LINE, f"Item '{code}' was not found.", fr.ValidationError)
        rate_val = fr.db.get_value("Item Price", {"item_code": code, "price_list": price_list}, "price_list_rate")
        rate_d = _q(_dec(rate_val if rate_val is not None else entry.get("rate", 0)), precision)
        if rate_d <= 0:
            _refuse(CODE_RETURN_INVALID_LINE, f"Item '{code}' has no valid selling price.", fr.ValidationError)
        total_refund += _q(qty_d * rate_d, precision)
        normalized_items.append({
            "item_code": code,
            "item_name": _field(item_info, "item_name") or code,
            "uom": _field(item_info, "stock_uom") or "Nos",
            "qty": float(qty_d),
            "rate": float(rate_d),
        })

    parsed_pay = _parse_json_arg(refund_payments)
    if not isinstance(parsed_pay, list) or not parsed_pay:
        _refuse(CODE_RETURN_TENDER_MISMATCH, "Select a refund payment method.", fr.ValidationError)
    profile_modes = {_field(p, "mode_of_payment") for p in (_field(profile_doc, "payments") or []) if _field(p, "mode_of_payment")}
    normalized_pay = []
    pay_sum = Decimal("0")
    for p in parsed_pay:
        mop = p.get("mode_of_payment") if isinstance(p, dict) else None
        amt = _q(_dec(p.get("amount", 0) if isinstance(p, dict) else 0), precision)
        if not isinstance(mop, str) or not mop.strip() or amt <= 0:
            _refuse(CODE_RETURN_TENDER_MISMATCH, "Each refund tender must have a valid payment method and positive amount.", fr.ValidationError)
        mop = mop.strip()
        if profile_modes and mop not in profile_modes:
            _refuse(CODE_RETURN_DIFFERENT_TENDER, f"Payment method '{mop}' is not enabled on this till.", fr.ValidationError)
        pay_sum += amt
        normalized_pay.append({"mode_of_payment": mop, "amount": float(amt)})
    if abs(pay_sum - total_refund) > Decimal("1e-9"):
        _refuse(CODE_RETURN_TENDER_MISMATCH, "Refund tenders must equal the unreferenced return total.", fr.ValidationError)

    reason_info = resolve_pos_reason("return", profile_name, reason, note=note)
    claim = {
        "unreferenced": 1,
        "pos_profile": profile_name,
        "customer": customer,
        "items": sorted(
            [{"item_code": i["item_code"], "qty": i["qty"], "rate": i["rate"]} for i in normalized_items],
            key=lambda x: x["item_code"],
        ),
        "refund_total": float(total_refund),
        "currency": currency,
        "refund_payments": sorted(normalized_pay, key=lambda x: x["mode_of_payment"]),
        "reason": reason_info["code"],
        "note": reason_info["note"],
    }

    # Unreferenced returns ALWAYS require a distinct manager PIN (`forbid_solo=True`), AND the
    # approving manager MUST hold one of the profile's configured `custom_unreferenced_return_roles`!
    approver = require_pos_override(
        "return",
        profile_name,
        "POS Profile",
        profile_name,
        claim,
        approval_token=approval_token,
        amount=float(total_refund),
        forbid_solo=True,
    )
    approver_roles = _user_roles(approver)
    if not (approver_roles & allowed_roles):
        _refuse(
            CODE_RETURN_UNREFERENCED_NOT_ALLOWED,
            "Approving manager does not hold the dedicated privilege for unreferenced returns.",
            fr.PermissionError,
        )

    session_user = getattr(getattr(fr, "session", None), "user", None) or ""
    ret = fr.new_doc("POS Invoice")
    ret.is_return = 1
    ret.return_against = None
    ret.company = _field(profile_doc, "company")
    ret.branch = _field(profile_doc, "branch")
    ret.pos_profile = profile_name
    ret.customer = customer
    ret.currency = currency
    ret.selling_price_list = price_list
    ret.posting_date = _today_date()
    ret.posting_time = _now_time()
    ret.set_posting_time = 1
    ret.invoice_printed = 0
    ret.restaurant_table = None
    ret.cashier = session_user
    ret.waiter = session_user
    for i in normalized_items:
        ret.append("items", {
            "item_code": i["item_code"],
            "item_name": i["item_name"],
            "qty": -float(i["qty"]),
            "rate": float(i["rate"]),
            "uom": i["uom"],
            "warehouse": _field(profile_doc, "warehouse"),
            "cost_center": _field(profile_doc, "cost_center"),
            "ury_held": 0,
        })
    for prow in normalized_pay:
        ret.append("payments", {
            "mode_of_payment": prow["mode_of_payment"],
            "amount": -float(prow["amount"]),
        })
    if _has_field("POS Invoice", "custom_return_approver"):
        ret.custom_return_approver = approver
    if _has_field("POS Invoice", "cancel_reason"):
        ret.cancel_reason = reason_info["stored"]

    mark_verified_pos_approval(ret, "return")
    ret.insert(ignore_permissions=True)
    ret.submit()

    return {
        "return_invoice": ret.name,
        "unreferenced": True,
        "currency": currency,
        "refund_total": float(total_refund),
        "refund_payments": normalized_pay,
        "approver": approver,
        "reason": reason_info["code"],
    }


def validate_pos_return_doc(doc) -> None:
    """Mutation-level POS Invoice validation hook for `is_return=1` (blocks REST/Desk bypass)."""
    fr = _f()
    if getattr(fr.flags, "in_patch", False) or getattr(fr.flags, "in_migrate", False):
        return

    # Always keep return checks detached from tables and unprinted on creation.
    if doc.is_new():
        doc.restaurant_table = None
        doc.invoice_printed = 0

    ov = _override_module()
    local_proof = getattr(getattr(doc, "flags", None), "_pos_verified_return_proof", None)
    ov_verified = False
    if ov is not None and callable(getattr(ov, "has_verified_approval", None)):
        try:
            ov_verified = bool(ov.has_verified_approval(doc, "return"))
        except AttributeError:
            ov_verified = False
    has_verified = bool(
        (isinstance(local_proof, _VerifiedNewReturnProof) and local_proof.doc is doc and local_proof.privilege == "return")
        or ov_verified
    )
    actor = getattr(getattr(fr, "session", None), "user", None) or ""

    return_against = _field(doc, "return_against")
    if return_against:
        orig = fr.get_doc("POS Invoice", return_against)
        if _field(orig, "custom_merged_pos_invoice"):
            _refuse(CODE_RETURN_MERGED, "Legacy linked merged checks cannot be returned individually.", fr.ValidationError)
        _assert_not_consolidated_or_day_closed(orig)
        prior_returns = [
            r for r in _load_prior_returns(orig.name, for_update=False)
            if _field(r, "name") != _field(doc, "name")
        ]
        prior_qty = _prior_returned_qty_by_line(orig, prior_returns)
        orig_by_name = {_field(r, "name"): r for r in (_field(orig, "items") or []) if _field(r, "name")}
        req_by_name: dict[str, Decimal] = {}
        for row in _field(doc, "items") or []:
            qty_abs = abs(_dec(_field(row, "qty", 0)))
            ref = _field(row, "pos_invoice_item")
            if ref and ref in orig_by_name:
                req_by_name[ref] = req_by_name.get(ref, Decimal("0")) + qty_abs
        for ref, req_qty in req_by_name.items():
            sold = _dec(_field(orig_by_name[ref], "qty", 0))
            rem = sold - prior_qty.get(ref, Decimal("0"))
            if req_qty > rem + Decimal("1e-9"):
                _refuse(
                    CODE_RETURN_OVER_QTY,
                    f"Return quantity ({req_qty}) exceeds remaining returnable quantity ({rem}).",
                    fr.ValidationError,
                )
        if not has_verified and actor != "Administrator":
            check_actors = {
                a for a in (_field(orig, "owner"), _field(orig, "cashier"), _field(orig, "waiter"))
                if isinstance(a, str) and a.strip()
            }
            if actor in check_actors:
                _refuse(CODE_AUTH_REQUIRED, "You cannot approve a return on your own check.", fr.PermissionError)
    else:
        # Unreferenced return: ALWAYS blocked unless profile grants dedicated roles AND verified approval!
        raw_actor = getattr(getattr(fr, "session", None), "user", None)
        if not isinstance(raw_actor, str) and not _field(doc, "company"):
            return
        profile_name = _safe_profile_name(_field(doc, "pos_profile"))
        raw_roles = (
            fr.db.get_value("POS Profile", profile_name, "custom_unreferenced_return_roles")
            if profile_name
            else ""
        )
        allowed_roles = (
            {part.strip() for part in raw_roles.replace(",", "\n").splitlines() if part.strip()}
            if isinstance(raw_roles, str) and raw_roles.strip()
            else set()
        )
        if not allowed_roles or (not has_verified and actor != "Administrator"):
            _refuse(
                CODE_RETURN_UNREFERENCED_NOT_ALLOWED,
                "Unreferenced returns require dedicated POS Profile roles and manager PIN approval.",
                fr.PermissionError,
            )


# ---------------------------------------------------------------------------
# Slice 3: Reopen / Adjust Closed Check (Matrix row 10, Simphony Opt 67/68, SAP FP17)
# ---------------------------------------------------------------------------


def _assert_shift_still_open(orig) -> None:
    fr = _f()
    orig_name = _field(orig, "name")
    if isinstance(orig_name, str) and orig_name and fr.db.table_exists("POS Invoice Reference") is True:
        close_rows = fr.db.sql(
            """
            SELECT pce.name
            FROM `tabPOS Closing Entry` pce
            INNER JOIN `tabPOS Invoice Reference` pir
                ON pir.parent = pce.name AND pir.parenttype = 'POS Closing Entry'
            WHERE pce.docstatus = 1 AND pir.pos_invoice = %s
            LIMIT 1
            """,
            (orig_name,),
        )
        if close_rows:
            _refuse(
                CODE_REOPEN_SHIFT_CLOSED,
                "The shift for this check has already closed.",
                fr.ValidationError,
            )
    profile_name = _safe_profile_name(_field(orig, "pos_profile"))
    if profile_name and fr.db.table_exists("POS Opening Entry") is True:
        open_entry = fr.db.exists(
            "POS Opening Entry",
            {"pos_profile": profile_name, "docstatus": 1, "status": "Open"},
        )
        if not open_entry:
            _refuse(
                CODE_REOPEN_SHIFT_CLOSED,
                "An open shift on this till is required to reopen or adjust a closed check.",
                fr.ValidationError,
            )


def _validate_reopen_or_adjust_base(orig, *, credit_and_rebill: int = 0) -> bool:
    """Enforce shared same-day / open-shift / unconsolidated / unreturned / fiscal limits."""
    fr = _f()
    _assert_same_outlet(_field(orig, "branch"))
    if cint(_field(orig, "docstatus", 0)) != 1:
        _refuse(CODE_REOPEN_NOT_CLOSED, "Only closed checks can be reopened or adjusted.", fr.ValidationError)
    if cint(_field(orig, "is_return", 0)) == 1:
        _refuse(CODE_REOPEN_OF_RETURN, "A return check cannot be reopened or adjusted.", fr.ValidationError)
    if _field(orig, "custom_merged_pos_invoice"):
        _refuse(CODE_RETURN_MERGED, "Legacy linked merged checks cannot be reopened individually.", fr.ValidationError)

    # Consolidation check
    if _field(orig, "consolidated_invoice") or str(_field(orig, "status") or "") == "Consolidated":
        _refuse(
            CODE_REOPEN_CONSOLIDATED,
            "This check has already been consolidated and cannot be reopened or adjusted.",
            fr.ValidationError,
        )
    if fr.db.table_exists("POS Invoice Reference") is True:
        merge_rows = fr.db.sql(
            """
            SELECT piml.name
            FROM `tabPOS Invoice Merge Log` piml
            INNER JOIN `tabPOS Invoice Reference` pir
                ON pir.parent = piml.name AND pir.parenttype = 'POS Invoice Merge Log'
            WHERE piml.docstatus = 1 AND pir.pos_invoice = %s
            LIMIT 1
            """,
            (orig.name,),
        )
        if merge_rows:
            _refuse(
                CODE_REOPEN_CONSOLIDATED,
                "This check is already consolidated and cannot be reopened or adjusted.",
                fr.ValidationError,
            )

    # Same business day only
    orig_day = str(_field(orig, "posting_date") or "")[:10]
    if orig_day != _today_date():
        _refuse(
            CODE_REOPEN_PREVIOUS_DAY,
            "Only checks from the current business day can be reopened or adjusted.",
            fr.ValidationError,
        )

    # Shift still open
    _assert_shift_still_open(orig)

    # Not already returned
    prior_returns = _load_prior_returns(orig.name, for_update=True)
    if prior_returns:
        _refuse(
            CODE_REOPEN_ALREADY_RETURNED,
            "This check already has a return and cannot be reopened or adjusted.",
            fr.ValidationError,
        )

    # Fiscalised check requires explicit credit-and-rebill confirmation
    is_fiscal = is_fiscalised_invoice(orig)
    if is_fiscal and cint(credit_and_rebill) != 1:
        _refuse(
            CODE_REOPEN_FISCALISED,
            "This check was fiscalised. Confirm credit and rebill to issue an EFRIS credit note and replacement check.",
            fr.ValidationError,
        )
    return is_fiscal


def _build_full_reversal_return(orig, reason_info: dict[str, Any], approver: str, is_fiscal: bool):
    """Create and submit a full reversal POS Return against `orig`."""
    fr = _f()
    currency = _field(orig, "currency") or "UGX"
    precision = _currency_precision(currency)
    session_user = getattr(getattr(fr, "session", None), "user", None) or ""
    orig_net_payments = _net_original_payments(orig, precision)

    rev = fr.new_doc("POS Invoice")
    rev.is_return = 1
    rev.return_against = orig.name
    rev.company = _field(orig, "company")
    rev.branch = _field(orig, "branch")
    rev.pos_profile = _field(orig, "pos_profile")
    rev.customer = _field(orig, "customer")
    rev.customer_name = _field(orig, "customer_name") or _field(orig, "customer")
    rev.currency = currency
    rev.selling_price_list = _field(orig, "selling_price_list")
    rev.posting_date = _today_date()
    rev.posting_time = _now_time()
    rev.set_posting_time = 1
    rev.invoice_printed = 0
    rev.restaurant_table = None
    rev.cashier = session_user
    rev.waiter = session_user
    if _field(orig, "room_number"):
        rev.room_number = _field(orig, "room_number")

    for row in _field(orig, "items") or []:
        qty = flt(_field(row, "qty", 0))
        if qty <= 0:
            continue
        rev.append("items", {
            "item_code": _field(row, "item_code"),
            "item_name": _field(row, "item_name") or _field(row, "item_code"),
            "qty": -qty,
            "rate": flt(_field(row, "rate", 0)),
            "uom": _field(row, "uom") or "Nos",
            "warehouse": _field(row, "warehouse"),
            "income_account": _field(row, "income_account"),
            "cost_center": _field(row, "cost_center"),
            "pos_invoice_item": _field(row, "name"),
            "ury_held": 0,
        })

    disc = abs(flt(_field(orig, "discount_amount", 0)))
    if disc > 0:
        rev.apply_discount_on = _field(orig, "apply_discount_on") or "Grand Total"
        rev.discount_amount = -disc

    for t in _field(orig, "taxes") or []:
        if cint(_field(t, "included_in_print_rate", 0)) == 0:
            rev.append("taxes", {
                "charge_type": _field(t, "charge_type") or "Actual",
                "account_head": _field(t, "account_head"),
                "description": _field(t, "description") or _field(t, "account_head"),
                "rate": flt(_field(t, "rate", 0)),
                "tax_amount": -abs(flt(_field(t, "tax_amount", 0))),
                "included_in_print_rate": 0,
            })

    for p in orig_net_payments:
        rev.append("payments", {
            "mode_of_payment": p["mode_of_payment"],
            "amount": -float(p["amount"]),
        })

    if is_fiscal or _has_field("POS Invoice", "efris_creditnote_reasoncode"):
        efris_code = _resolve_efris_credit_reason(reason_info["code"])
        if _has_field("POS Invoice", "efris_creditnote_reasoncode"):
            rev.efris_creditnote_reasoncode = efris_code
        if _has_field("POS Invoice", "efris_creditnote_reason"):
            rev.efris_creditnote_reason = reason_info["stored"]

    if _has_field("POS Invoice", "custom_return_approver"):
        rev.custom_return_approver = approver
    if _has_field("POS Invoice", "cancel_reason"):
        rev.cancel_reason = reason_info["stored"]

    mark_verified_pos_approval(rev, "return")
    rev.insert(ignore_permissions=True)
    rev.submit()
    return rev


def _clone_check_lines_and_taxes(src, dst) -> None:
    for row in _field(src, "items") or []:
        qty = flt(_field(row, "qty", 0))
        if qty <= 0:
            continue
        dst.append("items", {
            "item_code": _field(row, "item_code"),
            "item_name": _field(row, "item_name") or _field(row, "item_code"),
            "qty": qty,
            "rate": flt(_field(row, "rate", 0)),
            "uom": _field(row, "uom") or "Nos",
            "warehouse": _field(row, "warehouse"),
            "income_account": _field(row, "income_account"),
            "cost_center": _field(row, "cost_center"),
            "ury_held": 0,
        })
    disc = abs(flt(_field(src, "discount_amount", 0)))
    if disc > 0:
        dst.apply_discount_on = _field(src, "apply_discount_on") or "Grand Total"
        dst.discount_amount = disc
    for t in _field(src, "taxes") or []:
        dst.append("taxes", {
            "charge_type": _field(t, "charge_type") or "Actual",
            "account_head": _field(t, "account_head"),
            "description": _field(t, "description") or _field(t, "account_head"),
            "rate": flt(_field(t, "rate", 0)),
            "tax_amount": flt(_field(t, "tax_amount", 0)),
            "included_in_print_rate": cint(_field(t, "included_in_print_rate", 0)),
        })


def _record_before_after_audit(orig, replacement, action: str, before: dict[str, Any], after: dict[str, Any], approver: str, reason_info: dict[str, Any]) -> None:
    summary = (
        f"{action} | Original: {orig.name} -> Replacement: {replacement.name} "
        f"| Reason: {reason_info['stored']} | Approved by: {approver} "
        f"| Before: {json.dumps(before, sort_keys=True, default=str)} "
        f"| After: {json.dumps(after, sort_keys=True, default=str)}"
    )
    for target in (orig, replacement):
        add_comment = getattr(target, "add_comment", None)
        if callable(add_comment):
            add_comment("Info", summary)


@_default_frappe.whitelist()
def adjust_closed_check(
    invoice_id: str,
    payments: Any,
    reason: Any,
    note: Any = None,
    tip_amount: Any = None,
    credit_and_rebill: int = 0,
    approval_token: str | None = None,
    items: Any = None,
    offline_captured: int = 0,
) -> dict[str, Any]:
    """Adjust tender/tip ONLY on a same-day open-shift closed check (Simphony Opt 67)."""
    fr = _f()
    if cint(offline_captured) == 1:
        _refuse(CODE_OFFLINE_NOT_ALLOWED, "Closed checks cannot be adjusted offline.", fr.ValidationError)
    if items is not None:
        _refuse(
            CODE_ADJUST_ITEMS_FORBIDDEN,
            "Items cannot be changed in Adjust Closed Check. Use Reopen Check instead.",
            fr.ValidationError,
        )

    orig = fr.get_doc("POS Invoice", invoice_id, for_update=True)
    is_fiscal = _validate_reopen_or_adjust_base(orig, credit_and_rebill=cint(credit_and_rebill))

    profile_name = _safe_profile_name(_field(orig, "pos_profile"))
    profile_doc = fr.get_doc("POS Profile", profile_name) if profile_name and fr.db.exists("POS Profile", profile_name) else None
    currency = _field(orig, "currency") or "UGX"
    precision = _currency_precision(currency)

    parsed_pay = _parse_json_arg(payments)
    if not isinstance(parsed_pay, list) or not parsed_pay:
        _refuse(CODE_ADJUST_TOTAL_MISMATCH, "Provide at least one replacement payment method.", fr.ValidationError)

    profile_modes = (
        {_field(p, "mode_of_payment") for p in (_field(profile_doc, "payments") or []) if _field(p, "mode_of_payment")}
        if profile_doc is not None
        else set()
    )
    orig_tip = _q(_dec(_field(orig, "tip_amount", 0)), precision)
    new_tip = _q(_dec(tip_amount), precision) if tip_amount is not None else orig_tip
    if new_tip < 0:
        _refuse(CODE_ADJUST_TOTAL_MISMATCH, "Tip amount cannot be negative.", fr.ValidationError)

    orig_grand = _q(_dec(_field(orig, "rounded_total") or _field(orig, "grand_total") or 0), precision)
    expected_pay_total = orig_grand + new_tip

    normalized_payments = []
    pay_sum = Decimal("0")
    for p in parsed_pay:
        mop = p.get("mode_of_payment") if isinstance(p, dict) else None
        amt = _q(_dec(p.get("amount", 0) if isinstance(p, dict) else 0), precision)
        if not isinstance(mop, str) or not mop.strip() or amt <= 0:
            _refuse(CODE_ADJUST_TOTAL_MISMATCH, "Each payment row needs a valid payment method and positive amount.", fr.ValidationError)
        mop = mop.strip()
        if profile_modes and mop not in profile_modes:
            _refuse(CODE_RETURN_DIFFERENT_TENDER, f"Payment method '{mop}' is not enabled on this till.", fr.ValidationError)
        pay_sum += amt
        normalized_payments.append({"mode_of_payment": mop, "amount": float(amt)})

    if abs(pay_sum - expected_pay_total) > Decimal("1e-9"):
        _refuse(
            CODE_ADJUST_TOTAL_MISMATCH,
            f"Adjusted payments ({pay_sum}) must equal check total plus tip ({expected_pay_total}).",
            fr.ValidationError,
        )

    reason_info = resolve_pos_reason("adjust_closed_check", profile_name, reason, note=note)

    claim = {
        "original_invoice": orig.name,
        "payments": sorted(normalized_payments, key=lambda x: x["mode_of_payment"]),
        "tip_amount": float(new_tip),
        "credit_and_rebill": 1 if (is_fiscal and cint(credit_and_rebill) == 1) else 0,
        "reason": reason_info["code"],
        "note": reason_info["note"],
    }
    approver = require_pos_override(
        "adjust_closed_check",
        profile_name,
        "POS Invoice",
        orig.name,
        claim,
        approval_token=approval_token,
        amount=float(expected_pay_total),
    )

    before_snapshot = {
        "invoice": orig.name,
        "grand_total": float(orig_grand),
        "tip_amount": float(orig_tip),
        "payments": [
            {"mode_of_payment": p["mode_of_payment"], "amount": float(p["amount"])}
            for p in _net_original_payments(orig, precision)
        ],
        "status": _field(orig, "status"),
    }

    rev = _build_full_reversal_return(orig, reason_info, approver, is_fiscal)

    session_user = getattr(getattr(fr, "session", None), "user", None) or ""
    rep = fr.new_doc("POS Invoice")
    rep.is_return = 0
    rep.company = _field(orig, "company")
    rep.branch = _field(orig, "branch")
    rep.pos_profile = profile_name
    rep.customer = _field(orig, "customer")
    rep.customer_name = _field(orig, "customer_name") or _field(orig, "customer")
    rep.order_type = _field(orig, "order_type") or "Dine In"
    rep.currency = currency
    rep.selling_price_list = _field(orig, "selling_price_list")
    rep.posting_date = _today_date()
    rep.posting_time = _now_time()
    rep.set_posting_time = 1
    rep.invoice_printed = 1
    rep.restaurant_table = _field(orig, "restaurant_table")
    rep.waiter = _field(orig, "waiter") or session_user
    rep.cashier = session_user
    if _has_field("POS Invoice", "tip_amount"):
        rep.tip_amount = float(new_tip)
    if _has_field("POS Invoice", "custom_reopened_from"):
        rep.custom_reopened_from = orig.name
    if _field(orig, "room_number") and any(_is_room_charge_mode(p["mode_of_payment"]) for p in normalized_payments):
        rep.room_number = _field(orig, "room_number")

    _clone_check_lines_and_taxes(orig, rep)
    for prow in normalized_payments:
        rep.append("payments", {
            "mode_of_payment": prow["mode_of_payment"],
            "amount": float(prow["amount"]),
        })

    rep.insert(ignore_permissions=True)
    rep.submit()

    after_snapshot = {
        "reversal_return": rev.name,
        "replacement_invoice": rep.name,
        "grand_total": float(orig_grand),
        "tip_amount": float(new_tip),
        "payments": normalized_payments,
        "status": _field(rep, "status") or "Paid",
        "credit_and_rebill": bool(is_fiscal and cint(credit_and_rebill) == 1),
    }
    _record_before_after_audit(orig, rep, "Adjust Closed Check", before_snapshot, after_snapshot, approver, reason_info)

    return {
        "action": "adjust_closed_check",
        "original_invoice": orig.name,
        "reversal_return": rev.name,
        "replacement_invoice": rep.name,
        "before": before_snapshot,
        "after": after_snapshot,
        "approver": approver,
        "reason": reason_info["code"],
        "credit_and_rebill": bool(is_fiscal and cint(credit_and_rebill) == 1),
    }


@_default_frappe.whitelist()
def reopen_closed_check(
    invoice_id: str,
    reason: Any,
    note: Any = None,
    credit_and_rebill: int = 0,
    approval_token: str | None = None,
    offline_captured: int = 0,
) -> dict[str, Any]:
    """Reopen a same-day open-shift closed check into an editable Draft (Simphony Opt 68 / SAP FP17)."""
    fr = _f()
    if cint(offline_captured) == 1:
        _refuse(CODE_OFFLINE_NOT_ALLOWED, "Closed checks cannot be reopened offline.", fr.ValidationError)

    orig = fr.get_doc("POS Invoice", invoice_id, for_update=True)
    is_fiscal = _validate_reopen_or_adjust_base(orig, credit_and_rebill=cint(credit_and_rebill))

    profile_name = _safe_profile_name(_field(orig, "pos_profile"))
    currency = _field(orig, "currency") or "UGX"
    precision = _currency_precision(currency)
    orig_grand = _q(_dec(_field(orig, "rounded_total") or _field(orig, "grand_total") or 0), precision)

    reason_info = resolve_pos_reason("reopen_check", profile_name, reason, note=note)
    claim = {
        "original_invoice": orig.name,
        "grand_total": float(orig_grand),
        "credit_and_rebill": 1 if (is_fiscal and cint(credit_and_rebill) == 1) else 0,
        "reason": reason_info["code"],
        "note": reason_info["note"],
    }
    approver = require_pos_override(
        "reopen_check",
        profile_name,
        "POS Invoice",
        orig.name,
        claim,
        approval_token=approval_token,
        amount=float(orig_grand),
    )

    before_snapshot = {
        "invoice": orig.name,
        "grand_total": float(orig_grand),
        "payments": [
            {"mode_of_payment": p["mode_of_payment"], "amount": float(p["amount"])}
            for p in _net_original_payments(orig, precision)
        ],
        "restaurant_table": _field(orig, "restaurant_table"),
        "status": _field(orig, "status"),
    }

    rev = _build_full_reversal_return(orig, reason_info, approver, is_fiscal)

    # Restore original table ONLY if it exists and is currently free; never collide with another guest!
    orig_table = _field(orig, "restaurant_table")
    restored_table = None
    table_restored = False
    if isinstance(orig_table, str) and orig_table.strip() and fr.db.exists("URY Table", orig_table):
        is_occupied = cint(fr.db.get_value("URY Table", orig_table, "occupied") or 0) == 1
        if not is_occupied:
            fr.db.set_value("URY Table", orig_table, "occupied", 1, update_modified=False)
            restored_table = orig_table
            table_restored = True

    session_user = getattr(getattr(fr, "session", None), "user", None) or ""
    reopened = fr.new_doc("POS Invoice")
    reopened.is_return = 0
    reopened.company = _field(orig, "company")
    reopened.branch = _field(orig, "branch")
    reopened.pos_profile = profile_name
    reopened.customer = _field(orig, "customer")
    reopened.customer_name = _field(orig, "customer_name") or _field(orig, "customer")
    reopened.order_type = _field(orig, "order_type") or "Dine In"
    reopened.currency = currency
    reopened.selling_price_list = _field(orig, "selling_price_list")
    reopened.posting_date = _today_date()
    reopened.posting_time = _now_time()
    reopened.set_posting_time = 1
    reopened.invoice_printed = 0
    reopened.restaurant_table = restored_table
    reopened.waiter = _field(orig, "waiter") or session_user
    reopened.cashier = session_user
    reopened.payments = []
    default_mop = None
    orig_pays = _field(orig, "payments") or []
    if orig_pays:
        default_mop = _field(orig_pays[0], "mode_of_payment")
    if not default_mop and profile_name and fr.db.exists("POS Profile", profile_name):
        prof = fr.get_doc("POS Profile", profile_name)
        prof_pays = _field(prof, "payments") or []
        if prof_pays:
            default_mop = _field(prof_pays[0], "mode_of_payment")
    if default_mop:
        # ERPNext v16 requires at least one payment row on POS Invoice drafts (zero-amount unpaid).
        reopened.append("payments", {"mode_of_payment": default_mop, "amount": 0.0})
    if _has_field("POS Invoice", "custom_reopened_from"):
        reopened.custom_reopened_from = orig.name

    _clone_check_lines_and_taxes(orig, reopened)
    reopened.insert(ignore_permissions=True)

    after_snapshot = {
        "reversal_return": rev.name,
        "reopened_invoice": reopened.name,
        "docstatus": 0,
        "restaurant_table": restored_table,
        "table_restored": table_restored,
        "payments": [],
        "credit_and_rebill": bool(is_fiscal and cint(credit_and_rebill) == 1),
    }
    _record_before_after_audit(orig, reopened, "Reopen Closed Check", before_snapshot, after_snapshot, approver, reason_info)

    return {
        "action": "reopen_closed_check",
        "original_invoice": orig.name,
        "reversal_return": rev.name,
        "reopened_invoice": reopened.name,
        "table_restored": table_restored,
        "restaurant_table": restored_table,
        "before": before_snapshot,
        "after": after_snapshot,
        "approver": approver,
        "reason": reason_info["code"],
        "credit_and_rebill": bool(is_fiscal and cint(credit_and_rebill) == 1),
    }
