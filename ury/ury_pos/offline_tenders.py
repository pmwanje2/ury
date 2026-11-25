"""Data-driven offline tender eligibility and per-till offline floor limits (matrix row 38).

Eligibility comes strictly from `Mode of Payment.custom_allow_offline` and
`Mode of Payment.custom_offline_requires_reference` plus per-till
`POS Profile.custom_offline_tender_limit`. Never guesses from tender names.
Room Charge tenders and live Phone-push tenders are never offline-eligible.
"""

from __future__ import annotations

import datetime

try:
    import frappe
    from frappe import _
    from frappe.utils import cint, flt
except ImportError:  # pragma: no cover - site-less host unit tests without frappe installed
    frappe = None

    def _(msg):
        return msg

    def cint(val):
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    def flt(val, precision=None):
        try:
            num = float(val or 0)
        except (TypeError, ValueError):
            num = 0.0
        return round(num, precision) if precision is not None else num

from ury.ury_pos.room_charge import profile_room_charge_modes

_ZERO_DP_CURRENCIES = frozenset({
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW",
    "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
})
_THREE_DP_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


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


def currency_precision(currency: str | None) -> int:
    """Return minor-unit decimal places for an ISO 4217 currency (0/2/3dp)."""
    code = (currency or "").strip().upper()
    if not code:
        return 2
    if code in _ZERO_DP_CURRENCIES:
        return 0
    if code in _THREE_DP_CURRENCIES:
        return 3
    try:
        from babel.numbers import get_currency_precision

        return int(get_currency_precision(code))
    except Exception:
        return 2


def offline_tender_limit(profile) -> float:
    return max(0.0, flt(_field(profile, "custom_offline_tender_limit") or 0))


def profile_offline_payment_modes(profile, room_charge_modes: list[str] | None = None, *, _frappe=None) -> list[dict]:
    """Return offline eligibility metadata for every payment mode on `profile`."""
    f = _frappe or frappe
    rc_set = set(
        room_charge_modes
        if room_charge_modes is not None
        else profile_room_charge_modes(profile, _field(profile, "company"), _frappe=f)
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
    for row in _field(profile, "payments") or []:
        mode = _field(row, "mode_of_payment")
        if not mode or mode in seen:
            continue
        seen.add(mode)
        raw_info = f.db.get_value("Mode of Payment", mode, fields, as_dict=True)
        info = raw_info if isinstance(raw_info, dict) else {}
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
    consume_token: bool = True,
    _frappe=None,
) -> dict | None:
    """Re-verify Mode of Payment offline flags, reference requirement, and per-till
    floor limit when replaying or submitting an offline-captured check."""
    f = _frappe or frappe
    captured_at = _field(invoice, "ury_offline_captured_at")
    has_captured_at = isinstance(captured_at, (str, datetime.date, datetime.datetime)) and bool(captured_at)
    is_offline = bool(cint(offline_replay)) or has_captured_at
    if not is_offline:
        return None
    if _field(invoice, "is_return"):
        return None

    if not cint(_field(profile, "ury_offline_capture")):
        f.throw(
            _("Offline capture is disabled on this till. [POS-OFFLINE-DISABLED]"),
            f.ValidationError,
        )

    modes_list = profile_offline_payment_modes(profile, _frappe=f)
    by_mode = {row["mode_of_payment"]: row for row in modes_list}

    positive_rows = [row for row in (payments or []) if flt(_field(row, "amount")) > 0]
    if not positive_rows:
        return None

    total_offline = 0.0
    modes_used: list[str] = []
    comments = (_field(invoice, "custom_comments") or "").strip()

    for row in positive_rows:
        mode = _field(row, "mode_of_payment") or ""
        info = by_mode.get(mode)
        if not info or not info.get("allow_offline"):
            f.throw(
                _(
                    "{0} is not permitted for offline sales on this till. [POS-OFFLINE-TENDER-DISABLED]"
                ).format(mode or _("Payment method")),
                f.ValidationError,
            )
        if info.get("requires_reference"):
            ref = (_field(row, "reference_no") or "").strip()
            if not ref and not comments:
                f.throw(
                    _(
                        "A payment reference is required for offline {0} settlement. [POS-OFFLINE-REF-REQUIRED]"
                    ).format(mode),
                    f.ValidationError,
                )
        total_offline += flt(_field(row, "amount"))
        if mode not in modes_used:
            modes_used.append(mode)

    limit = offline_tender_limit(profile)
    prec = currency_precision(_field(invoice, "currency") or _field(profile, "currency"))
    if limit <= 0:
        return None
    from ury.ury_pos.approval_currency import company_approval_amount, invoice_approval_currency

    approval_currency = invoice_approval_currency(invoice, f)
    approval_amount = company_approval_amount(invoice, total_offline, f, currency_and_rate=approval_currency)
    if approval_amount > limit:
        # Already verified in-process during this make_invoice -> submit flow?
        if getattr(getattr(invoice, "flags", None), "pos_verified_offline_override", False):
            return {
                "override_approver": _field(invoice, "custom_override_approver"),
                "override_reason": _field(invoice, "custom_override_reason"),
            }
        if not offline_approval:
            f.throw(
                _(
                    "Offline tender ({0}) exceeds this till's offline limit ({1}). "
                    "Manager approval is required. [POS-OFFLINE-OVER-LIMIT] [POS-AUTH-REQUIRED]"
                ).format(approval_amount, limit),
                f.PermissionError,
            )
        if not consume_token:
            return {"override_approver": None, "override_reason": None}

        from ury.ury_pos import override

        claim = {
            "amount": round(flt(total_offline), prec),
            "limit": round(flt(limit), prec),
            "modes": sorted(modes_used),
            "reason": (offline_reason or "").strip(),
        }
        consumed = override.consume(
            offline_approval,
            "offline_tender",
            profile.name,
            invoice.name,
            claim,
            approval_amount=approval_amount,
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
