"""Data-driven offline tender eligibility and per-till offline floor limits (matrix row 38).

Eligibility comes strictly from `Mode of Payment.custom_allow_offline` and
`Mode of Payment.custom_offline_requires_reference` plus per-till
`POS Profile.custom_offline_tender_limit`. Never guesses from tender names.
Room Charge tenders and live Phone-push tenders are never offline-eligible.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.ury_pos import override
from ury.ury_pos.room_charge import profile_room_charge_modes


def offline_tender_limit(profile) -> float:
    return max(0.0, flt(profile.get("custom_offline_tender_limit") or 0))


def profile_offline_payment_modes(profile, room_charge_modes: list[str] | None = None, *, _frappe=None) -> list[dict]:
    """Return offline eligibility metadata for every payment mode on `profile`."""
    f = _frappe or frappe
    rc_set = set(
        room_charge_modes
        if room_charge_modes is not None
        else profile_room_charge_modes(profile, profile.get("company"), _frappe=f)
    )
    mop_meta = f.get_meta("Mode of Payment")
    has_allow = bool(mop_meta and mop_meta.has_field("custom_allow_offline"))
    has_ref = bool(mop_meta and mop_meta.has_field("custom_offline_requires_reference"))
    fields = ["type"]
    if has_allow:
        fields.append("custom_allow_offline")
    if has_ref:
        fields.append("custom_offline_requires_reference")

    out: list[dict] = []
    seen: set[str] = set()
    for row in profile.get("payments") or []:
        mode = row.get("mode_of_payment")
        if not mode or mode in seen:
            continue
        seen.add(mode)
        info = f.db.get_value("Mode of Payment", mode, fields, as_dict=True) or {}
        mop_type = info.get("type") or ""
        if mode in rc_set or mop_type == "Phone":
            allow_offline = False
            requires_reference = False
        else:
            allow_offline = bool(cint(info.get("custom_allow_offline"))) if has_allow else False
            requires_reference = bool(cint(info.get("custom_offline_requires_reference"))) if has_ref else False
        out.append(
            {
                "mode_of_payment": mode,
                "type": mop_type,
                "allow_offline": allow_offline,
                "requires_reference": requires_reference,
            }
        )
    return out


def enforce_offline_tender_replay(
    invoice,
    profile,
    payments,
    *,
    offline_replay: int = 0,
    offline_approval: str | None = None,
    offline_reason: str | None = None,
    _frappe=None,
) -> dict | None:
    """Re-verify Mode of Payment offline flags, reference requirement, and per-till
    floor limit when replaying or submitting an offline-captured check."""
    f = _frappe or frappe
    is_offline = bool(cint(offline_replay)) or bool(invoice.get("ury_offline_captured_at"))
    if not is_offline:
        return None
    if invoice.get("is_return"):
        return None

    if not cint(profile.get("ury_offline_capture")):
        f.throw(
            _("Offline capture is disabled on this till. [POS-OFFLINE-DISABLED]"),
            f.ValidationError,
        )

    modes_list = profile_offline_payment_modes(profile, _frappe=f)
    by_mode = {row["mode_of_payment"]: row for row in modes_list}

    positive_rows = [row for row in (payments or []) if flt(row.get("amount")) > 0]
    if not positive_rows:
        return None

    total_offline = 0.0
    modes_used: list[str] = []
    comments = (invoice.get("custom_comments") or "").strip()

    for row in positive_rows:
        mode = row.get("mode_of_payment") or ""
        info = by_mode.get(mode)
        if not info or not info.get("allow_offline"):
            f.throw(
                _(
                    "{0} is not permitted for offline sales on this till. [POS-OFFLINE-TENDER-DISABLED]"
                ).format(mode or _("Payment method")),
                f.ValidationError,
            )
        if info.get("requires_reference"):
            ref = (row.get("reference_no") or "").strip()
            if not ref and not comments:
                f.throw(
                    _(
                        "A payment reference is required for offline {0} settlement. [POS-OFFLINE-REF-REQUIRED]"
                    ).format(mode),
                    f.ValidationError,
                )
        total_offline += flt(row.get("amount"))
        if mode not in modes_used:
            modes_used.append(mode)

    limit = offline_tender_limit(profile)
    if limit > 0 and flt(total_offline, 2) > flt(limit, 2):
        # Already verified in-process during this make_invoice -> submit flow?
        if getattr(getattr(invoice, "flags", None), "pos_verified_offline_override", False):
            return {
                "override_approver": invoice.get("custom_override_approver"),
                "override_reason": invoice.get("custom_override_reason"),
            }
        if not offline_approval:
            f.throw(
                _(
                    "Offline tender ({0}) exceeds this till's offline limit ({1}). "
                    "Manager approval is required. [POS-OFFLINE-OVER-LIMIT] [POS-AUTH-REQUIRED]"
                ).format(round(flt(total_offline), 2), round(flt(limit), 2)),
                f.PermissionError,
            )

        claim = {
            "amount": round(flt(total_offline), 2),
            "limit": round(flt(limit), 2),
            "modes": sorted(modes_used),
            "reason": (offline_reason or "").strip(),
        }
        consumed = override.consume(
            offline_approval,
            "offline_tender",
            profile.name,
            invoice.name,
            claim,
        )
        approver = consumed.get("approver") if isinstance(consumed, dict) else getattr(consumed, "approver", None)
        approved_reason = (
            ((consumed.get("claim") or {}).get("reason") if isinstance(consumed, dict) else (getattr(consumed, "claim", None) or {}).get("reason"))
            or (offline_reason or "").strip()
        )
        invoice.custom_override_approver = approver
        invoice.custom_override_reason = str(approved_reason)[:200]
        invoice.flags.pos_verified_offline_override = True
        return {
            "override_approver": approver,
            "override_reason": str(approved_reason)[:200],
        }

    return {"override_approver": None, "override_reason": None}
