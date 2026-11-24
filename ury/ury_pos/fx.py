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
  gain/loss account — balanced at ERPNext's exact Journal Entry Account precision.
- Exchange rates come strictly from native ERPNext `Currency Exchange` (honoring active
  Workflow `workflow_state == "Approved"`) guarded by `Accounts Settings.stale_days`
  (fail-closed `stale_days = 1` when unset). Missing or stale rates throw
  `[POS-FX-RATE-STALE]` and never fall back to `1.0` or trust client-supplied rates.
- Payment reference regex patterns are length-bounded (<= 128 chars) and AST-validated
  against catastrophic backtracking (nested quantifiers, alternation inside repeats,
  adjacent unbounded repeats, and backreferences) before matching inputs (<= 256 chars).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import re
from urllib.parse import urlparse

try:
    from re import _parser as _re_parser
except ImportError:  # pragma: no cover - Python < 3.11 fallback
    import sre_parse as _re_parser  # type: ignore

import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.brand import coded_message


def _today() -> str:
    fn = getattr(getattr(frappe, "utils", None), "today", None)
    if callable(fn):
        try:
            res = fn()
            if isinstance(res, str) and res.strip():
                return res
        except Exception:
            pass
    return date.today().isoformat()


def _getdate(val=None) -> date:
    fn = getattr(getattr(frappe, "utils", None), "getdate", None)
    if callable(fn):
        try:
            res = fn(val)
            if isinstance(res, date):
                return res
        except Exception:
            pass
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
        try:
            res = fn(val, days)
            if isinstance(res, date):
                return res
        except Exception:
            pass
    return _getdate(val) + timedelta(days=int(days))


MAX_REFERENCE_PATTERN_LENGTH = 128
MAX_REFERENCE_VALUE_LENGTH = 256
GUEST_LABEL_MAX = 40

# Standard ISO 4217 non-2dp currencies for reliable site-less / fallback precision.
ZERO_DECIMAL_CURRENCIES = frozenset(
    {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "TZS", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
)
THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


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


def _je_debit_precision(je, home_currency: str | None = None) -> int:
    """Resolve the exact company-currency debit/credit precision ERPNext JournalEntry uses."""
    accounts = getattr(je, "accounts", None) or (je.get("accounts") if isinstance(je, dict) else None) or []
    if accounts:
        first_row = accounts[0]
        prec_fn = getattr(first_row, "precision", None)
        if callable(prec_fn):
            try:
                p = prec_fn("debit")
                if isinstance(p, int) and not isinstance(p, bool) and p >= 0:
                    return p
            except Exception:
                pass
    try:
        p = frappe.get_precision("Journal Entry Account", "debit")
        if isinstance(p, int) and not isinstance(p, bool) and p >= 0:
            return p
    except Exception:
        pass
    return max(2, _base_precision(home_currency))


def _ast_is_redos_safe(nodes, in_repeat: bool = False) -> bool:
    """Walk parsed regex AST and reject ReDoS-prone constructs:
    - Backreferences (GROUPREF / GROUPREF_EXISTS)
    - Lookarounds inside repeated groups
    - Nested quantifiers (any repeat inside a repeat whose upper bound > 1)
    - Alternation (BRANCH) inside a repeated group (e.g., (a|aa)+, (a|a?)+, (?:ab|a){2,})
    - Adjacent unbounded repeats (e.g., a+a+, .*.*)
    """
    max_repeat = _re_parser.MAXREPEAT
    repeat_ops = {_re_parser.MAX_REPEAT, _re_parser.MIN_REPEAT}
    if hasattr(_re_parser, "POSSESSIVE_REPEAT"):
        repeat_ops.add(_re_parser.POSSESSIVE_REPEAT)

    prev_unbounded = False
    for op, av in nodes:
        if op in (_re_parser.GROUPREF, _re_parser.GROUPREF_EXISTS):
            return False

        if op in (_re_parser.ASSERT, _re_parser.ASSERT_NOT):
            if in_repeat:
                return False
            if not _ast_is_redos_safe(av[1], in_repeat=False):
                return False
            prev_unbounded = False
            continue

        if op == _re_parser.BRANCH:
            if in_repeat:
                return False
            for branch in av[1]:
                if not _ast_is_redos_safe(branch, in_repeat=in_repeat):
                    return False
            prev_unbounded = False
            continue

        if op == _re_parser.SUBPATTERN:
            if not _ast_is_redos_safe(av[3], in_repeat=in_repeat):
                return False
            prev_unbounded = False
            continue

        if op in repeat_ops:
            _lo, hi, subpattern = av
            is_multi = hi == max_repeat or hi > 1
            is_unbounded = hi == max_repeat
            if in_repeat:
                # Any quantifier nested inside a multi-repeat risks exponential backtracking.
                return False
            if prev_unbounded and is_unbounded:
                # Adjacent unbounded repeats like a+a+ or .*.* risk polynomial backtracking.
                return False
            if not _ast_is_redos_safe(subpattern, in_repeat=is_multi):
                return False
            prev_unbounded = is_unbounded
            continue

        prev_unbounded = False

    return True


def is_safe_regex_pattern(pattern: str) -> bool:
    if not isinstance(pattern, str):
        return False
    if len(pattern) > MAX_REFERENCE_PATTERN_LENGTH:
        return False
    try:
        parsed = _re_parser.parse(pattern)
        if not _ast_is_redos_safe(parsed, in_repeat=False):
            return False
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
            coded_message(
                "PAY-REF-INVALID",
                _("{0} for {1} exceeds maximum length of {2} characters.").format(
                    label, mode_of_payment, MAX_REFERENCE_VALUE_LENGTH
                ),
            ),
            frappe.ValidationError,
        )

    if ref_mode == "Required" and not ref:
        frappe.throw(
            coded_message(
                "PAY-REF-REQUIRED",
                _("{0} is required for payment mode {1}.").format(label, mode_of_payment),
            ),
            frappe.ValidationError,
        )

    if ref and pattern:
        if not is_safe_regex_pattern(pattern):
            frappe.throw(
                coded_message(
                    "PAY-REF-INVALID",
                    _("Configured reference validation pattern for {0} is invalid or unsafe.").format(
                        mode_of_payment
                    ),
                ),
                frappe.ValidationError,
            )
        if not re.fullmatch(pattern, ref):
            frappe.throw(
                coded_message(
                    "PAY-REF-INVALID",
                    _("{0} for {1} does not match the required format.").format(label, mode_of_payment),
                ),
                frappe.ValidationError,
            )

    if date_required and (ref_mode == "Required" or bool(ref)) and not ref_date:
        frappe.throw(
            coded_message(
                "PAY-REF-REQUIRED",
                _("Reference date is required for payment mode {0}.").format(mode_of_payment),
            ),
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

    Never falls back to 1.0 or an unapproved/stale rate; throws `[POS-FX-RATE-STALE]`.
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
            coded_message(
                "FX-RATE-STALE",
                _("No approved fresh exchange rate found for {0} to {1} within {2} day(s) of {3}.").format(
                    src, dst, stale_days, eff_date
                ),
            ),
            frappe.ValidationError,
        )

    chosen = rows[0]
    rate = flt(chosen.get("exchange_rate"), 9)
    if rate <= 0:
        frappe.throw(
            coded_message(
                "FX-RATE-STALE",
                _("Exchange rate for {0} to {1} ({2}) is not positive.").format(
                    src, dst, chosen.get("name")
                ),
            ),
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


class _VerifiedFxOverrideProof:
    """In-process proof that a server-side FX rate override check succeeded; cannot be forged via REST JSON."""

    __slots__ = ("docname", "from_currency", "mode_of_payment", "rate", "to_currency")

    def __init__(self, docname: str, mode_of_payment: str, from_currency: str, to_currency: str, rate: float):
        self.docname = str(docname or "")
        self.mode_of_payment = mode_of_payment
        self.from_currency = from_currency
        self.to_currency = to_currency
        self.rate = flt(rate, 9)


def mark_verified_fx_override(
    doc,
    mode_of_payment: str,
    from_currency: str,
    to_currency: str,
    rate: float,
) -> None:
    if doc is None:
        return
    flags = getattr(doc, "flags", None)
    if flags is None:
        return
    proofs = flags.get("pos_verified_fx_overrides")
    if not isinstance(proofs, list):
        proofs = []
        flags["pos_verified_fx_overrides"] = proofs
    proofs.append(
        _VerifiedFxOverrideProof(
            docname=str(getattr(doc, "name", None) or doc.get("name") or ""),
            mode_of_payment=mode_of_payment,
            from_currency=(from_currency or "").strip().upper(),
            to_currency=(to_currency or "").strip().upper(),
            rate=rate,
        )
    )


def _has_verified_fx_override(
    doc,
    mode_of_payment: str,
    from_currency: str,
    to_currency: str,
    rate: float,
) -> bool:
    docname = str(getattr(doc, "name", None) or doc.get("name") or "")
    src = (from_currency or "").strip().upper()
    dst = (to_currency or "").strip().upper()
    target_rate = flt(rate, 9)

    flags = getattr(doc, "flags", None)
    if isinstance(flags, dict):
        for proof in flags.get("pos_verified_fx_overrides") or []:
            if (
                isinstance(proof, _VerifiedFxOverrideProof)
                and proof.docname == docname
                and proof.mode_of_payment == mode_of_payment
                and proof.from_currency == src
                and proof.to_currency == dst
                and abs(proof.rate - target_rate) <= 1e-6
            ):
                return True

    is_new_fn = getattr(doc, "is_new", None)
    if cint(doc.get("__islocal")) == 1 or (callable(is_new_fn) and is_new_fn()):
        return False

    pos_profile = doc.get("pos_profile")
    if not docname or not pos_profile:
        return False

    try:
        logs = frappe.get_all(
            "POS Authorisation Log",
            filters={
                "privilege": "fx_rate_override",
                "pos_profile": pos_profile,
                "reference_name": docname,
                "outcome": "Consumed",
            },
            fields=["claim_values", "creation"],
            order_by="creation desc",
            limit_page_length=20,
        )
    except Exception:
        return False

    doc_creation = doc.get("creation")
    doc_creation_str = str(doc_creation)[:19] if doc_creation else ""

    for entry in logs or []:
        if doc_creation_str and entry.get("creation"):
            if str(entry.get("creation"))[:19] < doc_creation_str:
                continue
        raw_claim = entry.get("claim_values") or entry.get("claim_json")
        if not raw_claim:
            continue
        try:
            parsed = json.loads(raw_claim) if isinstance(raw_claim, str) else raw_claim
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        if (
            parsed.get("mode_of_payment") == mode_of_payment
            and (parsed.get("from_currency") or "").strip().upper() == src
            and (parsed.get("to_currency") or "").strip().upper() == dst
            and abs(flt(parsed.get("override_rate"), 9) - target_rate) <= 1e-6
        ):
            return True

    return False


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
    invoice_doc=None,
) -> None:
    if cint(pos_profile.get("custom_allow_fx_rate_override")) != 1:
        frappe.throw(
            coded_message(
                "FX-RATE-STALE",
                _("FX rate override is not enabled on POS Profile {0}.").format(pos_profile.get("name")),
            ),
            frappe.PermissionError,
        )
    if override_rate <= 0:
        frappe.throw(
            coded_message(
                "FX-RATE-STALE",
                _("Override exchange rate for {0} must be positive.").format(tender_currency),
            ),
            frappe.ValidationError,
        )
    max_var_pct = flt(pos_profile.get("custom_max_fx_variance_pct"))
    if market_rate and market_rate > 0 and max_var_pct > 0:
        actual_var_pct = abs(override_rate - market_rate) / market_rate * 100.0
        if actual_var_pct > max_var_pct + 1e-9:
            frappe.throw(
                coded_message(
                    "FX-RATE-VARIANCE",
                    _("Override rate {0} deviates by {1:.2f}% from {2}, exceeding maximum allowed variance of {3:.2f}%.").format(
                        override_rate, actual_var_pct, market_rate, max_var_pct
                    ),
                ),
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
        validated_reason = override._validate_reason(pos_profile.get("name"), "fx_rate_override", claim)
        if invoice_name and callable(getattr(override, "_write_log", None)):
            try:
                if frappe.db.table_exists("POS Authorisation Log"):
                    override._write_log(
                        requester=frappe.session.user,
                        approver=frappe.session.user,
                        privilege="fx_rate_override",
                        pos_profile=pos_profile.get("name"),
                        company=pos_profile.get("company"),
                        reference_doctype="POS Invoice",
                        reference_name=invoice_name,
                        claim_json=override._canonical_claim_json(claim),
                        reason=validated_reason or (reason or "").strip(),
                        outcome="Consumed",
                        token_hash=None,
                        consumed_token_hash=None,
                        claim_hash=override._canonical_claim_hash(claim),
                        isolated=True,
                    )
            except Exception:
                pass
    else:
        frappe.throw(
            _("Manager authorisation is required to override an exchange rate.") + " [POS-AUTH-REQUIRED]",
            frappe.PermissionError,
        )

    if invoice_doc is not None:
        mark_verified_fx_override(invoice_doc, mode_of_payment, tender_currency, home_currency, override_rate)


def prepare_settlement_payments(
    payments,
    pos_profile,
    posting_date=None,
    invoice_name: str | None = None,
    is_return: bool = False,
    invoice_doc=None,
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
                    invoice_doc=invoice_doc,
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


def validate_invoice_fx_payments(doc, pos_profile=None) -> None:
    """Enforce G22 FX rate & converted base-amount integrity on POS Invoice validate/before_submit.

    Blocks direct REST POST/PUT `/api/resource/POS Invoice` or ORM bypasses that forge
    `custom_exchange_rate`, `custom_tender_amount`, or home-currency `amount` without
    an authoritative `Currency Exchange` rate or server-verified `#147` `fx_rate_override`.
    """
    if getattr(frappe.flags, "in_patch", False) or getattr(frappe.flags, "in_migrate", False):
        return
    if cint(doc.get("is_consolidated")) == 1:
        return

    payments = doc.get("payments") or []
    if not payments:
        return

    raw_company = doc.get("company")
    company = raw_company if isinstance(raw_company, str) else None
    profile = pos_profile
    if profile is None and isinstance(doc.get("pos_profile"), str) and doc.get("pos_profile"):
        try:
            profile = frappe.get_cached_doc("POS Profile", doc.get("pos_profile"))
        except Exception:
            profile = None

    raw_home = doc.get("currency") or (profile.get("currency") if profile else None)
    home_currency = raw_home.strip().upper() if isinstance(raw_home, str) else ""
    base_prec = _base_precision(home_currency)
    amount_tol = max(1e-6, 0.5 * (10 ** -base_prec)) if base_prec > 0 else 0.5
    eff_date = _getdate(doc.get("posting_date") if isinstance(doc.get("posting_date"), (str, date, datetime)) else _today())
    is_return = bool(cint(doc.get("is_return")))

    for row in payments:
        mode = row.get("mode_of_payment")
        if not mode:
            continue
        amt = flt(row.get("amount"))
        fx_amt = flt(row.get("custom_tender_amount"))
        row_fx_curr = (row.get("custom_tender_currency") or "").strip().upper()
        cfg = get_mode_payment_config(mode, company=company)
        tender_curr = (cfg.get("custom_tender_currency") or "").strip().upper()
        is_fx = bool(tender_curr and home_currency and tender_curr != home_currency)

        if not is_fx:
            if (row_fx_curr and row_fx_curr != home_currency) or abs(fx_amt) > 0:
                frappe.throw(
                    coded_message(
                        "FX-INVALID",
                        _("Foreign currency tender fields are not allowed on home-currency payment mode {0}.").format(
                            mode
                        ),
                    ),
                    frappe.ValidationError,
                )
            continue

        if abs(amt) <= 0 and abs(fx_amt) <= 0:
            continue

        if not is_return and (amt < 0 or fx_amt < 0):
            frappe.throw(
                coded_message(
                    "FX-INVALID",
                    _("Negative foreign currency payment amount for {0} is not allowed.").format(mode),
                ),
                frappe.ValidationError,
            )

        if row_fx_curr != tender_curr:
            frappe.throw(
                coded_message(
                    "FX-INVALID",
                    _("Payment mode {0} requires tender currency {1}.").format(mode, tender_curr),
                ),
                frappe.ValidationError,
            )

        if abs(fx_amt) <= 0:
            frappe.throw(
                coded_message(
                    "FX-INVALID",
                    _("Foreign tender amount is required for payment mode {0}.").format(mode),
                ),
                frappe.ValidationError,
            )

        rate = flt(row.get("custom_exchange_rate"), 9)
        if rate <= 0:
            frappe.throw(
                coded_message(
                    "FX-RATE-STALE",
                    _("A valid exchange rate is required for payment mode {0}.").format(mode),
                ),
                frappe.ValidationError,
            )

        fx_prec = currency_precision(tender_curr)
        # Allow up to 4dp proportional split on merged bills while validating conversion math.
        expected_base = flt(flt(fx_amt, max(fx_prec, 4)) * rate, base_prec)
        if abs(flt(amt) - expected_base) > amount_tol:
            frappe.throw(
                coded_message(
                    "FX-AMOUNT-MISMATCH",
                    _("Converted base amount {0} for {1} does not match {2} {3} at rate {4} ({5}).").format(
                        flt(amt), mode, fx_amt, tender_curr, rate, expected_base
                    ),
                ),
                frappe.ValidationError,
            )

        # Return invoices may match the original invoice's recorded exchange rate.
        if is_return and doc.get("return_against"):
            orig_rate = frappe.db.get_value(
                "Sales Invoice Payment",
                {
                    "parent": doc.get("return_against"),
                    "parenttype": "POS Invoice",
                    "mode_of_payment": mode,
                },
                "custom_exchange_rate",
            )
            if orig_rate and abs(flt(orig_rate, 9) - rate) <= 1e-6:
                continue

        rate_type = cfg.get("custom_fx_rate_type") or "Buying"
        market_info = None
        market_error = None
        try:
            market_info = resolve_pos_exchange_rate(
                tender_curr,
                home_currency,
                posting_date=eff_date,
                company=company,
                rate_type=rate_type,
            )
        except frappe.ValidationError as exc:
            market_error = exc

        if market_info is not None and abs(rate - flt(market_info["rate"], 9)) <= 1e-6:
            if not row.get("custom_exchange_rate_date"):
                row.custom_exchange_rate_date = market_info["rate_date"]
            continue

        # Rate differs from authoritative Currency Exchange (or market rate is missing/stale):
        # MUST satisfy POS Profile override policy, variance cap, AND server-verified #147 approval!
        if not profile or cint(profile.get("custom_allow_fx_rate_override")) != 1:
            if market_error is not None and not (profile and cint(profile.get("custom_allow_fx_rate_override")) == 1):
                raise market_error
            frappe.throw(
                coded_message(
                    "FX-RATE-STALE",
                    _("Exchange rate {0} for {1} does not match the approved rate and FX rate override is disabled.").format(
                        rate, mode
                    ),
                ),
                frappe.PermissionError,
            )

        max_var_pct = flt(profile.get("custom_max_fx_variance_pct"))
        if market_info is not None and flt(market_info["rate"]) > 0 and max_var_pct > 0:
            actual_var_pct = abs(rate - flt(market_info["rate"])) / flt(market_info["rate"]) * 100.0
            if actual_var_pct > max_var_pct + 1e-9:
                frappe.throw(
                    coded_message(
                        "FX-RATE-VARIANCE",
                        _("Override rate {0} deviates by {1:.2f}% from {2}, exceeding maximum allowed variance of {3:.2f}%.").format(
                            rate, actual_var_pct, market_info["rate"], max_var_pct
                        ),
                    ),
                    frappe.ValidationError,
                )

        if not _has_verified_fx_override(doc, mode, tender_curr, home_currency, rate):
            frappe.throw(
                _("Manager authorisation is required to override an exchange rate.") + " [POS-AUTH-REQUIRED]",
                frappe.PermissionError,
            )


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
                coded_message(
                    "FX-ACCOUNT-INVALID",
                    _("Foreign tender mode {0} ({1}) must configure a {2} FX Till Clearing account in default_account for {3}.").format(
                        mode, tender_currency, home_currency, company
                    ),
                ),
                frappe.ValidationError,
            )
        clearing_curr = (frappe.db.get_value("Account", clearing_acct, "account_currency") or "").strip().upper()
        if clearing_curr and clearing_curr != home_currency:
            frappe.throw(
                coded_message(
                    "FX-ACCOUNT-INVALID",
                    _("Foreign tender mode {0} default_account ({1}) must be in {2} (FX Till Clearing), not {3}. Configure the {4} drawer in custom_fx_vault_account instead.").format(
                        mode, clearing_acct, home_currency, clearing_curr, tender_currency
                    ),
                ),
                frappe.ValidationError,
            )

        if not vault_acct:
            frappe.throw(
                coded_message(
                    "FX-ACCOUNT-INVALID",
                    _("Foreign tender mode {0} ({1}) must configure custom_fx_vault_account in {1} for {2}.").format(
                        mode, tender_currency, company
                    ),
                ),
                frappe.ValidationError,
            )
        vault_curr = (frappe.db.get_value("Account", vault_acct, "account_currency") or "").strip().upper()
        if vault_curr and vault_curr != tender_currency:
            frappe.throw(
                coded_message(
                    "FX-ACCOUNT-INVALID",
                    _("Foreign tender mode {0} custom_fx_vault_account ({1}) must be in {2}, not {3}.").format(
                        mode, vault_acct, tender_currency, vault_curr
                    ),
                ),
                frappe.ValidationError,
            )


def validate_offline_peer_relay_url(url: str | None) -> None:
    raw = (url or "").strip()
    if not raw:
        return
    if len(raw) > 512:
        frappe.throw(
            coded_message(
                "OFFLINE-RELAY-INVALID",
                _("Offline peer relay URL must be 512 characters or fewer."),
            ),
            frappe.ValidationError,
        )
    if raw.startswith("/"):
        return
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.netloc:
        frappe.throw(
            coded_message(
                "OFFLINE-RELAY-INVALID",
                _("Offline peer relay URL must be a valid http(s)://, ws(s)://, or same-origin /path URL."),
            ),
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

    All company-currency debit/credit amounts match ERPNext JournalEntry's actual
    field precision (`debit`/`credit`) so non-integer multiples on 0dp/2dp/3dp currencies
    balance to 0.00 during `JournalEntry.validate()`.
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

    je_prec = _je_debit_precision(je, home_currency)

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
                coded_message(
                    "FX-ACCOUNT-INVALID",
                    _("Mode of Payment {0} requires both FX Till Clearing (default_account) and FX Vault (custom_fx_vault_account) for {1}.").format(
                        mode, company
                    ),
                ),
                frappe.ValidationError,
            )

        clearing_base = flt(row.get("expected_amount"), je_prec)
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

        # 2. Debit FX Vault Account (native foreign currency at shift_rate)
        if counted_fx > 0:
            vault_base = flt(counted_fx * shift_rate, je_prec)
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

        # 3. Physical FX cash variance (overage/shortage -> Cash Over & Short / Write Off)
        fx_diff = flt(counted_fx - expected_fx, fx_prec)
        if abs(fx_diff) > 0:
            if not variance_account:
                frappe.throw(
                    coded_message(
                        "FX-ACCOUNT-INVALID",
                        _("Configure a Write Off / Cash Over and Short account on POS Profile {0} or Company {1} to post FX variance.").format(
                            doc.get("pos_profile"), company
                        ),
                    ),
                    frappe.ValidationError,
                )
            var_base = flt(abs(fx_diff) * shift_rate, je_prec)
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

    # Recompute exact ERPNext company-currency debit/credit amounts before balancing so
    # 0dp UGX / 2dp USD / 3dp KWD precision differences never leave a residual imbalance.
    if callable(getattr(je, "set_amounts_in_company_currency", None)):
        je.set_amounts_in_company_currency()
    je_prec = _je_debit_precision(je, home_currency)
    total_debit = flt(sum(flt(r.get("debit"), je_prec) for r in (je.get("accounts") or [])), je_prec)
    total_credit = flt(sum(flt(r.get("credit"), je_prec) for r in (je.get("accounts") or [])), je_prec)

    # 4. Sub-minor-unit rounding imbalance -> Exchange Gain/Loss
    imbalance = flt(total_debit - total_credit, je_prec)
    if abs(imbalance) > 0:
        if not rounding_account:
            frappe.throw(
                coded_message(
                    "FX-ACCOUNT-INVALID",
                    _("Configure an Exchange Gain/Loss account on Company {0} or POS Profile {1}.").format(
                        company, doc.get("pos_profile")
                    ),
                ),
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
