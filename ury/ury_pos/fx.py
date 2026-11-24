"""Foreign currency tender (G22), config-driven payment reference validation (G25),
and offline waiter collision/relay helpers (G46).

Key accounting & security invariants:
- ERPNext v16 POS Invoice GL bug avoidance (`sales_invoice.py:2089-2091`):
  Foreign-currency Modes of Payment ALWAYS map `Mode of Payment Account.default_account`
  to a home-currency FX Till Clearing account (`POS Profile.currency`) and map the
  actual foreign cash/bank account in `Mode of Payment Account.custom_fx_vault_account`.
- On `POS Closing Entry.on_submit`, a balanced `multi_currency = 1` Journal Entry sweeps
  from `FX Till Clearing` (company currency) into `custom_fx_vault_account` (exact native
  foreign currency amount at the locked shift rate), posting physical note over/short to
  the till's write-off / over-short account and sub-minor-unit rounding to the exchange
  gain/loss account.
- Exchange rates come strictly from native ERPNext `Currency Exchange` (honoring active
  Workflow `workflow_state == "Approved"`) guarded by `Accounts Settings.stale_days`
  (fail-closed `stale_days = 1` when unset). Missing or stale rates throw
  `URY-FX-RATE-STALE` and never fall back to `1.0` or trust client-supplied rates.
- Payment reference regex patterns are length-bounded (<= 128 chars) and checked against
  catastrophic nested quantifiers before matching inputs (<= 256 chars).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, flt


def _today() -> str:
    fn = getattr(getattr(frappe, "utils", None), "today", None)
    if callable(fn):
        return fn()
    return date.today().isoformat()


def _getdate(val=None) -> date:
    fn = getattr(getattr(frappe, "utils", None), "getdate", None)
    if callable(fn):
        return fn(val)
    if val is None or val == "":
        return date.today()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return date.fromisoformat(str(val)[:10])


def _add_days(val, days: int) -> date:
    fn = getattr(getattr(frappe, "utils", None), "add_days", None)
    if callable(fn):
        return fn(val, days)
    return _getdate(val) + timedelta(days=int(days))

MAX_REFERENCE_PATTERN_LENGTH = 128
MAX_REFERENCE_VALUE_LENGTH = 256
GUEST_LABEL_MAX = 40

# Standard ISO 4217 non-2dp currencies for reliable site-less / fallback precision.
ZERO_DECIMAL_CURRENCIES = frozenset(
    {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "TZS", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
)
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})

# Reject nested quantifiers / backreferences that could cause catastrophic backtracking.
_UNSAFE_REGEX_FRAGMENTS = (
    re.compile(r"\([^)]*[+*?][^)]*\)[+*?{]"),
    re.compile(r"[+*?]\s*[+*?]"),
    re.compile(r"\\[1-9]"),
)


def currency_precision(currency: str | None) -> int:
    code = (currency or "").strip().upper()
    if not code:
        return 2
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    try:
        fraction_units = frappe.db.get_value("Currency", code, "fraction_units")
        if fraction_units is not None:
            fu = cint(fraction_units)
            if fu == 1:
                return 0
            if fu == 1000:
                return 3
            if fu == 100:
                return 2
    except Exception:
        pass
    return 2


def _base_precision(currency: str | None) -> int:
    code = (currency or "").strip().upper()
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    try:
        prec = frappe.get_precision("Sales Invoice Payment", "amount")
        if isinstance(prec, int) and not isinstance(prec, bool) and prec >= 0:
            return prec
    except Exception:
        pass
    return currency_precision(currency)


def is_safe_regex_pattern(pattern: str) -> bool:
    if not isinstance(pattern, str):
        return False
    if len(pattern) > MAX_REFERENCE_PATTERN_LENGTH:
        return False
    for bad in _UNSAFE_REGEX_FRAGMENTS:
        if bad.search(pattern):
            return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def get_mode_payment_config(mode_of_payment: str, company: str | None = None) -> dict:
    """Return G25 payment-reference + G22 FX tender configuration for a Mode of Payment."""
    if not mode_of_payment:
        return {}
    fields = [
        "name",
        "type",
        "custom_reference_mode",
        "custom_reference_label",
        "custom_reference_placeholder",
        "custom_reference_pattern",
        "custom_reference_date_required",
        "custom_tender_currency",
        "custom_fx_rate_type",
    ]
    row = None
    try:
        row = frappe.db.get_value("Mode of Payment", mode_of_payment, fields, as_dict=True)
    except Exception:
        # Fallback if a mock/older DB does not have all fields yet.
        try:
            doc = frappe.get_cached_doc("Mode of Payment", mode_of_payment)
            row = {f: doc.get(f) for f in fields}
        except Exception:
            row = {}

    cfg = dict(row) if isinstance(row, dict) else {}
    def _s(key: str, default: str = "") -> str:
        val = cfg.get(key)
        return val.strip() if isinstance(val, str) and val.strip() else default

    cfg["mode_of_payment"] = mode_of_payment
    cfg["custom_reference_mode"] = _s("custom_reference_mode", "Hidden")
    cfg["custom_reference_label"] = _s("custom_reference_label")
    cfg["custom_reference_placeholder"] = _s("custom_reference_placeholder")
    cfg["custom_reference_pattern"] = _s("custom_reference_pattern")
    raw_date_req = cfg.get("custom_reference_date_required")
    cfg["custom_reference_date_required"] = cint(raw_date_req) if isinstance(raw_date_req, (int, float, str, bool)) else 0
    cfg["custom_tender_currency"] = _s("custom_tender_currency").upper()
    cfg["custom_fx_rate_type"] = _s("custom_fx_rate_type", "Buying")

    if company:
        acct = None
        try:
            acct = frappe.db.get_value(
                "Mode of Payment Account",
                {"parent": mode_of_payment, "company": company},
                ["default_account", "custom_fx_vault_account"],
                as_dict=True,
            )
        except Exception:
            try:
                default_acct = frappe.db.get_value(
                    "Mode of Payment Account",
                    {"parent": mode_of_payment, "company": company},
                    "default_account",
                )
                acct = {"default_account": default_acct, "custom_fx_vault_account": None}
            except Exception:
                acct = None
        if isinstance(acct, dict):
            cfg["default_account"] = acct.get("default_account")
            cfg["custom_fx_vault_account"] = acct.get("custom_fx_vault_account")

    return cfg


def validate_payment_reference(
    mode_of_payment: str,
    reference_no: str | None = None,
    reference_date: str | None = None,
    mode_config: dict | None = None,
) -> dict:
    """Validate and normalize payment reference_no / reference_date per Mode of Payment config."""
    cfg = mode_config if mode_config is not None else get_mode_payment_config(mode_of_payment)
    ref_mode = (cfg.get("custom_reference_mode") or "Hidden").strip() or "Hidden"
    label = (cfg.get("custom_reference_label") or "").strip() or _("Payment Reference")
    pattern = (cfg.get("custom_reference_pattern") or "").strip()
    date_required = cint(cfg.get("custom_reference_date_required")) == 1

    ref = str(reference_no or "").strip()
    ref_date = str(reference_date).strip() if reference_date else None

    if len(ref) > MAX_REFERENCE_VALUE_LENGTH:
        frappe.throw(
            f"URY-PAY-REF-INVALID: {label} for {mode_of_payment} exceeds maximum length of {MAX_REFERENCE_VALUE_LENGTH} characters.",
            frappe.ValidationError,
        )

    if ref_mode == "Required" and not ref:
        frappe.throw(
            f"URY-PAY-REF-REQUIRED: {label} is required for payment mode {mode_of_payment}.",
            frappe.ValidationError,
        )

    if ref and pattern:
        if not is_safe_regex_pattern(pattern):
            frappe.throw(
                f"URY-PAY-REF-INVALID: Configured reference validation pattern for {mode_of_payment} is invalid or unsafe.",
                frappe.ValidationError,
            )
        if not re.fullmatch(pattern, ref):
            frappe.throw(
                f"URY-PAY-REF-INVALID: {label} for {mode_of_payment} does not match the required format.",
                frappe.ValidationError,
            )

    if date_required and (ref_mode == "Required" or bool(ref)) and not ref_date:
        frappe.throw(
            f"URY-PAY-REF-REQUIRED: Reference date is required for payment mode {mode_of_payment}.",
            frappe.ValidationError,
        )

    return {"reference_no": ref, "reference_date": ref_date}


def _currency_exchange_workflow_active() -> bool:
    try:
        wf = frappe.db.get_value(
            "Workflow",
            {"document_type": "Currency Exchange", "is_active": 1},
            "name",
        )
        if not wf:
            return False
        meta = frappe.get_meta("Currency Exchange")
        return bool(meta and meta.has_field("workflow_state"))
    except Exception:
        return False


def _accounts_stale_days() -> int:
    try:
        raw = frappe.db.get_single_value("Accounts Settings", "stale_days")
        days = cint(raw)
        if days > 0:
            return days
    except Exception:
        pass
    # Fail closed to 1 day when Accounts Settings.stale_days is unset or 0.
    return 1


def resolve_pos_exchange_rate(
    from_currency: str,
    to_currency: str,
    posting_date=None,
    company: str | None = None,
    rate_type: str = "Buying",
) -> dict:
    """Resolve an approved, non-stale POS exchange rate from ERPNext Currency Exchange.

    Never falls back to 1.0 or an unapproved/stale rate; throws `URY-FX-RATE-STALE`.
    """
    src = (from_currency or "").strip().upper()
    dst = (to_currency or "").strip().upper()
    eff_date = _getdate(posting_date or _today())
    effective_rate_type = (rate_type or "Buying").strip() or "Buying"

    if not src or not dst or src == dst:
        return {
            "rate": 1.0,
            "rate_date": str(eff_date),
            "currency_exchange": None,
            "from_currency": src or dst,
            "to_currency": dst or src,
            "rate_type": effective_rate_type,
            "stale_days": 0,
            "is_home_currency": True,
        }

    stale_days = _accounts_stale_days()
    min_date = _add_days(eff_date, -stale_days)

    filters = {
        "from_currency": src,
        "to_currency": dst,
        "date": ["between", [min_date, eff_date]],
    }
    if effective_rate_type == "Mid/Selling":
        filters["for_selling"] = 1
    else:
        filters["for_buying"] = 1

    if _currency_exchange_workflow_active():
        filters["workflow_state"] = "Approved"

    rows = frappe.get_all(
        "Currency Exchange",
        filters=filters,
        fields=["name", "date", "exchange_rate"],
        order_by="date desc, modified desc",
        limit_page_length=1,
    )

    if not rows:
        frappe.throw(
            f"URY-FX-RATE-STALE: No approved fresh exchange rate found for {src} to {dst} within {stale_days} day(s) of {eff_date}.",
            frappe.ValidationError,
        )

    chosen = rows[0]
    rate = flt(chosen.get("exchange_rate"), 9)
    if rate <= 0:
        frappe.throw(
            f"URY-FX-RATE-STALE: Exchange rate for {src} to {dst} ({chosen.get('name')}) is not positive.",
            frappe.ValidationError,
        )

    return {
        "rate": rate,
        "rate_date": str(_getdate(chosen.get("date"))),
        "currency_exchange": chosen.get("name"),
        "from_currency": src,
        "to_currency": dst,
        "rate_type": effective_rate_type,
        "stale_days": stale_days,
        "is_home_currency": False,
    }


def _authorize_fx_rate_override(
    *,
    pos_profile,
    mode_of_payment: str,
    tender_currency: str,
    home_currency: str,
    override_rate: float,
    market_rate: float | None,
    token: str | None,
    invoice_name: str | None,
    reason: str | None = None,
) -> None:
    if cint(pos_profile.get("custom_allow_fx_rate_override")) != 1:
        frappe.throw(
            f"URY-FX-RATE-STALE: FX rate override is not enabled on POS Profile {pos_profile.get('name')}.",
            frappe.PermissionError,
        )
    if override_rate <= 0:
        frappe.throw(
            f"URY-FX-RATE-STALE: Override exchange rate for {tender_currency} must be positive.",
            frappe.ValidationError,
        )
    max_var_pct = flt(pos_profile.get("custom_max_fx_variance_pct"))
    if market_rate and market_rate > 0 and max_var_pct > 0:
        actual_var_pct = abs(override_rate - market_rate) / market_rate * 100.0
        if actual_var_pct > max_var_pct + 1e-9:
            frappe.throw(
                f"POS-FX-RATE-VARIANCE: Override rate {override_rate} deviates by {actual_var_pct:.2f}% from {market_rate}, exceeding maximum allowed variance of {max_var_pct:.2f}%.",
                frappe.ValidationError,
            )

    from ury.ury_pos import override

    claim = {
        "action": "pos_fx_rate_override",
        "mode_of_payment": mode_of_payment,
        "from_currency": tender_currency,
        "to_currency": home_currency,
        "override_rate": flt(override_rate, 9),
        "reason": (reason or "").strip(),
    }
    if token:
        override.consume(token, "fx_rate_override", pos_profile.get("name"), invoice_name or pos_profile.get("name"), claim)
    elif override.may_perform(frappe.session.user, pos_profile.get("name"), "fx_rate_override"):
        override._validate_reason(pos_profile.get("name"), "fx_rate_override", claim)
    else:
        frappe.throw(
            _("Manager authorisation is required to override an exchange rate.") + " [POS-AUTH-REQUIRED]",
            frappe.PermissionError,
        )


def prepare_settlement_payments(
    payments,
    pos_profile,
    posting_date=None,
    invoice_name: str | None = None,
    is_return: bool = False,
) -> list[dict]:
    """Authoritatively validate G25 payment references and resolve G22 FX conversions.

    - Never trusts client-supplied `custom_exchange_rate` or client-converted local `amount`
      for FX modes unless `custom_allow_fx_rate_override` is enabled and verified via `#147`.
    - Stamps `custom_tender_currency`, `custom_tender_amount`, `custom_exchange_rate`,
      `custom_exchange_rate_date`, `reference_no`, `reference_date`.
    """
    raw_home = pos_profile.get("currency") if pos_profile is not None else None
    home_currency = raw_home.strip().upper() if isinstance(raw_home, str) else ""
    raw_company = pos_profile.get("company") if pos_profile is not None else None
    company = raw_company if isinstance(raw_company, str) else None
    eff_date = _getdate(posting_date if isinstance(posting_date, (str, date, datetime)) else _today())
    base_prec = _base_precision(home_currency)

    prepared = []
    for raw in payments or []:
        mode = raw.get("mode_of_payment")
        if not mode:
            continue
        cfg = get_mode_payment_config(mode, company=company)
        tender_currency = (cfg.get("custom_tender_currency") or "").strip().upper()
        is_fx = bool(tender_currency and home_currency and tender_currency != home_currency)

        raw_amount = flt(raw.get("amount"))
        raw_fx_amount = raw.get("custom_tender_amount")
        has_fx_amount = raw_fx_amount is not None and str(raw_fx_amount).strip() != ""
        fx_amount_val = flt(raw_fx_amount) if has_fx_amount else 0.0

        # Active tender row check (skip zero-amount placeholder rows for ref/FX checks)
        is_active = (abs(fx_amount_val) > 0 if is_fx and has_fx_amount else abs(raw_amount) > 0)
        if not is_return and (raw_amount < 0 or (has_fx_amount and fx_amount_val < 0)):
            frappe.throw(
                _("Negative payment amount for {0} is not allowed.").format(mode),
                frappe.ValidationError,
            )

        ref_no = raw.get("reference_no")
        ref_date = raw.get("reference_date")
        if is_active:
            normalized_ref = validate_payment_reference(
                mode,
                reference_no=ref_no,
                reference_date=ref_date,
                mode_config=cfg,
            )
            ref_no = normalized_ref["reference_no"]
            ref_date = normalized_ref["reference_date"]
        else:
            ref_no = str(ref_no or "").strip()

        entry = {
            "mode_of_payment": mode,
            "amount": flt(raw_amount, base_prec),
        }
        if ref_no:
            entry["reference_no"] = ref_no
        if ref_date:
            entry["reference_date"] = ref_date
        if raw.get("clearance_date"):
            entry["clearance_date"] = raw.get("clearance_date")

        if is_fx and is_active:
            fx_prec = currency_precision(tender_currency)
            rate_type = cfg.get("custom_fx_rate_type") or "Buying"

            override_rate_raw = raw.get("override_exchange_rate")
            override_token = raw.get("fx_override_token") or raw.get("approval_token")
            override_reason = raw.get("fx_override_reason") or raw.get("reason")

            market_info = None
            market_error = None
            try:
                market_info = resolve_pos_exchange_rate(
                    tender_currency,
                    home_currency,
                    posting_date=eff_date,
                    company=company,
                    rate_type=rate_type,
                )
            except frappe.ValidationError as exc:
                market_error = exc

            if override_rate_raw is not None and str(override_rate_raw).strip() != "":
                override_rate = flt(override_rate_raw, 9)
                _authorize_fx_rate_override(
                    pos_profile=pos_profile,
                    mode_of_payment=mode,
                    tender_currency=tender_currency,
                    home_currency=home_currency,
                    override_rate=override_rate,
                    market_rate=market_info["rate"] if market_info else None,
                    token=override_token,
                    invoice_name=invoice_name,
                    reason=override_reason,
                )
                rate = override_rate
                rate_date = str(eff_date)
            else:
                if market_error is not None:
                    raise market_error
                rate = market_info["rate"]
                rate_date = market_info["rate_date"]

            if has_fx_amount:
                tender_amount = flt(fx_amount_val, fx_prec)
            else:
                tender_amount = flt(raw_amount / rate, fx_prec)

            base_amount = flt(tender_amount * rate, base_prec)
            entry["amount"] = base_amount
            entry["custom_tender_currency"] = tender_currency
            entry["custom_tender_amount"] = tender_amount
            entry["custom_exchange_rate"] = flt(rate, 9)
            entry["custom_exchange_rate_date"] = rate_date
        elif any(k in raw for k in ("custom_tender_currency", "custom_tender_amount", "custom_exchange_rate", "custom_exchange_rate_date")):
            entry["custom_tender_currency"] = ""
            entry["custom_tender_amount"] = 0.0
            entry["custom_exchange_rate"] = 0.0
            entry["custom_exchange_rate_date"] = None

        prepared.append(entry)

    return prepared


def ensure_local_change_payment_row(doc, pos_profile=None) -> None:
    """Guarantee a local-currency Cash row mapped to `account_for_change_amount` exists
    when `change_amount > 0` so ERPNext `POSClosingEntry.get_change_payment_row` ALWAYS
    deducts local change from local Cash and NEVER from an FX tender mode.
    """
    if doc.get("is_return"):
        return
    raw_change = doc.get("change_amount")
    if not isinstance(raw_change, (int, float, str)) or flt(raw_change) <= 0:
        return

    raw_company = doc.get("company")
    company = raw_company if isinstance(raw_company, str) else None
    raw_home = doc.get("currency")
    home_currency = raw_home.strip().upper() if isinstance(raw_home, str) else ""
    if pos_profile is None and isinstance(doc.get("pos_profile"), str):
        try:
            pos_profile = frappe.get_cached_doc("POS Profile", doc.pos_profile)
        except Exception:
            pos_profile = None

    raw_change_acct = doc.get("account_for_change_amount") or (
        pos_profile.get("account_for_change_amount") if pos_profile else None
    )
    change_account = raw_change_acct if isinstance(raw_change_acct, str) and raw_change_acct.strip() else None

    profile_modes = [row.mode_of_payment for row in (pos_profile.get("payments") or [])] if pos_profile else []
    mode_to_account = {}
    for mode in profile_modes:
        acct = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": mode, "company": company},
            "default_account",
        )
        if acct:
            mode_to_account[mode] = acct

    if not change_account and mode_to_account:
        # Pick the first local Cash mode's default account as fallback change account.
        for mode, acct in mode_to_account.items():
            cfg = get_mode_payment_config(mode, company=company)
            if cfg.get("type") == "Cash" and (
                not cfg.get("custom_tender_currency") or cfg.get("custom_tender_currency") == home_currency
            ):
                change_account = acct
                doc.account_for_change_amount = change_account
                break

    if not change_account:
        return

    for row in doc.get("payments") or []:
        row_acct = row.get("account") or mode_to_account.get(row.get("mode_of_payment"))
        if not row_acct and row.get("mode_of_payment"):
            row_acct = frappe.db.get_value(
                "Mode of Payment Account",
                {"parent": row.get("mode_of_payment"), "company": company},
                "default_account",
            )
        if row_acct == change_account:
            return

    local_mode = None
    for mode, acct in mode_to_account.items():
        if acct == change_account:
            local_mode = mode
            break

    if not local_mode:
        # Look up any Mode of Payment mapped to change_account for this company.
        local_mode = frappe.db.get_value(
            "Mode of Payment Account",
            {"default_account": change_account, "company": company},
            "parent",
        )

    if local_mode:
        doc.append(
            "payments",
            {
                "mode_of_payment": local_mode,
                "account": change_account,
                "amount": 0.0,
                "base_amount": 0.0,
            },
        )


def validate_fx_payment_modes(pos_profile_doc) -> None:
    """Enforce ERPNext v16 GL safety on every FX Mode of Payment attached to a POS Profile.

    - `Mode of Payment Account.default_account` MUST be in `POS Profile.currency`
      (an FX Till Clearing account, NEVER a foreign-currency account!).
    - `Mode of Payment Account.custom_fx_vault_account` MUST be set and MUST be in
      `Mode of Payment.custom_tender_currency`.
    """
    home_currency = (pos_profile_doc.get("currency") or "").strip().upper()
    company = pos_profile_doc.get("company")
    if not home_currency or not company:
        return

    for row in pos_profile_doc.get("payments") or []:
        mode = row.get("mode_of_payment")
        if not mode:
            continue
        mop = frappe.db.get_value(
            "Mode of Payment",
            mode,
            ["custom_tender_currency", "custom_fx_rate_type"],
            as_dict=True,
        )
        tender_currency = ((mop or {}).get("custom_tender_currency") or "").strip().upper()
        if not tender_currency or tender_currency == home_currency:
            continue

        acct_row = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": mode, "company": company},
            ["default_account", "custom_fx_vault_account"],
            as_dict=True,
        ) or {}
        clearing_acct = acct_row.get("default_account")
        vault_acct = acct_row.get("custom_fx_vault_account")

        if not clearing_acct:
            frappe.throw(
                f"POS-FX-ACCOUNT-INVALID: Foreign tender mode {mode} ({tender_currency}) must configure a {home_currency} FX Till Clearing account in default_account for {company}.",
                frappe.ValidationError,
            )
        clearing_curr = (frappe.db.get_value("Account", clearing_acct, "account_currency") or "").strip().upper()
        if clearing_curr and clearing_curr != home_currency:
            frappe.throw(
                f"POS-FX-ACCOUNT-INVALID: Foreign tender mode {mode} default_account ({clearing_acct}) must be in {home_currency} (FX Till Clearing), not {clearing_curr}. Configure the {tender_currency} drawer in custom_fx_vault_account instead.",
                frappe.ValidationError,
            )

        if not vault_acct:
            frappe.throw(
                f"POS-FX-ACCOUNT-INVALID: Foreign tender mode {mode} ({tender_currency}) must configure custom_fx_vault_account in {tender_currency} for {company}.",
                frappe.ValidationError,
            )
        vault_curr = (frappe.db.get_value("Account", vault_acct, "account_currency") or "").strip().upper()
        if vault_curr and vault_curr != tender_currency:
            frappe.throw(
                f"POS-FX-ACCOUNT-INVALID: Foreign tender mode {mode} custom_fx_vault_account ({vault_acct}) must be in {tender_currency}, not {vault_curr}.",
                frappe.ValidationError,
            )


def validate_offline_peer_relay_url(url: str | None) -> None:
    raw = (url or "").strip()
    if not raw:
        return
    if len(raw) > 512:
        frappe.throw(
            "POS-OFFLINE-RELAY-INVALID: Offline peer relay URL must be 512 characters or fewer.",
            frappe.ValidationError,
        )
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.netloc:
        frappe.throw(
            "POS-OFFLINE-RELAY-INVALID: Offline peer relay URL must be a valid http(s):// or ws(s):// URL.",
            frappe.ValidationError,
        )


def build_offline_collision_plan(
    existing_invoice,
    table: str,
    client_order_id: str,
    round_id: str | None,
    policy: str | None = None,
) -> dict:
    """Build a deterministic reconciliation plan when an offline-captured table order
    replays onto a table that already has an open draft POS Invoice for another order.
    """
    effective_policy = (policy or "Auto Split Check").strip() or "Auto Split Check"
    short_id = (client_order_id or "")[:4] or "OFF"

    if effective_policy == "Merge Rounds":
        return {
            "action": "merge_rounds",
            "target_invoice": existing_invoice.get("name"),
            "collision_round_id": f"{client_order_id}:{round_id or '1'}",
            "custom_offline_conflict": 1,
        }

    raw_label = f"{table} · Offline Check {short_id}"
    if len(raw_label) > GUEST_LABEL_MAX:
        raw_label = f"{table[:20]} · Offline {short_id}"[:GUEST_LABEL_MAX]
    return {
        "action": "auto_split_check",
        "custom_split_from": existing_invoice.get("name"),
        "custom_split_group": existing_invoice.get("custom_split_group") or existing_invoice.get("name"),
        "custom_offline_conflict": 1,
        "custom_guest_label": raw_label,
    }


def post_fx_vault_sweep_journal_entry(doc, method=None) -> str | None:
    """On POS Closing Entry.on_submit, post ONE balanced multi-currency Journal Entry
    sweeping each FX mode from its company-currency FX Till Clearing account into its
    native foreign-currency FX Vault account, routing physical over/short to the
    write-off / cash-over-short account and sub-minor-unit rounding to exchange gain/loss.
    """
    if doc.get("custom_fx_vault_journal_entry"):
        return doc.get("custom_fx_vault_journal_entry")

    company = doc.get("company")
    company_info = frappe.db.get_value(
        "Company",
        company,
        ["default_currency", "cost_center", "write_off_account", "exchange_gain_loss_account"],
        as_dict=True,
    ) or {}
    home_currency = (company_info.get("default_currency") or "").strip().upper()

    profile = None
    if doc.get("pos_profile"):
        try:
            profile = frappe.get_cached_doc("POS Profile", doc.pos_profile)
        except Exception:
            profile = None
    if not home_currency and profile:
        home_currency = (profile.get("currency") or "").strip().upper()

    base_prec = _base_precision(home_currency)

    fx_rows = []
    for row in doc.get("payment_reconciliation") or []:
        tender_curr = (row.get("custom_tender_currency") or "").strip().upper()
        if not tender_curr or tender_curr == home_currency:
            continue
        if abs(flt(row.get("expected_amount"))) <= 0 and abs(flt(row.get("custom_closing_foreign_amount"))) <= 0:
            continue
        fx_rows.append(row)

    if not fx_rows:
        return None

    cost_center = (profile.get("cost_center") if profile else None) or company_info.get("cost_center")
    variance_account = (
        (profile.get("custom_cash_over_short_account") if profile else None)
        or (profile.get("write_off_account") if profile else None)
        or company_info.get("write_off_account")
    )
    rounding_account = (
        (profile.get("custom_fx_clearing_loss_account") if profile else None)
        or company_info.get("exchange_gain_loss_account")
        or variance_account
    )

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = "Journal Entry"
    je.company = company
    je.posting_date = doc.get("posting_date") or _today()
    je.multi_currency = 1
    je.user_remark = f"FX Till Vault Sweep for POS Closing Entry {doc.get('name')}"
    if doc.get("branch") and hasattr(je, "branch"):
        je.branch = doc.get("branch")

    total_debit = 0.0
    total_credit = 0.0

    for row in fx_rows:
        mode = row.get("mode_of_payment")
        tender_curr = (row.get("custom_tender_currency") or "").strip().upper()
        fx_prec = currency_precision(tender_curr)

        acct_row = frappe.db.get_value(
            "Mode of Payment Account",
            {"parent": mode, "company": company},
            ["default_account", "custom_fx_vault_account"],
            as_dict=True,
        ) or {}
        clearing_acct = acct_row.get("default_account")
        vault_acct = acct_row.get("custom_fx_vault_account")

        if not clearing_acct or not vault_acct:
            frappe.throw(
                f"POS-FX-ACCOUNT-INVALID: Mode of Payment {mode} requires both FX Till Clearing (default_account) and FX Vault (custom_fx_vault_account) for {company}.",
                frappe.ValidationError,
            )

        clearing_base = flt(row.get("expected_amount"), base_prec)
        expected_fx = flt(row.get("custom_expected_foreign_amount"), fx_prec)
        counted_fx = flt(row.get("custom_closing_foreign_amount"), fx_prec)

        if expected_fx > 0 and clearing_base > 0:
            shift_rate = flt(clearing_base / expected_fx, 9)
        else:
            rate_info = resolve_pos_exchange_rate(
                tender_curr,
                home_currency,
                posting_date=doc.get("posting_date"),
                company=company,
            )
            shift_rate = flt(rate_info["rate"], 9)

        # 1. Credit FX Till Clearing (company currency) to zero the clearing account
        if clearing_base > 0:
            je.append(
                "accounts",
                {
                    "account": clearing_acct,
                    "account_currency": home_currency,
                    "exchange_rate": 1.0,
                    "credit_in_account_currency": clearing_base,
                    "credit": clearing_base,
                    "debit_in_account_currency": 0.0,
                    "debit": 0.0,
                    "cost_center": cost_center,
                },
            )
            total_credit = flt(total_credit + clearing_base, base_prec)

        # 2. Debit FX Vault Account (native foreign currency at shift_rate)
        if counted_fx > 0:
            vault_base = flt(counted_fx * shift_rate, base_prec)
            je.append(
                "accounts",
                {
                    "account": vault_acct,
                    "account_currency": tender_curr,
                    "exchange_rate": shift_rate,
                    "debit_in_account_currency": counted_fx,
                    "debit": vault_base,
                    "credit_in_account_currency": 0.0,
                    "credit": 0.0,
                    "cost_center": cost_center,
                },
            )
            total_debit = flt(total_debit + vault_base, base_prec)

        # 3. Physical FX cash variance (overage/shortage -> Cash Over & Short / Write Off)
        fx_diff = flt(counted_fx - expected_fx, fx_prec)
        if abs(fx_diff) > 0:
            if not variance_account:
                frappe.throw(
                    f"POS-FX-ACCOUNT-INVALID: Configure a Write Off / Cash Over and Short account on POS Profile {doc.get('pos_profile')} or Company {company} to post FX variance.",
                    frappe.ValidationError,
                )
            var_base = flt(abs(fx_diff) * shift_rate, base_prec)
            if var_base > 0:
                if fx_diff < 0:
                    # Physical shortage: Dr Cash Over and Short
                    je.append(
                        "accounts",
                        {
                            "account": variance_account,
                            "account_currency": home_currency,
                            "exchange_rate": 1.0,
                            "debit_in_account_currency": var_base,
                            "debit": var_base,
                            "credit_in_account_currency": 0.0,
                            "credit": 0.0,
                            "cost_center": cost_center,
                        },
                    )
                    total_debit = flt(total_debit + var_base, base_prec)
                else:
                    # Physical overage: Cr Cash Over and Short
                    je.append(
                        "accounts",
                        {
                            "account": variance_account,
                            "account_currency": home_currency,
                            "exchange_rate": 1.0,
                            "credit_in_account_currency": var_base,
                            "credit": var_base,
                            "debit_in_account_currency": 0.0,
                            "debit": 0.0,
                            "cost_center": cost_center,
                        },
                    )
                    total_credit = flt(total_credit + var_base, base_prec)

    # 4. Sub-minor-unit rounding imbalance -> Exchange Gain/Loss
    imbalance = flt(total_debit - total_credit, base_prec)
    if abs(imbalance) > 0:
        if not rounding_account:
            frappe.throw(
                f"POS-FX-ACCOUNT-INVALID: Configure an Exchange Gain/Loss account on Company {company} or POS Profile {doc.get('pos_profile')}.",
                frappe.ValidationError,
            )
        if imbalance > 0:
            je.append(
                "accounts",
                {
                    "account": rounding_account,
                    "account_currency": home_currency,
                    "exchange_rate": 1.0,
                    "credit_in_account_currency": imbalance,
                    "credit": imbalance,
                    "debit_in_account_currency": 0.0,
                    "debit": 0.0,
                    "cost_center": cost_center,
                },
            )
        else:
            amt = abs(imbalance)
            je.append(
                "accounts",
                {
                    "account": rounding_account,
                    "account_currency": home_currency,
                    "exchange_rate": 1.0,
                    "debit_in_account_currency": amt,
                    "debit": amt,
                    "credit_in_account_currency": 0.0,
                    "credit": 0.0,
                    "cost_center": cost_center,
                },
            )

    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    je.submit()
    doc.db_set("custom_fx_vault_journal_entry", je.name, update_modified=False)
    return je.name


def cancel_fx_vault_sweep_journal_entry(doc, method=None) -> None:
    je_name = doc.get("custom_fx_vault_journal_entry")
    if not je_name:
        return
    if frappe.db.exists("Journal Entry", je_name):
        je = frappe.get_doc("Journal Entry", je_name)
        if je.docstatus == 1:
            je.flags.ignore_permissions = True
            je.cancel()


@frappe.whitelist()
def get_pos_fx_rates(pos_profile: str, posting_date: str | None = None) -> dict:
    """Return authoritative fresh/stale FX rate metadata for all FX payment modes on a POS Profile."""
    if not pos_profile:
        return {"home_currency": "", "rates": {}}
    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    home_currency = (profile.get("currency") or "").strip().upper()
    company = profile.get("company")
    eff_date = _getdate(posting_date or _today())

    rates = {}
    for row in profile.get("payments") or []:
        mode = row.get("mode_of_payment")
        if not mode:
            continue
        cfg = get_mode_payment_config(mode, company=company)
        tender_curr = cfg.get("custom_tender_currency") or ""
        if not tender_curr or tender_curr == home_currency:
            continue
        rate_type = cfg.get("custom_fx_rate_type") or "Buying"
        try:
            info = resolve_pos_exchange_rate(
                tender_curr,
                home_currency,
                posting_date=eff_date,
                company=company,
                rate_type=rate_type,
            )
            rates[mode] = {
                "status": "fresh",
                "tender_currency": tender_curr,
                "home_currency": home_currency,
                "rate": info["rate"],
                "rate_date": info["rate_date"],
                "rate_type": rate_type,
                "precision": currency_precision(tender_curr),
            }
        except frappe.ValidationError as exc:
            rates[mode] = {
                "status": "stale",
                "tender_currency": tender_curr,
                "home_currency": home_currency,
                "rate": None,
                "rate_date": None,
                "rate_type": rate_type,
                "precision": currency_precision(tender_curr),
                "error": str(exc),
            }

    return {
        "home_currency": home_currency,
        "allow_fx_rate_override": cint(profile.get("custom_allow_fx_rate_override")),
        "max_fx_variance_pct": flt(profile.get("custom_max_fx_variance_pct")),
        "rates": rates,
    }
