"""Server-side room-charge eligibility and duty-manager override gate (matrix row 34).

Calls the POS Profile's configured room-lookup/verifier hook (e.g.
`kamra.pos_interface.verify_room_charge`), fails closed whenever the hook is
missing or raises, hard-refuses non-overridable states (checked-out guest,
house account, wrong property, missing FX rate), and requires a consumed
`room_charge_over_limit` manager approval for over-limit or No Post stays.
"""

from __future__ import annotations

try:
    import frappe
    from frappe import _
    from frappe.utils import flt
except ImportError:  # pragma: no cover - site-less host unit tests without frappe installed
    frappe = None

    def _(msg):
        return msg

    def flt(val, precision=None):
        try:
            num = float(val or 0)
        except (TypeError, ValueError):
            num = 0.0
        return round(num, precision) if precision is not None else num


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


def _resolve_verifier(method_path: str, _frappe=None, *, room_number: str = ""):
    if not method_path or not isinstance(method_path, str):
        raise ImportError("Room lookup method is not configured.")
    f = _frappe or frappe
    if f is None:
        raise ImportError("Framework context is unavailable.")
    message_log = getattr(f.local, "message_log", None)
    message_count = len(message_log) if message_log is not None else 0
    try:
        fn = f.get_attr(method_path)
    except Exception:
        # get_attr can queue an "App is not installed" message before raising.
        if message_log is not None:
            del message_log[message_count:]
        script_name = f.db.get_value(
            "Server Script",
            {"script_type": "API", "api_method": method_path, "disabled": 0},
            "name",
        )
        if not script_name:
            raise
    else:
        if not callable(fn):
            raise ImportError(f"Room lookup method {method_path} is not callable.")
        return fn

    script = f.get_doc("Server Script", script_name)

    def verify(**kwargs):
        room = room_number or kwargs.get("room", "")
        outer_form, outer_response = f.local.form_dict, f.local.response
        try:
            f.local.form_dict = f._dict(kwargs, room=room, room_number=room)
            # Frappe exposes frappe.response to safe_exec only when it is truthy.
            f.local.response = f._dict(docs=[])
            returned = script.execute_method()
            result = f.local.response.get("message", returned)
        finally:
            f.local.form_dict = outer_form
            f.local.response = outer_response

        if isinstance(result, dict) and result.get("ok") and "posting_allowed" not in result:
            if result.get("no_post"):
                return dict(
                    result,
                    posting_allowed=False,
                    override_required=True,
                    reason_code="KMR-RC-011",
                    reason=_("Guest stay is marked No Post."),
                )
            return dict(result, posting_allowed=True)
        return result

    return verify


def profile_room_charge_modes(profile, company: str | None = None, *, _frappe=None) -> list[str]:
    f = _frappe or frappe
    comp = company or _field(profile, "company")
    if not isinstance(comp, str) or not comp or f is None:
        return []
    meta = f.get_meta("Company")
    if not meta or not meta.has_field("custom_guest_ledger_clearing"):
        return []
    clearing = f.db.get_value("Company", comp, "custom_guest_ledger_clearing")
    if not isinstance(clearing, str) or not clearing:
        return []
    modes: list[str] = []
    for row in _field(profile, "payments") or []:
        mode = _field(row, "mode_of_payment")
        if not isinstance(mode, str) or not mode:
            continue
        account = f.db.get_value(
            "Mode of Payment Account",
            {"parent": mode, "company": comp},
            "default_account",
        )
        if isinstance(account, str) and account == clearing:
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

    Returns None when the check has no room-charge tender. Fails closed with
    [POS-RC-UNCONFIGURED] when a room-charge tender is present and the profile
    has no configured room verifier method. Otherwise returns verification
    metadata and records `custom_room_charge_stay` + override attribution on
    `invoice`.
    """
    f = _frappe or frappe
    rc_modes = profile_room_charge_modes(profile, _field(invoice, "company"), _frappe=f)
    rc_amt = room_charge_amount(payments, rc_modes)
    if rc_amt <= 0:
        return None

    raw_method = _field(profile, "custom_room_lookup_method")
    method_path = raw_method.strip() if isinstance(raw_method, str) else ""
    if not method_path:
        f.throw(
            _(
                "Room charge verification is not configured for this till. "
                "Configure a room lookup method before posting room charges. [POS-RC-UNCONFIGURED]"
            ),
            f.ValidationError,
        )

    room_in = (room_number if room_number is not None else _field(invoice, "room_number") or "").strip()
    stay_in = (room_charge_stay if room_charge_stay is not None else _field(invoice, "custom_room_charge_stay") or "").strip()
    if not room_in and not stay_in:
        f.throw(
            _("Enter and verify a room or guest stay before posting a room charge. [POS-RC-REQUIRED]"),
            f.ValidationError,
        )

    try:
        verifier = _resolve_verifier(method_path, _frappe=f, room_number=room_in)
    except Exception:
        f.throw(
            _("Room charge verification is unavailable. Check the property connection before posting. [POS-RC-UNAVAILABLE]"),
            f.ValidationError,
        )

    from ury.ury_pos.offline_tenders import currency_precision

    req_currency = (_field(invoice, "currency") or _field(profile, "currency") or "").strip()
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
    claim_currency = (result.get("currency") or req_currency or "").strip()
    claim_prec = currency_precision(claim_currency)

    def _resolved_claim_amount() -> float:
        if result.get("charge_amount") is not None:
            return round(flt(result.get("charge_amount")), claim_prec)
        if claim_currency and req_currency and claim_currency.upper() != req_currency.upper():
            inv_company = _field(invoice, "company") or _field(profile, "company")
            comp_cur = (
                f.get_cached_value("Company", inv_company, "default_currency")
                if isinstance(inv_company, str) and inv_company
                else None
            )
            conv_rate = flt(_field(invoice, "conversion_rate"))
            if isinstance(comp_cur, str) and comp_cur.strip().upper() == claim_currency.upper() and conv_rate > 0:
                return round(flt(rc_amt) * conv_rate, claim_prec)
            f.throw(
                _("Room charge currency conversion is unavailable. [POS-RC-CURRENCY]"),
                f.ValidationError,
            )
        return round(flt(rc_amt), claim_prec)

    if result.get("ok") is not False and result.get("posting_allowed"):
        if resolved_stay:
            invoice.custom_room_charge_stay = resolved_stay
        invoice.flags.pos_verified_room_charge = True
        return {
            "reservation": resolved_stay,
            "room_number": resolved_room,
            "amount": _resolved_claim_amount(),
            "currency": claim_currency,
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

    rounded_rc_amt = _resolved_claim_amount()

    # Two-step draft save (`make_invoice(..., submit_invoice=0)`): keep the stay
    # and defer consuming the single-use approval token until actual submission.
    if not consume_token:
        if resolved_stay:
            invoice.custom_room_charge_stay = resolved_stay
        return {
            "reservation": resolved_stay,
            "room_number": resolved_room,
            "amount": rounded_rc_amt,
            "currency": claim_currency,
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
        "currency": claim_currency,
        "override_approver": approver,
        "override_reason": str(approved_reason)[:200],
    }
