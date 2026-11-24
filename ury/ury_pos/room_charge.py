"""Server-side room-charge eligibility and duty-manager override gate (matrix row 34).

Calls the POS Profile's configured room-lookup/verifier hook (e.g.
`kamra.pos_interface.verify_room_charge`), fails closed whenever the hook is
missing or raises, hard-refuses non-overridable states (checked-out guest,
house account, wrong property, missing FX rate), and requires a consumed
`room_charge_over_limit` manager approval for over-limit or No Post stays.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt


def _field(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    getter = getattr(obj, "get", None)
    if callable(getter):
        val = getter(key)
        return default if val is None else val
    val = getattr(obj, key, default)
    return default if val is None else val


def _resolve_verifier(method_path: str):
    if not method_path or not isinstance(method_path, str):
        raise ImportError("Room lookup method is not configured.")
    fn = frappe.get_attr(method_path)
    if not callable(fn):
        raise ImportError(f"Room lookup method {method_path} is not callable.")
    return fn


def profile_room_charge_modes(profile, company: str | None = None, *, _frappe=None) -> list[str]:
    f = _frappe or frappe
    comp = company or _field(profile, "company")
    if not comp:
        return []
    meta = f.get_meta("Company")
    if not meta or not meta.has_field("custom_guest_ledger_clearing"):
        return []
    clearing = f.db.get_value("Company", comp, "custom_guest_ledger_clearing")
    if not clearing:
        return []
    modes: list[str] = []
    for row in _field(profile, "payments") or []:
        mode = _field(row, "mode_of_payment")
        if not mode:
            continue
        account = f.db.get_value(
            "Mode of Payment Account",
            {"parent": mode, "company": comp},
            "default_account",
        )
        if account == clearing:
            modes.append(mode)
    return modes


def room_charge_amount(payments, room_charge_modes: list[str]) -> float:
    mode_set = set(room_charge_modes or [])
    if not mode_set:
        return 0.0
    return sum(
        flt(_field(row, "amount"))
        for row in (payments or [])
        if _field(row, "mode_of_payment") in mode_set and flt(_field(row, "amount")) > 0
    )


def enforce_room_charge_settlement(
    invoice,
    profile,
    payments,
    *,
    room_number: str | None = None,
    room_charge_stay: str | None = None,
    room_charge_approval: str | None = None,
    room_charge_reason: str | None = None,
    consume_token: bool = True,
    _frappe=None,
) -> dict | None:
    """Enforce room-charge eligibility and manager override at settlement/submit.

    Returns None when the check has no room-charge tender or the profile has no
    configured room verifier method. Otherwise returns verification metadata and
    records `custom_room_charge_stay` + override attribution on `invoice`.
    """
    f = _frappe or frappe
    rc_modes = profile_room_charge_modes(profile, _field(invoice, "company"), _frappe=f)
    rc_amt = room_charge_amount(payments, rc_modes)
    if rc_amt <= 0:
        return None

    method_path = (_field(profile, "custom_room_lookup_method") or "").strip()
    if not method_path:
        return None

    room_in = (room_number if room_number is not None else _field(invoice, "room_number") or "").strip()
    stay_in = (room_charge_stay if room_charge_stay is not None else _field(invoice, "custom_room_charge_stay") or "").strip()
    if not room_in and not stay_in:
        f.throw(
            _("Enter and verify a room or guest stay before posting a room charge. [POS-RC-REQUIRED]"),
            f.ValidationError,
        )

    try:
        verifier = _resolve_verifier(method_path)
    except Exception:
        f.throw(
            _("Room charge verification is unavailable. Check the property connection before posting. [POS-RC-UNAVAILABLE]"),
            f.ValidationError,
        )

    from ury.ury_pos.offline_tenders import currency_precision

    req_currency = _field(invoice, "currency") or _field(profile, "currency") or ""
    req_prec = currency_precision(req_currency)
    kwargs = {
        "pos_profile": profile.name,
        "amount": round(flt(rc_amt), req_prec),
        "currency": req_currency,
    }
    if stay_in:
        kwargs["reservation"] = stay_in
    elif room_in:
        kwargs["room"] = room_in

    try:
        result = verifier(**kwargs)
    except (f.ValidationError, f.PermissionError):
        raise
    except Exception:
        f.throw(
            _("Room charge verification failed. Try again or take another payment method. [POS-RC-UNAVAILABLE]"),
            f.ValidationError,
        )

    if not isinstance(result, dict):
        f.throw(
            _("Room charge verification returned an unexpected response. [POS-RC-UNAVAILABLE]"),
            f.ValidationError,
        )

    resolved_stay = (result.get("reservation") or stay_in or "").strip()
    resolved_room = (result.get("room_number") or room_in or "").strip()

    if result.get("ok") is not False and result.get("posting_allowed"):
        if resolved_stay:
            invoice.custom_room_charge_stay = resolved_stay
        invoice.flags.pos_verified_room_charge = True
        return {
            "reservation": resolved_stay,
            "room_number": resolved_room,
            "amount": round(flt(rc_amt), currency_precision(result.get("currency") or req_currency)),
            "override_approver": None,
            "override_reason": None,
        }

    reason_code = (result.get("reason_code") or "POS-RC-REFUSED").strip()
    reason_text = (result.get("reason") or _("Room charge is not permitted for this stay.")).strip()

    # Non-overridable states (checked out, house account, wrong property, missing FX,
    # ambiguous shared room) are hard-refused even if an approval token is supplied.
    if not result.get("override_required"):
        f.throw(
            f"{reason_text} [{reason_code}]",
            f.ValidationError,
        )

    # Over-limit or No Post: NEVER silently bypass; require consumed manager approval.
    if not room_charge_approval:
        f.throw(
            f"{reason_text} [{reason_code}] [POS-AUTH-REQUIRED]",
            f.PermissionError,
        )

    claim_currency = result.get("currency") or req_currency
    claim_prec = currency_precision(claim_currency)
    rounded_rc_amt = round(flt(rc_amt), claim_prec)

    # Two-step draft save (`make_invoice(..., submit_invoice=0)`): keep the stay
    # and defer consuming the single-use approval token until actual submission.
    if not consume_token:
        if resolved_stay:
            invoice.custom_room_charge_stay = resolved_stay
        return {
            "reservation": resolved_stay,
            "room_number": resolved_room,
            "amount": rounded_rc_amt,
            "override_approver": None,
            "override_reason": None,
        }

    from ury.ury_pos import override

    claim = {
        "reservation": resolved_stay,
        "room": resolved_room,
        "amount": rounded_rc_amt,
        "currency": claim_currency,
        "reason": (room_charge_reason or "").strip(),
    }
    consumed = override.consume(
        room_charge_approval,
        "room_charge_over_limit",
        profile.name,
        invoice.name,
        claim,
    )
    approver = consumed.get("approver") if isinstance(consumed, dict) else getattr(consumed, "approver", None)
    approved_reason = (
        ((consumed.get("claim") or {}).get("reason") if isinstance(consumed, dict) else (getattr(consumed, "claim", None) or {}).get("reason"))
        or (room_charge_reason or "").strip()
    )

    if resolved_stay:
        invoice.custom_room_charge_stay = resolved_stay
    invoice.custom_override_approver = approver
    invoice.custom_override_reason = str(approved_reason)[:200]
    invoice.flags.pos_verified_room_charge_override = True
    invoice.flags.pos_verified_room_charge = True

    return {
        "reservation": resolved_stay,
        "room_number": resolved_room,
        "amount": rounded_rc_amt,
        "override_approver": approver,
        "override_reason": str(approved_reason)[:200],
    }
