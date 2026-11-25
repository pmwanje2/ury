"""Server-side POS discount engine, presets, reasons, authority limits, and return/split/merge rules.

Implements B2b (pos-override-contract.md and pos-pms-side-gaps.md §4.1–4.7):
- One model across POS and PMS: Approval Limit (Role: % lines; blank base = 0% at the till),
  active Discount Reason master, native Coupon Code / Pricing Rule presets, native ERPNext
  header & line discount fields, Item.max_discount cap, and single-use manager override tokens.
- Works both with and without siteops sales-discount-control@3 / sales-discount-reasons@1 installed.
"""

from contextlib import contextmanager
import datetime
import hashlib
import json
import math

import frappe
from frappe import _
from frappe.utils import cint, flt

from ury.ury_pos.approval_currency import company_approval_amount
from ury.ury_pos.auth_challenge import attach_auth_challenge

from ury.brand import coded_message

_CURRENCY_PRECISION_FIELDS = frozenset(
    {
        "rate",
        "base_rate",
        "price_list_rate",
        "base_price_list_rate",
        "rate_with_margin",
        "net_rate",
        "base_net_rate",
        "amount",
        "base_amount",
        "net_amount",
        "base_net_amount",
        "discount_amount",
        "base_discount_amount",
        "distributed_discount_amount",
        "total",
        "base_total",
        "net_total",
        "base_net_total",
        "grand_total",
        "base_grand_total",
        "rounded_total",
        "base_rounded_total",
        "total_taxes_and_charges",
        "base_total_taxes_and_charges",
        "paid_amount",
        "base_paid_amount",
        "outstanding_amount",
        "change_amount",
        "base_change_amount",
        "rounding_adjustment",
        "base_rounding_adjustment",
        "write_off_amount",
        "base_write_off_amount",
        "distributed_discount_amount",
        "net_rate",
        "base_net_rate",
        "net_amount",
        "base_net_amount",
        "tax_amount",
        "base_tax_amount",
        "tax_amount_after_discount_amount",
        "base_tax_amount_after_discount_amount",
    }
)


def _today_str():
    fn = getattr(frappe.utils, "today", None)
    if callable(fn):
        return str(fn())
    return datetime.date.today().isoformat()


def _date_str(value):
    fn = getattr(frappe.utils, "getdate", None)
    if callable(fn):
        return str(fn(value))
    return str(value)[:10]

ZERO_DECIMAL_CURRENCIES = frozenset({"UGX", "JPY", "KRW", "RWF", "TZS", "XAF", "XOF", "BIF", "GNF", "MGA", "VND"})
THREE_DECIMAL_CURRENCIES = frozenset({"KWD", "BHD", "OMR", "JOD", "TND", "IQD"})

FALLBACK_POS_REASONS = (
    {"name": "Corporate rate", "code": "Corporate rate", "description": "Corporate rate", "applies_to": "POS", "requires_supervisor": 0, "requires_note": 0, "sort_order": 10, "disabled": 0},
    {"name": "Complaint", "code": "Complaint", "description": "Complaint", "applies_to": "Both", "requires_supervisor": 0, "requires_note": 0, "sort_order": 20, "disabled": 0},
    {"name": "Staff", "code": "Staff", "description": "Staff", "applies_to": "POS", "requires_supervisor": 0, "requires_note": 0, "sort_order": 30, "disabled": 0},
    {"name": "Other", "code": "Other", "description": "Other", "applies_to": "Both", "requires_supervisor": 0, "requires_note": 1, "sort_order": 60, "disabled": 0},
)


from ury.ury_pos import override as _OVERRIDE_MODULE


def _get_override_module():
    """Return B2a's manager override module."""
    return _OVERRIDE_MODULE


def currency_precision(doc=None, currency=None):
    code = (currency or (doc.get("currency") if doc is not None else None) or "").strip().upper()
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    if doc is not None and callable(getattr(doc, "precision", None)):
        try:
            p = doc.precision("grand_total")
            if isinstance(p, int) and not isinstance(p, bool) and p >= 0:
                return p
        except Exception:
            pass
    return 2


@contextmanager
def _with_currency_precision(doc, prec=None):
    """Ensure ISO 0-decimal (UGX) and 3-decimal (KWD) currencies calculate and save at
    their true minor-unit precision even when Frappe System Settings currency_precision
    defaults to 2."""
    code = (doc.get("currency") if doc is not None else None) or ""
    if code.strip().upper() not in (ZERO_DECIMAL_CURRENCIES | THREE_DECIMAL_CURRENCIES):
        yield
        return
    if prec is None:
        prec = currency_precision(doc)

    saved = []

    def _patch_obj(obj):
        orig_fn = getattr(obj, "precision", None)
        if not callable(orig_fn):
            return
        had_instance_attr = "precision" in getattr(obj, "__dict__", {})
        orig_dict_val = obj.__dict__.get("precision") if had_instance_attr else None
        had_cache = hasattr(obj, "_precision")
        orig_cache = getattr(obj, "_precision", None)

        def _iso_precision(fieldname, parentfield=None, _orig=orig_fn, _prec=prec):
            if fieldname in _CURRENCY_PRECISION_FIELDS:
                return _prec
            return _orig(fieldname, parentfield)

        try:
            setattr(obj, "precision", _iso_precision)
            saved.append((obj, had_instance_attr, orig_dict_val, had_cache, orig_cache))
        except Exception:
            return

    _patch_obj(doc)
    for child_field in ("items", "taxes", "payments"):
        for row in doc.get(child_field) or []:
            _patch_obj(row)
    try:
        yield
    finally:
        for obj, had_attr, orig_val, had_cache, orig_cache in saved:
            try:
                if had_attr:
                    obj.__dict__["precision"] = orig_val
                else:
                    obj.__dict__.pop("precision", None)
                if had_cache:
                    obj._precision = orig_cache
                elif hasattr(obj, "_precision"):
                    delattr(obj, "_precision")
            except Exception:
                pass


with_currency_precision = _with_currency_precision


def _norm_number(value, places=4):
    if value is None:
        return None
    rounded = round(float(value), places)
    if abs(rounded) < 10 ** (-(places + 1)):
        return None
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def _clean_nonneg_number(raw, label, *, max_val=None):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        frappe.throw(coded_message("POS-DISC-INVALID", _("Invalid {0}.").format(label)), frappe.ValidationError)
    try:
        num = float(raw)
    except (TypeError, ValueError):
        frappe.throw(coded_message("POS-DISC-INVALID", _("Invalid {0}.").format(label)), frappe.ValidationError)
    if not math.isfinite(num) or num < 0:
        frappe.throw(
            coded_message("POS-DISC-INVALID", _("{0} must be a finite number of zero or more.").format(label)),
            frappe.ValidationError,
        )
    if max_val is not None and num > max_val:
        frappe.throw(
            coded_message("POS-DISC-RANGE", _("{0} cannot exceed {1}.").format(label, max_val)),
            frappe.ValidationError,
        )
    return num


def parse_role_limits(raw_text):
    """Parse Kamra / B1 'Role: %' lines; ignore blank, malformed, negative, >100, or non-finite entries."""
    limits = {}
    if not isinstance(raw_text, str):
        return limits
    for line in raw_text.splitlines():
        text = line.strip()
        if not text or ":" not in text or text.startswith("#"):
            continue
        role_part, pct_part = text.split(":", 1)
        role = role_part.strip()
        val_str = pct_part.strip().rstrip("%").strip()
        if not role or not val_str:
            continue
        try:
            pct = float(val_str)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pct) or pct < 0 or pct > 100:
            continue
        if role not in limits or pct > limits[role]:
            limits[role] = pct
    return limits


def parse_base_limit(raw_value):
    """At the till, a blank or invalid base limit is 0% (matching Kamra #32 and B1)."""
    if raw_value is None:
        return 0.0
    text = str(raw_value).strip().rstrip("%").strip()
    if not text:
        return 0.0
    try:
        pct = float(text)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(pct) or pct < 0 or pct > 100:
        return 0.0
    return pct


def company_discount_policy(company):
    """Read company discount authority from Approval Limit; defaults to 0% when absent."""
    default = {"base_limit": 0.0, "role_limits": {}, "approver_role": None, "has_approval_limit": False}
    if not company:
        return default
    try:
        if not frappe.db.exists("DocType", "Approval Limit"):
            return default
        meta = frappe.get_meta("Approval Limit")
        fields = []
        for f in ("max_document_discount_percent", "discount_limits_by_role", "discount_approver_role"):
            if meta and meta.has_field(f):
                fields.append(f)
        if not fields:
            return default
        row = frappe.db.get_value("Approval Limit", company, fields, as_dict=True)
        if not row:
            return default
        return {
            "base_limit": parse_base_limit(row.get("max_document_discount_percent")),
            "role_limits": parse_role_limits(row.get("discount_limits_by_role")),
            "approver_role": row.get("discount_approver_role") or None,
            "has_approval_limit": True,
        }
    except Exception:
        return default


def _privilege_rules_for(company, privilege, pos_profile=None):
    """Read POS Privilege Rule rows for (company, privilege) when B2a's DocType exists.
    Returns (has_any_rules_for_privilege, role_to_narrowest_rule)."""
    if not company or not privilege:
        return False, {}
    try:
        if not frappe.db.exists("DocType", "POS Privilege Rule"):
            return False, {}
        rows = frappe.get_all(
            "POS Privilege Rule",
            filters={"company": company, "privilege": privilege},
            fields=[
                "role", "pos_profile", "may_perform", "may_authorise",
                "max_percent", "max_amount", "requires_reason",
                "allow_self_approval", "require_clocked_in", "privilege_group",
            ],
        ) or []
    except Exception:
        return False, {}

    if not rows:
        return False, {}

    # Prefer narrower pos_profile rule over company-wide (pos_profile empty/None)
    resolved = {}
    for row in rows:
        role = row.get("role")
        if not role:
            continue
        r_profile = (row.get("pos_profile") or "").strip()
        if r_profile:
            if pos_profile and r_profile == pos_profile:
                resolved[role] = row
        else:
            if role not in resolved:
                resolved[role] = row
    return True, resolved


def user_discount_limit(user, company, pos_profile=None, *, approval_amount=None, approval_basis=None):
    """Effective solo discount % limit for `user` at `pos_profile`:
    - Starts from company Approval Limit ('Role: %' lines; blank base = 0%).
    - When POS Privilege Rule rows exist for (company, 'discount'), Amendment 1 applies:
      no rule for a role = 0% alone; may_perform=0 = 0% alone; min(Approval Limit, rule.max_percent)
      wins when rule.max_percent > 0 (0 on rule defers to Approval Limit).
    - approval_amount and approval_basis are in company currency.
    - Administrator and System Manager get no free pass."""
    policy = company_discount_policy(company)
    roles = frappe.get_roles(user) or []
    has_rules, rules_by_role = _privilege_rules_for(company, "discount", pos_profile=pos_profile)

    if not has_rules:
        matched = [policy["role_limits"][r] for r in roles if r in policy["role_limits"]]
        if matched:
            return max(matched)
        return policy["base_limit"]

    effective = []
    has_approval_limit = bool(policy.get("has_approval_limit"))
    for role in roles:
        rule = rules_by_role.get(role)
        if not rule or cint(rule.get("may_perform")) != 1:
            continue
        amount_cap = flt(rule.get("max_amount"))
        if approval_amount is not None and amount_cap > 0 and approval_amount > amount_cap + 1e-9:
            continue
        rule_max = flt(rule.get("max_percent"))
        if has_approval_limit:
            app_lim = policy["role_limits"].get(role, policy["base_limit"])
            role_limit = min(app_lim, rule_max) if rule_max > 0 else app_lim
        else:
            role_limit = max(0.0, rule_max)
        if approval_basis and amount_cap > 0:
            role_limit = min(role_limit, amount_cap * 100 / approval_basis)
        effective.append(role_limit)
    return max(effective) if effective else 0.0


def _approver_discount_ceiling(approver, company, pos_profile=None, privilege="discount"):
    """Return the maximum discount % `approver` is authorised to approve, never exceeding their
    effective company Approval Limit / POS Privilege Rule ceiling."""
    if not approver:
        return 0.0
    if privilege == "complimentary":
        return 100.0
    policy = company_discount_policy(company)
    roles = set(frappe.get_roles(approver) or [])
    has_rules, rules_by_role = _privilege_rules_for(company, "discount", pos_profile=pos_profile)
    has_approval_limit = bool(policy.get("has_approval_limit"))

    if has_approval_limit:
        approver_role = (policy.get("approver_role") or "").strip()
        if not approver_role or approver_role in ("All", "Guest", "Desk User", "Administrator", "System Manager"):
            approver_role = "Owner Approver"
        if approver_role in roles and approver_role not in policy["role_limits"]:
            return 100.0
        if has_rules:
            caps = []
            for role in roles:
                rule = rules_by_role.get(role)
                if not rule or cint(rule.get("may_authorise")) != 1:
                    continue
                app_lim = policy["role_limits"][role] if role in policy["role_limits"] else policy["base_limit"]
                rule_max = flt(rule.get("max_percent"))
                caps.append(min(app_lim, rule_max) if rule_max > 0 else app_lim)
            if caps:
                return max(caps)
            if approver_role in roles:
                return policy["role_limits"].get(approver_role, 100.0)
            return 0.0
        matched = [policy["role_limits"][r] for r in roles if r in policy["role_limits"]]
        return max(matched) if matched else policy["base_limit"]

    if has_rules:
        caps = []
        for role in roles:
            rule = rules_by_role.get(role)
            if not rule or cint(rule.get("may_authorise")) != 1:
                continue
            rule_max = flt(rule.get("max_percent"))
            caps.append(rule_max if rule_max > 0 else 100.0)
        return max(caps) if caps else 0.0

    return 100.0


def user_can_remove_discount(user, company, pos_profile=None, invoice_printed=0):
    """Whether `user` may remove a discount without manager PIN approval."""
    roles = frappe.get_roles(user) or []
    has_rules, rules_by_role = _privilege_rules_for(company, "discount_remove", pos_profile=pos_profile)
    if has_rules:
        return any(
            (role in rules_by_role and cint(rules_by_role[role].get("may_perform")) == 1)
            for role in roles
        )
    if cint(invoice_printed) == 1:
        return bool(set(roles) & {"URY Manager", "URY Admin"})
    return True


def canonical_remove_claim(doc, row=None):
    prec = currency_precision(doc)
    return {
        "action": "remove",
        "row": str(row).strip() if row else None,
        "discount_amount": _norm_number(doc.get("discount_amount"), prec) or 0,
        "additional_discount_percentage": _norm_number(doc.get("additional_discount_percentage"), 4) or 0,
    }


def enforce_remove_discount_auth(bill, pos_profile_doc, row=None, approval=None):
    """Gate discount removal under the `discount_remove` privilege (Amendment 1)."""
    company = bill.get("company") or pos_profile_doc.get("company")
    claim = canonical_remove_claim(bill, row=row)
    token = str(approval).strip() if approval is not None and str(approval).strip() else None
    if token:
        override_mod = _get_override_module()
        consumed = override_mod.consume(
            token,
            "discount_remove",
            pos_profile_doc.name,
            bill.name,
            claim,
        )
        approver = consumed.get("approver")
        if not approver or approver == frappe.session.user:
            frappe.throw(
                coded_message("POS-AUTH-SELF", _("You cannot approve your own request.")),
                frappe.PermissionError,
            )
        return consumed

    if user_can_remove_discount(
        frappe.session.user,
        company,
        pos_profile=pos_profile_doc.name,
        invoice_printed=bill.get("invoice_printed"),
    ):
        return None

    frappe.throw(
        coded_message("POS-AUTH-REQUIRED", _("Manager approval is required to remove this discount.")),
        frappe.PermissionError,
    )


def _above_limit(discount_amount, basis_amount, explicit_percent, limit_percent, precision):
    """Exact integer-unit comparison so tiny discounts cannot round down to 0%."""
    if explicit_percent is not None and flt(explicit_percent) > flt(limit_percent):
        return True
    scale = 10 ** max(0, int(precision))
    disc_units = int(round(flt(discount_amount) * scale))
    basis_units = int(round(flt(basis_amount) * scale))
    if disc_units <= 0:
        return False
    if basis_units <= 0:
        return True
    limit_scale = 10000
    limit_units = int(round(flt(limit_percent) * limit_scale))
    return disc_units * 100 * limit_scale > basis_units * limit_units


def _exceeds_approved_percent(summary, doc, approved_percent, precision):
    """Match siteops#8620 sales-discount-control@3 exact minor-unit half-up limit comparison."""
    if summary["discount_total"] <= 0 and flt(doc.get("additional_discount_percentage")) <= 0:
        return False
    pct_val = flt(approved_percent)
    if pct_val < 0:
        return True
    header_pct = flt(doc.get("additional_discount_percentage"))
    if header_pct > pct_val + 1e-9:
        return True
    scale = 10 ** max(0, int(precision))
    disc_units = int(round(flt(summary["discount_total"]) * scale))
    basis_units = int(round(flt(summary["discount_basis"]) * scale))
    if disc_units <= 0:
        return False
    if basis_units <= 0:
        return True
    limit_units = int(round(pct_val * 1000000.0))
    limit_scale = 1000000
    while limit_scale > 1 and limit_units % 10 == 0:
        limit_units //= 10
        limit_scale //= 10
    if limit_units == 0:
        return True
    denom = 100 * limit_scale
    allowed_units = (2 * basis_units * limit_units + denom) // (2 * denom)
    return disc_units > allowed_units


def canonical_split_discount_claim(source_invoice, items_to_move, *, percent, reason=None):
    """Canonical manager override claim when split_bill elevates a check's effective discount %."""
    normalized_items = []
    for entry in items_to_move or []:
        if not isinstance(entry, dict):
            continue
        row_id = str(entry.get("name") or "").strip()
        qty = _norm_number(entry.get("qty"), 4)
        if not row_id or qty is None or qty <= 0:
            continue
        normalized_items.append({"name": row_id, "qty": qty})
    normalized_items.sort(key=lambda item: item["name"])
    return {
        "action": "split_discount",
        "source_invoice": str(source_invoice or "").strip(),
        "items": normalized_items,
        "percent": _norm_number(percent, 4),
        "reason": (str(reason).strip() or None) if reason else None,
    }


def canonical_discount_claim(*, percent=None, amount=None, coupon=None, lines=None, reason=None, note=None, precision=2):
    norm_lines = []
    for entry in lines or []:
        row_id = str(entry.get("row") or "").strip()
        if not row_id:
            continue
        l_pct = _norm_number(entry.get("percent"), 4)
        l_amt = _norm_number(entry.get("amount"), precision)
        if l_pct is None and l_amt is None:
            continue
        norm_lines.append({"row": row_id, "percent": l_pct, "amount": l_amt})
    norm_lines.sort(key=lambda item: item["row"])
    return {
        "percent": _norm_number(percent, 4),
        "amount": _norm_number(amount, precision),
        "coupon": (str(coupon).strip() or None) if coupon else None,
        "lines": norm_lines,
        "reason": (str(reason).strip() or None) if reason else None,
        "note": (str(note).strip() or None) if note else None,
    }


def claim_hash(claim):
    raw = json.dumps(claim, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_reason(reason_code, *, required=True):
    cleaned = str(reason_code).strip() if reason_code is not None else ""
    if not cleaned:
        if required:
            frappe.throw(
                coded_message("POS-DISC-REASON-REQUIRED", _("Select a discount reason.")),
                frappe.ValidationError,
            )
        return None

    if frappe.db.exists("DocType", "Discount Reason"):
        row = frappe.db.get_value(
            "Discount Reason",
            cleaned,
            ["name", "code", "description", "applies_to", "requires_supervisor", "requires_note", "disabled"],
            as_dict=True,
        )
        if not row:
            frappe.throw(
                coded_message("POS-DISC-REASON-INVALID", _("Discount reason {0} does not exist.").format(cleaned)),
                frappe.ValidationError,
            )
        if cint(row.get("disabled")) == 1:
            frappe.throw(
                coded_message("POS-DISC-REASON-DISABLED", _("Discount reason {0} is disabled.").format(cleaned)),
                frappe.ValidationError,
            )
        if row.get("applies_to") not in ("POS", "Both"):
            frappe.throw(
                coded_message("POS-DISC-REASON-SCOPE", _("Discount reason {0} cannot be used at the till.").format(cleaned)),
                frappe.ValidationError,
            )
        return frappe._dict(row)

    by_code = {r["code"]: r for r in FALLBACK_POS_REASONS}
    if cleaned in by_code:
        return frappe._dict(by_code[cleaned])

    frappe.throw(
        coded_message("POS-DISC-REASON-INVALID", _("Select a valid POS discount reason.")),
        frappe.ValidationError,
    )


def _date_in_range(check_date, valid_from, valid_upto):
    try:
        d = _date_str(check_date) if check_date else _today_str()
    except Exception:
        d = _today_str()
    if valid_from and d < _date_str(valid_from):
        return False
    if valid_upto and d > _date_str(valid_upto):
        return False
    return True


def _resolve_coupon_preset(coupon_code, company, price_list=None, posting_date=None):
    cleaned = str(coupon_code or "").strip()
    if not cleaned:
        return None

    coupon_meta = frappe.get_meta("Coupon Code")
    has_reason_field = bool(coupon_meta and coupon_meta.has_field("custom_discount_reason"))
    coupon_fields = [
        "name", "coupon_name", "coupon_code", "coupon_type", "pricing_rule",
        "valid_from", "valid_upto", "maximum_use", "used",
    ]
    if has_reason_field:
        coupon_fields.append("custom_discount_reason")

    coupon = None
    if frappe.db.exists("Coupon Code", cleaned):
        coupon = frappe.db.get_value("Coupon Code", cleaned, coupon_fields, as_dict=True)
    if not coupon:
        coupon = frappe.db.get_value("Coupon Code", {"coupon_code": cleaned}, coupon_fields, as_dict=True)
    if not coupon:
        frappe.throw(
            coded_message("POS-DISC-COUPON-INVALID", _("Coupon preset {0} was not found.").format(cleaned)),
            frappe.ValidationError,
        )

    if not _date_in_range(posting_date, coupon.get("valid_from"), coupon.get("valid_upto")):
        frappe.throw(
            coded_message("POS-DISC-COUPON-EXPIRED", _("Coupon {0} is outside its valid dates.").format(cleaned)),
            frappe.ValidationError,
        )
    max_use = cint(coupon.get("maximum_use"))
    used = cint(coupon.get("used"))
    if max_use > 0 and used >= max_use:
        frappe.throw(
            coded_message("POS-DISC-COUPON-EXHAUSTED", _("Coupon {0} has reached its maximum uses.").format(cleaned)),
            frappe.ValidationError,
        )

    rule_name = coupon.get("pricing_rule")
    if not rule_name:
        frappe.throw(
            coded_message("POS-DISC-COUPON-INVALID", _("Coupon {0} has no linked Pricing Rule.").format(cleaned)),
            frappe.ValidationError,
        )
    rule = frappe.db.get_value(
        "Pricing Rule",
        rule_name,
        [
            "name", "title", "apply_on", "selling", "disable", "coupon_code_based",
            "price_or_product_discount", "rate_or_discount", "discount_percentage",
            "discount_amount", "apply_discount_on", "company", "for_price_list",
            "valid_from", "valid_upto",
        ],
        as_dict=True,
    )
    if not rule or cint(rule.get("disable")) == 1 or not cint(rule.get("selling")):
        frappe.throw(
            coded_message("POS-DISC-COUPON-INVALID", _("Pricing Rule for coupon {0} is inactive.").format(cleaned)),
            frappe.ValidationError,
        )
    if not cint(rule.get("coupon_code_based")):
        frappe.throw(
            coded_message("POS-DISC-COUPON-INVALID", _("Pricing Rule for coupon {0} is not coupon-based.").format(cleaned)),
            frappe.ValidationError,
        )
    if (rule.get("apply_on") or "") != "Transaction" or (rule.get("price_or_product_discount") or "Price") != "Price":
        frappe.throw(
            coded_message(
                "POS-DISC-COUPON-SCOPE",
                _("Coupon {0} applies to specific items and cannot be used as a check-level preset.").format(cleaned),
            ),
            frappe.ValidationError,
        )
    if rule.get("company") and company and rule.get("company") != company:
        frappe.throw(
            coded_message("POS-DISC-COUPON-SCOPE", _("Coupon {0} is not valid for company {1}.").format(cleaned, company)),
            frappe.ValidationError,
        )
    if rule.get("for_price_list") and price_list and rule.get("for_price_list") != price_list:
        frappe.throw(
            coded_message("POS-DISC-COUPON-SCOPE", _("Coupon {0} is not valid for this price list.").format(cleaned)),
            frappe.ValidationError,
        )
    if not _date_in_range(posting_date, rule.get("valid_from"), rule.get("valid_upto")):
        frappe.throw(
            coded_message("POS-DISC-COUPON-EXPIRED", _("Pricing Rule for coupon {0} is outside its valid dates.").format(cleaned)),
            frappe.ValidationError,
        )

    return frappe._dict(
        name=coupon.get("name"),
        coupon_code=coupon.get("coupon_code") or coupon.get("name"),
        title=rule.get("title") or coupon.get("coupon_name") or coupon.get("name"),
        pricing_rule=rule.get("name"),
        apply_on=rule.get("apply_on") or "Transaction",
        discount_percentage=flt(rule.get("discount_percentage")),
        discount_amount=flt(rule.get("discount_amount")),
        apply_discount_on=rule.get("apply_discount_on") or "Grand Total",
        discount_reason=coupon.get("custom_discount_reason") if has_reason_field else None,
    )


def _allow_open_discounts(profile):
    """Native POS Profile.allow_discount_change governs open discounts (ERPNext default is 0)."""
    val = profile.get("allow_discount_change")
    if val is not None:
        return cint(val) == 1
    return False


def _item_max_discount(item_code):
    if not item_code:
        return 0.0
    try:
        return flt(frappe.get_cached_value("Item", item_code, "max_discount"))
    except Exception:
        return 0.0


def _check_item_max_discount(item_row, discount_pct=None, discount_amt=None):
    max_disc = _item_max_discount(item_row.get("item_code"))
    if not max_disc or max_disc <= 0:
        return
    pct = flt(discount_pct if discount_pct is not None else item_row.get("discount_percentage"))
    amt = flt(discount_amt if discount_amt is not None else item_row.get("discount_amount"))
    base_rate = flt(item_row.get("price_list_rate") or item_row.get("rate"))
    if pct > max_disc + 1e-9 or (base_rate > 0 and amt > base_rate * max_disc / 100.0 + 1e-9):
        frappe.throw(
            coded_message(
                "POS-DISC-ITEM-CAP",
                _("Maximum discount for {0} is {1}%.").format(item_row.get("item_name") or item_row.get("item_code"), max_disc),
            ),
            frappe.ValidationError,
        )


def _is_auto_pricing_rule_row(item_row):
    """True when a row's discount comes strictly from an automatic Pricing Rule (exempt per SAP/§4.3)."""
    pr = item_row.get("pricing_rules")
    if not pr:
        return False
    text = str(pr).strip()
    return bool(text and text != "[]")


def _received_tender_total(doc):
    total = 0.0
    for row in doc.get("payments") or []:
        amt = flt(row.get("amount"))
        if amt > 0 and cint(row.get("custom_tender_received")) == 1:
            total += amt
    return total


def _read_meta(doc):
    raw = doc.get("ury_discount_meta")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _write_discount_audit_fields(doc, *, reason=None, reason_description=None, note=None, coupon=None, approver=None, approved_percent=None, line_reasons=None, c_hash=None):
    meta = frappe.get_meta("POS Invoice")
    if meta and meta.has_field("custom_discount_reason"):
        doc.set("custom_discount_reason", reason or None)
    if meta and meta.has_field("custom_discount_note"):
        doc.set("custom_discount_note", note or None)
    if meta and meta.has_field("custom_discount_approver"):
        doc.set("custom_discount_approver", approver or None)
    if meta and meta.has_field("custom_discount_approved_percent"):
        doc.set("custom_discount_approved_percent", flt(approved_percent) if approver else 0.0)
    if meta and meta.has_field("ury_discount_meta"):
        if not reason and not coupon and not approver and not line_reasons:
            doc.set("ury_discount_meta", None)
        else:
            payload = {
                "reason": reason or None,
                "reason_description": reason_description or reason or None,
                "note": note or None,
                "coupon_code": coupon or None,
                "approver": approver or None,
                "approved_percent": flt(approved_percent) if approver else 0.0,
                "line_reasons": line_reasons or {},
                "claim_hash": c_hash or None,
            }
            doc.set("ury_discount_meta", json.dumps(payload, sort_keys=True))


def _row_manual_discount_map(doc, precision):
    """Map row name -> manual line discount amount for active non-automatic rows."""
    out = {}
    for item in (doc.get("items") or []) if doc is not None else []:
        qty = flt(item.get("qty"))
        if qty <= 0 or cint(item.get("is_disposable")) or _is_auto_pricing_rule_row(item):
            continue
        rname = str(item.get("name") or "")
        if not rname:
            continue
        plr = flt(item.get("price_list_rate")) or flt(item.get("rate"))
        rate = flt(item.get("rate"))
        row_amt = flt(item.get("amount"))
        row_da = flt(item.get("discount_amount"))
        row_dp = flt(item.get("discount_percentage"))
        diff = 0.0
        if plr > 0 and rate >= 0 and plr > rate:
            gross_row = flt(plr * qty, precision)
            post_row = flt(row_amt, precision) if (row_amt > 0 or rate == 0) else flt(rate * qty, precision)
            diff = max(0.0, flt(gross_row - post_row, precision))
        diff = max(diff, flt(row_da * qty, precision), flt(plr * qty * row_dp / 100.0, precision))
        out[rname] = diff
    return out


def _compute_manual_discount_summary(doc, precision):
    """Compute pre-discount total, manual line discount total, header discount total,
    effective manual %, and after total from the invoice state.

    Isolates `charge_type == "Actual"` tax/service-charge rows so adding an Actual charge
    alongside a matching discount_amount can NEVER cancel discount_total to 0 or dilute
    effective_percent.
    """
    line_disc_total = 0.0
    pre_items_gross = 0.0
    for item in doc.get("items") or []:
        qty = flt(item.get("qty"))
        if qty <= 0 or cint(item.get("is_disposable")):
            continue
        plr = flt(item.get("price_list_rate")) or flt(item.get("rate"))
        rate = flt(item.get("rate"))
        row_amt = flt(item.get("amount"))
        if _is_auto_pricing_rule_row(item):
            pre_items_gross += row_amt
        else:
            gross_row = flt(plr * qty, precision)
            pre_items_gross += gross_row
            if plr > 0 and rate >= 0 and plr > rate:
                post_row = flt(row_amt, precision) if (row_amt > 0 or rate == 0) else flt(rate * qty, precision)
                row_disc = max(0.0, flt(gross_row - post_row, precision))
            else:
                row_disc = 0.0
            line_disc_total += max(
                row_disc,
                flt(flt(item.get("discount_amount")) * qty, precision),
                flt(gross_row * flt(item.get("discount_percentage")) / 100.0, precision),
            )

    header_disc = max(0.0, flt(doc.get("discount_amount"), precision))
    grand_after = flt(doc.get("grand_total"), precision)
    rounded_after = flt(doc.get("rounded_total") or doc.get("grand_total"), precision)
    tax_after = flt(doc.get("total_taxes_and_charges"), precision)

    # Separate percentage taxes from charge_type="Actual" fixed charges
    taxes = doc.get("taxes") or []
    exclusive_pct_rate = sum(
        flt(t.get("rate"))
        for t in taxes
        if not cint(t.get("included_in_print_rate")) and (t.get("charge_type") or "") != "Actual"
    )
    actual_charges = sum(
        flt(t.get("tax_amount"))
        for t in taxes
        if (t.get("charge_type") or "") == "Actual" and not cint(t.get("included_in_print_rate"))
    )

    if doc.get("items"):
        if exclusive_pct_rate > 0:
            discount_basis = flt(pre_items_gross * (1.0 + exclusive_pct_rate / 100.0), precision)
            line_disc_gross = flt(line_disc_total * (1.0 + exclusive_pct_rate / 100.0), precision)
        else:
            discount_basis = flt(pre_items_gross, precision)
            line_disc_gross = flt(line_disc_total, precision)
        total_discount = flt(line_disc_gross + header_disc, precision)
        before_total = flt(discount_basis + actual_charges, precision)
    else:
        discount_basis = flt(max(0.0, grand_after - actual_charges) + header_disc, precision)
        before_total = flt(grand_after + header_disc, precision)
        line_disc_gross = 0.0
        total_discount = header_disc

    effective_pct = 0.0
    if discount_basis > 0 and total_discount > 0:
        effective_pct = round(min(100.0, (total_discount / discount_basis) * 100.0), 4)
    elif flt(doc.get("additional_discount_percentage")) > 0:
        effective_pct = round(flt(doc.get("additional_discount_percentage")), 4)

    return {
        "before_total": flt(before_total, precision),
        "discount_basis": flt(discount_basis, precision),
        "discount_total": flt(total_discount, precision),
        "header_discount_amount": flt(header_disc, precision),
        "line_discount_total": flt(line_disc_gross, precision),
        "tax_total": flt(tax_after, precision),
        "after_total": flt(rounded_after, precision),
        "grand_total": flt(grand_after, precision),
        "rounded_total": flt(rounded_after, precision),
        "net_total": flt(doc.get("net_total"), precision),
        "effective_percent": effective_pct,
    }


def parse_discount_request(
    *,
    discount_percent=None,
    discount_amount=None,
    discount_reason=None,
    discount_note=None,
    coupon_code=None,
    line_discounts=None,
    approval=None,
    additionalDiscount=None,
    clear_discount=0,
    complimentary=0,
):
    """Normalize and validate the incoming discount payload before touching any document."""
    if isinstance(line_discounts, str):
        try:
            line_discounts = frappe.parse_json(line_discounts)
        except Exception:
            frappe.throw(coded_message("POS-DISC-INVALID", _("Invalid line discounts payload.")), frappe.ValidationError)

    is_explicit_comp = cint(complimentary) == 1

    # Legacy additionalDiscount parameter: NEVER apply old 100% bypass; map into discount_percent
    # or refuse if conflicting with the new parameters.
    legacy_pct = _clean_nonneg_number(additionalDiscount, "discount percentage", max_val=100.0)
    if legacy_pct is not None and legacy_pct > 0:
        if discount_percent not in (None, "") or discount_amount not in (None, "") or coupon_code:
            frappe.throw(
                coded_message("POS-DISC-INVALID", _("Use discount_percent or discount_amount instead of additionalDiscount.")),
                frappe.ValidationError,
            )
        discount_percent = legacy_pct

    pct = _clean_nonneg_number(discount_percent, "discount percentage", max_val=100.0)
    amt = _clean_nonneg_number(discount_amount, "discount amount")

    if is_explicit_comp:
        if amt is not None and amt > 0:
            frappe.throw(
                coded_message("POS-DISC-CONFLICT", _("Cannot combine complimentary with a flat amount discount.")),
                frappe.ValidationError,
            )
        if coupon_code is not None and str(coupon_code).strip():
            frappe.throw(
                coded_message("POS-DISC-CONFLICT", _("Cannot combine complimentary with a coupon preset.")),
                frappe.ValidationError,
            )
        if pct is not None and abs(pct - 100.0) > 1e-6:
            frappe.throw(
                coded_message("POS-DISC-INVALID", _("Complimentary discount must be 100%.")),
                frappe.ValidationError,
            )
        pct = 100.0

    if pct is not None and pct > 0 and amt is not None and amt > 0:
        frappe.throw(
            coded_message("POS-DISC-CONFLICT", _("Specify either a percentage discount or a flat amount, not both.")),
            frappe.ValidationError,
        )

    coupon = str(coupon_code).strip() if coupon_code is not None and str(coupon_code).strip() else None
    if coupon and ((pct is not None and pct > 0) or (amt is not None and amt > 0)):
        frappe.throw(
            coded_message("POS-DISC-CONFLICT", _("Cannot combine a coupon preset with an open check discount.")),
            frappe.ValidationError,
        )

    parsed_lines = []
    if line_discounts is not None:
        if not isinstance(line_discounts, (list, tuple)):
            frappe.throw(coded_message("POS-DISC-INVALID", _("line_discounts must be a list.")), frappe.ValidationError)
        seen_rows = set()
        for entry in line_discounts:
            if not isinstance(entry, dict):
                frappe.throw(coded_message("POS-DISC-INVALID", _("Invalid line discount entry.")), frappe.ValidationError)
            row_id = str(entry.get("row") or "").strip()
            if not row_id:
                frappe.throw(coded_message("POS-DISC-INVALID", _("Line discount is missing its row identifier.")), frappe.ValidationError)
            if row_id in seen_rows:
                frappe.throw(coded_message("POS-DISC-INVALID", _("Duplicate line discount for row {0}.").format(row_id)), frappe.ValidationError)
            seen_rows.add(row_id)
            l_pct = _clean_nonneg_number(entry.get("percent"), "line discount percentage", max_val=100.0)
            l_amt = _clean_nonneg_number(entry.get("amount"), "line discount amount")
            if l_pct is not None and l_pct > 0 and l_amt is not None and l_amt > 0:
                frappe.throw(
                    coded_message("POS-DISC-CONFLICT", _("Specify either line percent or line amount for a row, not both.")),
                    frappe.ValidationError,
                )
            parsed_lines.append({"row": row_id, "percent": l_pct, "amount": l_amt})

    has_any_arg = (
        cint(clear_discount) == 1
        or is_explicit_comp
        or pct is not None
        or amt is not None
        or coupon is not None
        or line_discounts is not None
        or (discount_reason is not None and str(discount_reason).strip() != "")
        or (discount_note is not None and str(discount_note).strip() != "")
        or (approval is not None and str(approval).strip() != "")
        or (legacy_pct is not None and legacy_pct > 0)
    )

    return {
        "has_any_arg": has_any_arg,
        "clear": cint(clear_discount) == 1,
        "complimentary": is_explicit_comp,
        "percent": pct if (pct is not None and pct > 0) else None,
        "amount": amt if (amt is not None and amt > 0) else None,
        "coupon": coupon,
        "lines": parsed_lines if line_discounts is not None else None,
        "reason": str(discount_reason).strip() if discount_reason is not None and str(discount_reason).strip() else None,
        "note": str(discount_note).strip() if discount_note is not None and str(discount_note).strip() else None,
        "approval": str(approval).strip() if approval is not None and str(approval).strip() else None,
    }


def _snapshot_doc_discount_state(doc):
    return {
        "additional_discount_percentage": doc.get("additional_discount_percentage"),
        "discount_amount": doc.get("discount_amount"),
        "apply_discount_on": doc.get("apply_discount_on"),
        "coupon_code": doc.get("coupon_code"),
        "grand_total": doc.get("grand_total"),
        "rounded_total": doc.get("rounded_total"),
        "net_total": doc.get("net_total"),
        "total": doc.get("total"),
        "total_taxes_and_charges": doc.get("total_taxes_and_charges"),
        "items": [
            {
                "price_list_rate": row.get("price_list_rate"),
                "rate_with_margin": row.get("rate_with_margin"),
                "margin_type": row.get("margin_type"),
                "margin_rate_or_amount": row.get("margin_rate_or_amount"),
                "discount_percentage": row.get("discount_percentage"),
                "discount_amount": row.get("discount_amount"),
                "rate": row.get("rate"),
                "amount": row.get("amount"),
            }
            for row in (doc.get("items") or [])
        ],
    }


def _set_row_field(row, key, value):
    if callable(getattr(row, "set", None)):
        row.set(key, value)
    elif isinstance(row, dict):
        row[key] = value
    else:
        setattr(row, key, value)


def _restore_doc_discount_state(doc, snap):
    for k in (
        "additional_discount_percentage", "discount_amount", "apply_discount_on",
        "coupon_code", "grand_total", "rounded_total", "net_total", "total", "total_taxes_and_charges",
    ):
        doc.set(k, snap[k])
    for row, item_snap in zip(doc.get("items") or [], snap["items"]):
        for k, v in item_snap.items():
            _set_row_field(row, k, v)


def apply_discount_to_invoice(
    invoice,
    pos_profile_doc,
    spec,
    *,
    require_reason=True,
    enforce_auth=True,
    linked_partner=None,
):
    """Apply (or preview) a validated discount spec onto `invoice` using native ERPNext fields.

    When `enforce_auth=True`, verifies role limits, reason/note, 100% comp two-person rule,
    partial tender floor, and consumes any single-use manager override token (setting
    `invoice.flags.discount_approval = {approver, percent}` ONLY from a consumed token).
    """
    currency = invoice.get("currency") or pos_profile_doc.get("currency") or ""
    prec = currency_precision(invoice, currency)
    company = invoice.get("company") or pos_profile_doc.get("company")
    allow_open = _allow_open_discounts(pos_profile_doc)

    # Clear-all path
    if spec["clear"]:
        prev_summary = _compute_manual_discount_summary(invoice, prec)
        prev_has_disc = (
            prev_summary["discount_total"] > 0
            or flt(invoice.get("additional_discount_percentage")) > 0
            or flt(invoice.get("discount_amount")) > 0
            or bool(invoice.get("coupon_code"))
        )
        removal_flag = getattr(getattr(invoice, "flags", None), "discount_removal", None)
        if enforce_auth and prev_has_disc and not removal_flag:
            consumed = enforce_remove_discount_auth(
                invoice, pos_profile_doc, row=None, approval=spec.get("approval")
            )
            removal_actor = (consumed.get("approver") if isinstance(consumed, dict) else None) or frappe.session.user
            removal_flag = frappe._dict(approver=removal_actor, action="remove")
            invoice.flags.discount_removal = removal_flag

        prev_had_approver = bool(
            invoice.get("custom_discount_approver") or flt(invoice.get("custom_discount_approved_percent")) > 0
        )
        invoice.additional_discount_percentage = 0
        invoice.discount_amount = 0
        if hasattr(invoice, "coupon_code"):
            invoice.coupon_code = None
        for row in invoice.get("items") or []:
            if not _is_auto_pricing_rule_row(row):
                _set_row_field(row, "discount_percentage", 0)
                _set_row_field(row, "discount_amount", 0)
                if row.get("price_list_rate") is not None:
                    _set_row_field(row, "rate", row.get("price_list_rate"))
        with _with_currency_precision(invoice, prec):
            invoice.calculate_taxes_and_totals()
        if linked_partner is not None:
            linked_partner.additional_discount_percentage = 0
            linked_partner.discount_amount = 0
            with _with_currency_precision(linked_partner, prec):
                linked_partner.calculate_taxes_and_totals()
        if enforce_auth:
            if prev_had_approver or removal_flag:
                removal_actor = (
                    removal_flag.get("approver") if isinstance(removal_flag, dict) else None
                ) or frappe.session.user
                invoice.flags.discount_approval = frappe._dict(
                    approver=removal_actor, percent=0.0, action="remove"
                )
            else:
                invoice.flags.discount_approval = None
            _write_discount_audit_fields(invoice)
        summary = _compute_manual_discount_summary(invoice, prec)
        if linked_partner is not None:
            p_sum = _compute_manual_discount_summary(linked_partner, prec)
            summary["grand_total"] = flt(summary["grand_total"] + p_sum["grand_total"], prec)
            summary["rounded_total"] = flt(summary["rounded_total"] + p_sum["rounded_total"], prec)
            summary["after_total"] = summary["rounded_total"]
        summary.update(
            {
                "currency": currency,
                "precision": prec,
                "limit_percent": user_discount_limit(frappe.session.user, company, pos_profile_doc.name),
                "needs_approval": False,
                "is_comp": False,
                "privilege": "discount",
                "approval_reasons": [],
                "reason_required": False,
                "note_required": False,
                "reason": None,
                "reason_description": None,
                "coupon_code": None,
                "claim": canonical_discount_claim(precision=prec),
                "lines": [],
            }
        )
        return summary

    # Determine whether open discounts are requested
    is_explicit_comp = bool(spec.get("complimentary"))
    has_open_header = bool((spec["percent"] or spec["amount"]) and not is_explicit_comp)
    has_open_lines = bool(spec["lines"] and any(l.get("percent") or l.get("amount") for l in spec["lines"]))
    if (has_open_header or has_open_lines) and not allow_open:
        frappe.throw(
            coded_message("POS-DISC-DISABLED", _("Open discounts are not enabled for this till.")),
            frappe.PermissionError,
        )

    # Snapshot pre-mutation discount state for removal-auth detection, downward edit reconciliation, and rollback
    baseline_snap = _snapshot_doc_discount_state(invoice)
    prev_summary = _compute_manual_discount_summary(invoice, prec)
    prev_row_discs = _row_manual_discount_map(invoice, prec)
    prev_header_had_disc = (
        prev_summary["header_discount_amount"] > 0
        or flt(invoice.get("additional_discount_percentage")) > 0
        or bool(invoice.get("coupon_code"))
    )
    prev_meta_data = _read_meta(invoice)
    prev_approver = invoice.get("custom_discount_approver") or prev_meta_data.get("approver")
    prev_approved_pct = flt(invoice.get("custom_discount_approved_percent")) or flt(prev_meta_data.get("approved_percent"))
    prev_claim_snapshot = frappe._dict(
        name=invoice.name,
        company=company,
        currency=currency,
        invoice_printed=invoice.get("invoice_printed"),
        discount_amount=invoice.get("discount_amount"),
        additional_discount_percentage=invoice.get("additional_discount_percentage"),
    )

    # Resolve coupon preset (if any)
    coupon_info = None
    if spec["coupon"]:
        coupon_info = _resolve_coupon_preset(
            spec["coupon"],
            company=company,
            price_list=invoice.get("selling_price_list") or pos_profile_doc.get("selling_price_list"),
            posting_date=invoice.get("posting_date"),
        )

    # Auto-fill reason from Coupon Code.custom_discount_reason if caller omitted reason
    reason_code = spec["reason"] or (coupon_info.get("discount_reason") if coupon_info else None)
    has_positive_discount = bool(is_explicit_comp or has_open_header or has_open_lines or coupon_info)

    resolved_reason = _resolve_reason(reason_code, required=(require_reason and has_positive_discount))
    note = spec["note"]
    if resolved_reason and cint(resolved_reason.get("requires_note")) == 1 and require_reason and has_positive_discount:
        if not note:
            frappe.throw(
                coded_message(
                    "POS-DISC-NOTE-REQUIRED",
                    _("A note is required for discount reason {0}.").format(resolved_reason.get("description") or resolved_reason.get("code")),
                ),
                frappe.ValidationError,
            )

    # Validate & map line discounts against real invoice rows
    row_map = {str(r.get("name")): r for r in (invoice.get("items") or []) if r.get("name")}
    applied_line_spec = []
    if spec["lines"] is not None:
        for entry in spec["lines"]:
            row = row_map.get(entry["row"])
            if not row:
                frappe.throw(
                    coded_message("POS-DISC-ROW-NOT-FOUND", _("Line item {0} is not on this check.").format(entry["row"])),
                    frappe.ValidationError,
                )
            plr = flt(row.get("price_list_rate") if row.get("price_list_rate") is not None else row.get("rate"))
            if entry["amount"] is not None and entry["amount"] > plr + 1e-9:
                frappe.throw(
                    coded_message(
                        "POS-DISC-EXCEEDS-ITEM",
                        _("Line discount ({0}) cannot exceed the item price ({1}).").format(entry["amount"], plr),
                    ),
                    frappe.ValidationError,
                )
            _check_item_max_discount(row, discount_pct=entry["percent"], discount_amt=entry["amount"])
            applied_line_spec.append(entry)

    # Apply requested line discounts onto invoice rows.
    # In real ERPNext v16, calculate_item_rate() resets discount_percentage=0 if item.rate is
    # non-zero and does not equal calculated_rate; setting rate=0 lets ERPNext v16 compute
    # item.rate = calculated_rate at exact row precision so subsequent saves preserve it.
    if spec["lines"] is not None:
        spec_by_row = {e["row"]: e for e in applied_line_spec}
        for row in invoice.get("items") or []:
            rname = str(row.get("name") or "")
            if rname in spec_by_row:
                e = spec_by_row[rname]
                plr = flt(row.get("price_list_rate"))
                if plr <= 0 and flt(row.get("rate")) > 0:
                    plr = flt(row.get("rate"))
                    _set_row_field(row, "price_list_rate", plr)
                if e["percent"]:
                    _set_row_field(row, "discount_percentage", flt(e["percent"]))
                    _set_row_field(row, "discount_amount", flt(plr * flt(e["percent"]) / 100.0, prec))
                    _set_row_field(row, "rate", 0)
                elif e["amount"]:
                    _set_row_field(row, "discount_percentage", 0)
                    _set_row_field(row, "discount_amount", flt(e["amount"], prec))
                    _set_row_field(row, "rate", 0)
                else:
                    _set_row_field(row, "discount_percentage", 0)
                    _set_row_field(row, "discount_amount", 0)
                    if plr > 0:
                        _set_row_field(row, "rate", plr)

    # First measure total after line discounts and BEFORE header discount
    invoice.additional_discount_percentage = 0
    invoice.discount_amount = 0
    invoice.coupon_code = None
    with _with_currency_precision(invoice, prec):
        invoice.calculate_taxes_and_totals()
    basis_before_header = (
        flt(invoice.get("net_total"), prec)
        if (invoice.get("apply_discount_on") or "Grand Total") == "Net Total"
        else flt(invoice.get("grand_total"), prec)
    )

    # Apply header discount (coupon preset OR open % OR open flat amount)
    if coupon_info:
        invoice.coupon_code = coupon_info.name
        if coupon_info.apply_discount_on:
            invoice.apply_discount_on = coupon_info.apply_discount_on
        if coupon_info.discount_percentage > 0:
            invoice.additional_discount_percentage = coupon_info.discount_percentage
            invoice.discount_amount = 0
        elif coupon_info.discount_amount > 0:
            if invoice.get("items") and coupon_info.discount_amount > basis_before_header + 1e-9:
                frappe.throw(
                    coded_message("POS-DISC-EXCEEDS-TOTAL", _("Discount amount exceeds the check total.")),
                    frappe.ValidationError,
                )
            invoice.additional_discount_percentage = 0
            invoice.discount_amount = flt(coupon_info.discount_amount, prec)
    elif spec["percent"]:
        invoice.additional_discount_percentage = flt(spec["percent"])
        invoice.discount_amount = 0
    elif spec["amount"]:
        if invoice.get("items") and flt(spec["amount"], prec) > basis_before_header + 1e-9:
            _restore_doc_discount_state(invoice, baseline_snap)
            frappe.throw(
                coded_message("POS-DISC-EXCEEDS-TOTAL", _("Discount amount cannot exceed the check total.")),
                frappe.ValidationError,
            )
        invoice.additional_discount_percentage = 0
        invoice.discount_amount = flt(spec["amount"], prec)
    elif not spec["has_any_arg"]:
        # No discount arguments passed at all -> preserve existing check discount
        _restore_doc_discount_state(invoice, baseline_snap)
    else:
        # e.g. line-only discount with no header discount, or explicit zero header discount
        invoice.additional_discount_percentage = 0
        invoice.discount_amount = 0

    with _with_currency_precision(invoice, prec):
        invoice.calculate_taxes_and_totals()

    if linked_partner is not None:
        if invoice.additional_discount_percentage:
            linked_partner.additional_discount_percentage = invoice.additional_discount_percentage
            linked_partner.discount_amount = 0
        elif not spec["has_any_arg"]:
            pass
        else:
            linked_partner.additional_discount_percentage = 0
        with _with_currency_precision(linked_partner, prec):
            linked_partner.calculate_taxes_and_totals()

    summary = _compute_manual_discount_summary(invoice, prec)

    # P0-4: Gate any implicit removal via apply_check_discount(discount_percent=0) or line percent=0
    if enforce_auth:
        curr_row_discs = _row_manual_discount_map(invoice, prec)
        cleared_all = prev_summary["discount_total"] > 0 and summary["discount_total"] <= 0
        cleared_header = (
            prev_header_had_disc
            and summary["header_discount_amount"] <= 0
            and flt(invoice.get("additional_discount_percentage")) <= 0
            and not invoice.get("coupon_code")
        )
        cleared_rows = [
            r for r, old_val in prev_row_discs.items() if old_val > 0 and r in curr_row_discs and curr_row_discs[r] <= 0
        ]
        if cleared_all or cleared_header or cleared_rows:
            target_row = (
                cleared_rows[0] if (len(cleared_rows) == 1 and not cleared_all and not cleared_header) else None
            )
            try:
                consumed_rem = enforce_remove_discount_auth(
                    prev_claim_snapshot, pos_profile_doc, row=target_row, approval=spec.get("approval")
                )
            except Exception:
                _restore_doc_discount_state(invoice, baseline_snap)
                raise
            rem_actor = (
                consumed_rem.get("approver") if isinstance(consumed_rem, dict) else None
            ) or frappe.session.user
            invoice.flags.discount_removal = frappe._dict(approver=rem_actor, action="remove")
            if cleared_all:
                if prev_approver or prev_approved_pct > 0:
                    invoice.flags.discount_approval = frappe._dict(
                        approver=rem_actor, percent=0.0, action="remove"
                    )
                _write_discount_audit_fields(invoice)
    if linked_partner is not None:
        p_sum = _compute_manual_discount_summary(linked_partner, prec)
        summary["grand_total"] = flt(summary["grand_total"] + p_sum["grand_total"], prec)
        summary["rounded_total"] = flt(summary["rounded_total"] + p_sum["rounded_total"], prec)
        summary["before_total"] = flt(summary["before_total"] + p_sum["before_total"], prec)
        summary["discount_total"] = flt(summary["discount_total"] + p_sum["discount_total"], prec)
        summary["tax_total"] = flt(summary["tax_total"] + p_sum["tax_total"], prec)
        summary["after_total"] = summary["rounded_total"]

    # Partial tender floor check (Simphony / SAP: cannot discount below tenders already received)
    received_total = _received_tender_total(invoice)
    if linked_partner is not None:
        received_total += _received_tender_total(linked_partner)
    if received_total > 0 and flt(summary["after_total"], prec) < flt(received_total, prec):
        _restore_doc_discount_state(invoice, baseline_snap)
        frappe.throw(
            coded_message(
                "POS-DISC-BELOW-TENDER",
                _("Cannot apply discount: discounted total ({0}) is less than tenders already received ({1}).").format(
                    summary["after_total"], flt(received_total, prec)
                ),
            ),
            frappe.ValidationError,
        )

    # Authority check (min(Approval Limit, POS Privilege Rule); blank base = 0%; 100% comp ALWAYS needs 2nd person)
    approval_amount = (
        company_approval_amount(invoice, summary["discount_total"], frappe) if has_positive_discount else None
    )
    limit_pct = user_discount_limit(
        frappe.session.user, company, pos_profile_doc.name, approval_amount=approval_amount
    )
    effective_pct = summary["effective_percent"]
    is_comp = bool(
        has_positive_discount
        and (
            is_explicit_comp
            or effective_pct >= 100.0
            or (summary["discount_basis"] > 0 and flt(summary["after_total"], prec) <= 0)
        )
    )
    privilege = "complimentary" if is_comp else "discount"
    over_limit = bool(
        has_positive_discount
        and _above_limit(
            summary["discount_total"],
            summary["discount_basis"],
            spec["percent"],
            limit_pct,
            prec,
        )
    )
    above_prior_approval = bool(
        has_positive_discount
        and prev_approver
        and prev_approved_pct > 0
        and effective_pct > prev_approved_pct + 1e-4
    )
    reason_requires_sup = bool(resolved_reason and cint(resolved_reason.get("requires_supervisor")) == 1)

    approval_reasons = []
    if is_comp:
        approval_reasons.append("comp")
    if over_limit:
        approval_reasons.append("over_limit")
    if above_prior_approval:
        approval_reasons.append("above_prior_approval")
    if reason_requires_sup:
        approval_reasons.append("reason_requires_supervisor")
    needs_approval = bool(approval_reasons)

    # Build canonical claim matching pos-override-contract.md
    effective_lines_for_claim = []
    for row in invoice.get("items") or []:
        if _is_auto_pricing_rule_row(row):
            continue
        r_pct = flt(row.get("discount_percentage"))
        r_amt = flt(row.get("discount_amount"))
        if r_pct > 0 or r_amt > 0:
            # Prefer whichever mode was explicitly requested for this row
            req_entry = next((e for e in (spec["lines"] or []) if e["row"] == str(row.get("name"))), None)
            if req_entry and req_entry.get("amount"):
                effective_lines_for_claim.append({"row": str(row.get("name")), "percent": None, "amount": req_entry["amount"]})
            elif req_entry and req_entry.get("percent"):
                effective_lines_for_claim.append({"row": str(row.get("name")), "percent": req_entry["percent"], "amount": None})
            else:
                effective_lines_for_claim.append({"row": str(row.get("name")), "percent": r_pct or None, "amount": None if r_pct else r_amt})

    claim = canonical_discount_claim(
        percent=spec["percent"],
        amount=spec["amount"],
        coupon=coupon_info.name if coupon_info else None,
        lines=effective_lines_for_claim,
        reason=resolved_reason.get("code") if resolved_reason else reason_code,
        note=note,
        precision=prec,
    )

    if enforce_auth and has_positive_discount:
        approver = None
        approved_pct = 0.0
        if needs_approval:
            override_mod = _get_override_module()
            if not spec["approval"]:
                _restore_doc_discount_state(invoice, baseline_snap)
                attach_auth_challenge(
                    privilege=privilege, pos_profile=pos_profile_doc.name,
                    reference_doctype="POS Invoice", reference_name=invoice.name,
                    claim=claim, amount=approval_amount, percent=effective_pct, framework=frappe,
                )
                frappe.throw(
                    coded_message("POS-AUTH-REQUIRED", _("Manager approval is required for this discount.")),
                    frappe.PermissionError,
                )
            consumed = override_mod.consume(
                spec["approval"],
                privilege,
                pos_profile_doc.name,
                invoice.name,
                claim,
                approval_amount=approval_amount,
            )
            approver = consumed.get("approver")
            if not approver or approver == frappe.session.user:
                _restore_doc_discount_state(invoice, baseline_snap)
                frappe.throw(
                    coded_message("POS-AUTH-SELF", _("You cannot approve your own discount.")),
                    frappe.PermissionError,
                )
            requested_pct = max(
                flt(invoice.get("additional_discount_percentage")),
                flt(spec.get("percent")),
                flt(coupon_info.get("discount_percentage") if coupon_info else 0.0),
            )
            approved_pct = max(effective_pct, requested_pct)
            approver_ceiling = _approver_discount_ceiling(
                approver, company, pos_profile=pos_profile_doc.name, privilege=privilege
            )
            if approver_ceiling is not None and approved_pct > approver_ceiling:
                approved_pct = approver_ceiling
            # ONLY a consumed single-use token sets doc.flags.discount_approval!
            invoice.flags.discount_approval = frappe._dict(approver=approver, percent=approved_pct)
            if linked_partner is not None:
                linked_partner.flags.discount_approval = frappe._dict(approver=approver, percent=approved_pct)
        else:
            invoice.flags.discount_approval = None
            # P0-3: When lowering an already-approved discount downward within cashier limit
            # (effective_pct <= prev_approved_pct), preserve the stored approval stamp so neither
            # validate_pos_discount (POS-DISC-FORGED) nor siteops#8620 (SITEOPS-DISC-007) rejects it.
            if prev_approver and effective_pct <= prev_approved_pct + 1e-4:
                approver = prev_approver
                approved_pct = prev_approved_pct

        line_reasons = {
            entry["row"]: (resolved_reason.get("description") or resolved_reason.get("code"))
            for entry in effective_lines_for_claim
            if resolved_reason
        }
        _write_discount_audit_fields(
            invoice,
            reason=resolved_reason.get("code") if resolved_reason else None,
            reason_description=resolved_reason.get("description") if resolved_reason else None,
            note=note,
            coupon=coupon_info.name if coupon_info else None,
            approver=approver,
            approved_percent=approved_pct if approver else 0.0,
            line_reasons=line_reasons,
            c_hash=claim_hash(claim),
        )

    preview_lines = []
    for row in invoice.get("items") or []:
        if cint(row.get("is_disposable")) or flt(row.get("qty")) <= 0:
            continue
        plr = flt(row.get("price_list_rate") if row.get("price_list_rate") is not None else row.get("rate"))
        qty = flt(row.get("qty"))
        gross = flt(plr * qty, prec)
        amt_row = flt(row.get("amount"), prec)
        preview_lines.append(
            {
                "row": row.get("name"),
                "item_code": row.get("item_code"),
                "item_name": row.get("item_name"),
                "qty": qty,
                "price_list_rate": flt(plr, prec),
                "rate": flt(row.get("rate"), prec),
                "amount": amt_row,
                "discount_percentage": flt(row.get("discount_percentage")),
                "discount_amount": flt(row.get("discount_amount"), prec),
                "line_discount_total": max(0.0, flt(gross - amt_row, prec)),
                "max_discount": _item_max_discount(row.get("item_code")),
            }
        )

    summary.update(
        {
            "currency": currency,
            "precision": prec,
            "limit_percent": limit_pct,
            "approval_amount": approval_amount,
            "needs_approval": needs_approval,
            "is_comp": is_comp,
            "privilege": privilege,
            "approval_reasons": approval_reasons,
            "reason_required": bool(has_positive_discount and not resolved_reason),
            "note_required": bool(resolved_reason and cint(resolved_reason.get("requires_note")) == 1),
            "reason": resolved_reason.get("code") if resolved_reason else None,
            "reason_description": resolved_reason.get("description") if resolved_reason else None,
            "coupon_code": coupon_info.name if coupon_info else None,
            "claim": claim,
            "lines": preview_lines,
        }
    )
    return summary


def list_discount_options(pos_profile, invoice=None):
    """Return active POS Discount Reasons (in sort_order), valid Coupon Code presets,
    POS Profile allow_discount_change flag, and the caller's own discount limit %."""
    profile = frappe.get_cached_doc("POS Profile", pos_profile)
    company = profile.get("company")
    currency = profile.get("currency") or ""
    prec = currency_precision(currency=currency)
    price_list = profile.get("selling_price_list")
    posting_date = _today_str()
    inv_printed = 0
    approval_amount = None
    approval_basis = None
    if invoice:
        inv_date = frappe.db.get_value("POS Invoice", invoice, "posting_date")
        if inv_date:
            posting_date = str(inv_date)
        inv_printed = cint(frappe.db.get_value("POS Invoice", invoice, "invoice_printed") or 0)
        doc = frappe.get_doc("POS Invoice", invoice)
        doc.check_permission("read")
        if doc.get("pos_profile") == pos_profile:
            summary = _compute_manual_discount_summary(doc, currency_precision(doc))
            if summary["discount_basis"] > 0:
                approval_basis = company_approval_amount(doc, summary["discount_basis"], frappe)
            if summary["discount_total"] > 0:
                approval_amount = company_approval_amount(doc, summary["discount_total"], frappe)

    # 1) Reasons in sort_order
    if frappe.db.exists("DocType", "Discount Reason"):
        raw_reasons = frappe.get_all(
            "Discount Reason",
            filters={"disabled": 0, "applies_to": ["in", ["POS", "Both"]]},
            fields=["name", "code", "description", "requires_supervisor", "requires_note", "sort_order"],
            order_by="sort_order asc, code asc",
        ) or []
        reasons = [
            {
                "name": r.get("name"),
                "code": r.get("code") or r.get("name"),
                "description": r.get("description") or r.get("code") or r.get("name"),
                "requires_supervisor": cint(r.get("requires_supervisor")),
                "requires_note": cint(r.get("requires_note")),
                "sort_order": cint(r.get("sort_order")),
            }
            for r in raw_reasons
        ]
    else:
        reasons = [dict(r) for r in FALLBACK_POS_REASONS]

    # 2) Valid coupon presets (Pricing Rule coupon_code_based=1 + Coupon Code Promotional)
    presets = []
    if not cint(profile.get("ignore_pricing_rule")):
        coupon_meta = frappe.get_meta("Coupon Code")
        has_reason_field = bool(coupon_meta and coupon_meta.has_field("custom_discount_reason"))
        c_fields = [
            "name", "coupon_name", "coupon_code", "coupon_type", "pricing_rule",
            "valid_from", "valid_upto", "maximum_use", "used",
        ]
        if has_reason_field:
            c_fields.append("custom_discount_reason")
        coupons = frappe.get_all("Coupon Code", filters={"coupon_type": "Promotional"}, fields=c_fields) or []
        rules = frappe.get_all(
            "Pricing Rule",
            filters={"disable": 0, "selling": 1, "coupon_code_based": 1, "apply_on": "Transaction"},
            fields=[
                "name", "title", "price_or_product_discount", "rate_or_discount",
                "discount_percentage", "discount_amount", "apply_discount_on",
                "company", "for_price_list", "valid_from", "valid_upto",
            ],
        ) or []
        rules_by_name = {r.get("name"): r for r in rules if r.get("name")}
        for c in coupons:
            if not _date_in_range(posting_date, c.get("valid_from"), c.get("valid_upto")):
                continue
            max_use = cint(c.get("maximum_use"))
            if max_use > 0 and cint(c.get("used")) >= max_use:
                continue
            rule = rules_by_name.get(c.get("pricing_rule"))
            if not rule or rule.get("price_or_product_discount") != "Price":
                continue
            if rule.get("company") and company and rule.get("company") != company:
                continue
            if rule.get("for_price_list") and price_list and rule.get("for_price_list") != price_list:
                continue
            if not _date_in_range(posting_date, rule.get("valid_from"), rule.get("valid_upto")):
                continue
            presets.append(
                {
                    "name": c.get("name"),
                    "coupon_code": c.get("coupon_code") or c.get("name"),
                    "title": rule.get("title") or c.get("coupon_name") or c.get("name"),
                    "discount_percentage": flt(rule.get("discount_percentage")),
                    "discount_amount": flt(rule.get("discount_amount"), prec),
                    "apply_discount_on": rule.get("apply_discount_on") or "Grand Total",
                    "discount_reason": c.get("custom_discount_reason") if has_reason_field else None,
                }
            )
        presets.sort(key=lambda p: (p["title"] or p["name"]))

    return {
        "reasons": reasons,
        "presets": presets,
        "allow_discount_change": _allow_open_discounts(profile),
        "complimentary_enabled": True,
        "limit_percent": user_discount_limit(
            frappe.session.user, company, pos_profile, approval_amount=approval_amount, approval_basis=approval_basis
        ),
        "can_remove_discount": user_can_remove_discount(
            frappe.session.user, company, pos_profile=pos_profile, invoice_printed=inv_printed
        ),
        "currency": currency,
        "precision": prec,
    }


def get_check_discounts(invoice):
    """Read-only discount summary (reason, description, note, coupon, line discounts) for a check."""
    doc = frappe.get_doc("POS Invoice", invoice)
    doc.check_permission("read")
    prec = currency_precision(doc)
    meta_data = _read_meta(doc)

    reason_code = doc.get("custom_discount_reason") or meta_data.get("reason")
    reason_desc = meta_data.get("reason_description") or reason_code
    if reason_code and frappe.db.exists("DocType", "Discount Reason"):
        desc = frappe.db.get_value("Discount Reason", reason_code, "description")
        if desc:
            reason_desc = desc
    note = doc.get("custom_discount_note") or meta_data.get("note")
    coupon_code = doc.get("coupon_code") or meta_data.get("coupon_code")
    line_reasons = meta_data.get("line_reasons") or {}

    line_discounts = []
    for row in doc.get("items") or []:
        if cint(row.get("is_disposable")) or flt(row.get("qty")) <= 0:
            continue
        plr = flt(row.get("price_list_rate") if row.get("price_list_rate") is not None else row.get("rate"))
        qty = flt(row.get("qty"))
        gross = flt(plr * qty, prec)
        amt = flt(row.get("amount"), prec)
        disc_total = max(0.0, flt(gross - amt, prec))
        if disc_total > 0 or flt(row.get("discount_percentage")) > 0 or flt(row.get("discount_amount")) > 0:
            line_discounts.append(
                {
                    "row": row.get("name"),
                    "item_code": row.get("item_code"),
                    "item_name": row.get("item_name"),
                    "discount_percentage": flt(row.get("discount_percentage")),
                    "discount_amount": flt(row.get("discount_amount"), prec),
                    "line_discount_total": disc_total,
                    "reason": line_reasons.get(row.get("name")) or reason_desc,
                    "automatic": _is_auto_pricing_rule_row(row),
                }
            )

    return {
        "invoice": doc.name,
        "additional_discount_percentage": flt(doc.get("additional_discount_percentage")),
        "discount_amount": flt(doc.get("discount_amount"), prec),
        "coupon_code": coupon_code,
        "reason": reason_code,
        "reason_description": reason_desc,
        "note": note,
        "currency": doc.get("currency"),
        "precision": prec,
        "can_remove_discount": user_can_remove_discount(
            frappe.session.user,
            doc.get("company"),
            pos_profile=doc.get("pos_profile"),
            invoice_printed=doc.get("invoice_printed"),
        ),
        "remove_claim": canonical_remove_claim(doc),
        "line_discounts": line_discounts,
    }


def validate_manual_line_discount_cap(doc, previous):
    """Check company-currency authority when a pricing save adds or increases manual line discounts."""
    if not any(
        (flt(row.get("discount_percentage")) or flt(row.get("discount_amount")))
        and not _is_auto_pricing_rule_row(row)
        for row in doc.get("items") or []
    ):
        return
    precision = currency_precision(doc)
    if previous is not None:
        before = _row_manual_discount_map(previous, precision)
        after = _row_manual_discount_map(doc, precision)
        if not any(amount > before.get(row, 0) for row, amount in after.items()):
            return

    summary = _compute_manual_discount_summary(doc, precision)
    user = frappe.session.user
    company = doc.get("company")
    profile = doc.get("pos_profile")
    amount = company_approval_amount(doc, summary["discount_total"], frappe)
    limit = user_discount_limit(user, company, profile, approval_amount=amount)
    if _above_limit(summary["discount_total"], summary["discount_basis"], None, limit, precision):
        frappe.throw(
            coded_message("POS-AUTH-REQUIRED", _("Manager approval is required for this discount.")),
            frappe.PermissionError,
        )


def validate_pos_discount(doc, method=None):
    """POS Invoice.validate hook: enforces till discount rules even when siteops
    sales-discount-control@3 is NOT installed, and blocks REST straight-to-save bypasses."""
    if getattr(frappe.flags, "in_patch", False) or getattr(frappe.flags, "in_migrate", False):
        return
    if cint(doc.get("is_return")) == 1 or cint(doc.get("is_consolidated")) == 1:
        return
    if (
        getattr(frappe.flags, "ury_bill_split", False)
        or getattr(frappe.flags, "in_bill_merge_sync", False)
        or getattr(frappe.flags, "ury_reopen_adjust_copy", False)
    ):
        return

    # 1) Hard cap Item.max_discount on every row
    for row in doc.get("items") or []:
        if not _is_auto_pricing_rule_row(row):
            _check_item_max_discount(row)

    previous = doc.get_doc_before_save() if not doc.is_new() else None
    server_write = bool(getattr(frappe.flags, "ury_server_discount_write", False))
    approval_flag = getattr(getattr(doc, "flags", None), "discount_approval", None)

    # 2) Server-only guard on ury_discount_meta
    if not server_write:
        prev_meta = previous.get("ury_discount_meta") if previous is not None else None
        if doc.get("ury_discount_meta") != prev_meta:
            frappe.throw(
                coded_message("POS-DISC-FORGED", _("Discount metadata can only be updated by the server.")),
                frappe.PermissionError,
            )

    # 3) Server-only guard on custom_discount_approver / custom_discount_approved_percent
    prev_approver = previous.get("custom_discount_approver") if previous is not None else None
    prev_approved_pct = flt(previous.get("custom_discount_approved_percent")) if previous is not None else 0.0
    curr_approver = doc.get("custom_discount_approver")
    curr_approved_pct = flt(doc.get("custom_discount_approved_percent"))

    if not approval_flag:
        if curr_approver != prev_approver or curr_approved_pct != prev_approved_pct:
            # Allow clearing approval when the discount itself was cleared to 0
            prec = currency_precision(doc)
            curr_sum = _compute_manual_discount_summary(doc, prec)
            if curr_sum["discount_total"] > 0 or curr_approver or curr_approved_pct > 0:
                frappe.throw(
                    coded_message("POS-DISC-FORGED", _("Discount approval can only be set by a verified manager PIN.")),
                    frappe.PermissionError,
                )

    prec = currency_precision(doc)
    curr_summary = _compute_manual_discount_summary(doc, prec)
    prev_summary = _compute_manual_discount_summary(previous, prec) if previous is not None else {
        "discount_total": 0.0,
        "effective_percent": 0.0,
        "header_discount_amount": 0.0,
        "line_discount_total": 0.0,
    }

    # Check partial tender floor whenever any discount is present
    received_total = _received_tender_total(doc)
    if curr_summary["discount_total"] > 0 and received_total > 0:
        if flt(curr_summary["after_total"], prec) < flt(received_total, prec):
            frappe.throw(
                coded_message(
                    "POS-DISC-BELOW-TENDER",
                    _("Discounted total ({0}) cannot be less than tenders already received ({1}).").format(
                        curr_summary["after_total"], flt(received_total, prec)
                    ),
                ),
                frappe.ValidationError,
            )

    # P0-4: Block direct REST / Desk discount removal bypass (header OR line discount cleared without discount_remove auth)
    removal_flag = getattr(getattr(doc, "flags", None), "discount_removal", None)
    if previous is not None and not removal_flag:
        prev_header_had = (
            prev_summary["header_discount_amount"] > 0
            or flt(previous.get("additional_discount_percentage")) > 0
            or bool(previous.get("coupon_code"))
        )
        curr_header_has = (
            curr_summary["header_discount_amount"] > 0
            or flt(doc.get("additional_discount_percentage")) > 0
            or bool(doc.get("coupon_code"))
        )
        prev_rows = _row_manual_discount_map(previous, prec)
        curr_rows = _row_manual_discount_map(doc, prec)
        any_row_cleared = any(
            old_v > 0 and r in curr_rows and curr_rows[r] <= 0 for r, old_v in prev_rows.items()
        )
        has_active_items = any(
            not cint(r.get("is_disposable")) and flt(r.get("qty")) > 0 for r in (doc.get("items") or [])
        )
        discount_removed = (
            (prev_summary["discount_total"] > 0 and curr_summary["discount_total"] <= 0 and has_active_items)
            or (prev_header_had and not curr_header_has and has_active_items)
            or any_row_cleared
        )
        if discount_removed:
            was_printed = cint(previous.get("invoice_printed")) == 1 or cint(doc.get("invoice_printed")) == 1
            if not user_can_remove_discount(
                frappe.session.user,
                previous.get("company") or doc.get("company"),
                pos_profile=previous.get("pos_profile") or doc.get("pos_profile"),
                invoice_printed=was_printed,
            ):
                frappe.throw(
                    coded_message("POS-AUTH-REQUIRED", _("Manager approval is required to remove this discount.")),
                    frappe.PermissionError,
                )

    if curr_summary["discount_total"] <= 0:
        return

    # 4) Determine whether the discount changed or increased relative to saved doc
    prev_meta_dict = _read_meta(previous) if previous is not None else {}
    stored_approver = prev_approver or prev_meta_dict.get("approver")
    stored_approved_pct = prev_approved_pct or flt(prev_meta_dict.get("approved_percent"))
    prev_add_pct = flt(previous.get("additional_discount_percentage")) if previous is not None else 0.0
    curr_add_pct = flt(doc.get("additional_discount_percentage"))
    prev_row_discs = _row_manual_discount_map(previous, prec) if previous is not None else {}
    curr_row_discs = _row_manual_discount_map(doc, prec)
    row_tol = 10 ** (-(prec + 1))
    any_row_disc_increased = any(
        curr_v > prev_row_discs.get(r, 0.0) + row_tol for r, curr_v in curr_row_discs.items()
    )
    pct_scaled_with_basket = (
        previous is not None
        and prev_add_pct > 0
        and curr_add_pct <= prev_add_pct + 1e-6
        and curr_summary["discount_basis"] > prev_summary["discount_basis"] + row_tol
        and not any_row_disc_increased
        and not _exceeds_approved_percent(
            curr_summary, doc, max(prev_add_pct, prev_summary["effective_percent"]), prec
        )
    )
    discount_increased = (
        (
            curr_summary["discount_total"] > prev_summary["discount_total"] + row_tol
            and not pct_scaled_with_basket
        )
        or (
            curr_summary["effective_percent"] > prev_summary["effective_percent"] + 1e-4
            and not pct_scaled_with_basket
        )
        or any_row_disc_increased
    )
    if stored_approver and pct_scaled_with_basket:
        _has_rules, approver_rules = _privilege_rules_for(doc.get("company"), "discount", pos_profile=doc.get("pos_profile"))
        caps = [
            flt(rule.get("max_amount"))
            for role in frappe.get_roles(stored_approver) or []
            if (rule := approver_rules.get(role)) and cint(rule.get("may_authorise")) == 1
        ]
        if caps and all(cap > 0 for cap in caps):
            current_amount = company_approval_amount(doc, curr_summary["discount_total"], frappe)
            previous_amount = company_approval_amount(previous, prev_summary["discount_total"], frappe)
            discount_increased |= current_amount > max(caps) and current_amount > previous_amount
    is_new_discount = previous is None or prev_summary["discount_total"] <= 0

    if approval_flag and approval_flag.get("action") != "remove":
        approver = approval_flag.get("approver")
        if not approver or approver == frappe.session.user or (doc.get("owner") and approver == doc.get("owner")):
            frappe.throw(
                coded_message("POS-AUTH-SELF", _("The approver cannot be the cashier or check owner.")),
                frappe.PermissionError,
            )
        if not cint(frappe.db.get_value("User", approver, "enabled")):
            frappe.throw(
                coded_message("POS-AUTH-NOT-ELIGIBLE", _("Approver account is disabled.")),
                frappe.PermissionError,
            )
        meta = frappe.get_meta("POS Invoice")
        if meta and meta.has_field("custom_discount_approver"):
            doc.set("custom_discount_approver", approver)
        if meta and meta.has_field("custom_discount_approved_percent"):
            doc.set("custom_discount_approved_percent", flt(approval_flag.get("percent") or curr_summary["effective_percent"]))
        return

    # Unchanged or lowered already-approved discount (e.g. printing, settling, or lowering 10% -> 5%)
    if not is_new_discount and not discount_increased and stored_approver:
        if _exceeds_approved_percent(curr_summary, doc, stored_approved_pct, prec):
            frappe.throw(
                coded_message("SITEOPS-DISC-007", _("Cannot raise an approved discount without fresh manager approval.")),
                frappe.PermissionError,
            )
        return

    # Any new or raised discount without approval_flag must pass full authority & reason rules
    if not is_new_discount and discount_increased and stored_approver:
        frappe.throw(
            coded_message("SITEOPS-DISC-007", _("Cannot raise an approved discount without fresh manager approval.")),
            frappe.PermissionError,
        )

    company = doc.get("company")
    approval_amount = company_approval_amount(doc, curr_summary["discount_total"], frappe)
    limit_pct = user_discount_limit(
        frappe.session.user, company, doc.get("pos_profile"), approval_amount=approval_amount
    )
    is_comp = curr_summary["effective_percent"] >= 100.0 or (
        curr_summary["discount_basis"] > 0 and flt(curr_summary["after_total"], prec) <= 0
    )
    over_limit = _above_limit(
        curr_summary["discount_total"],
        curr_summary["discount_basis"],
        doc.get("additional_discount_percentage"),
        limit_pct,
        prec,
    )

    reason_code = doc.get("custom_discount_reason") or _read_meta(doc).get("reason")
    resolved_reason = _resolve_reason(reason_code, required=True)
    if resolved_reason and cint(resolved_reason.get("requires_note")) == 1:
        note = (doc.get("custom_discount_note") or _read_meta(doc).get("note") or "").strip()
        if not note:
            frappe.throw(
                coded_message("POS-DISC-NOTE-REQUIRED", _("A note is required for this discount reason.")),
                frappe.ValidationError,
            )

    if is_comp or over_limit or (resolved_reason and cint(resolved_reason.get("requires_supervisor")) == 1):
        frappe.throw(
            coded_message("POS-AUTH-REQUIRED", _("Manager approval is required for this discount.")),
            frappe.PermissionError,
        )


def prorate_return_discount(doc, method=None):
    """SAP copy-control proration for partial returns of flat-discounted POS Invoices.

    ERPNext v16's native make_return_doc copies `-1 * source.discount_amount` verbatim even
    when only a subset of items/quantities is returned. When the original check had a flat
    header discount (additional_discount_percentage == 0, discount_amount > 0), prorate
    proportionally to returned gross value and cap to remaining unreturned discount across
    all prior submitted returns so multi-step returns sum to 100% of the original discount.
    """
    if cint(doc.get("is_return")) != 1 or not doc.get("return_against"):
        return
    if getattr(getattr(doc, "flags", None), "ury_return_discount_computed", False):
        return
    if flt(doc.get("additional_discount_percentage")):
        return

    source = frappe.get_doc("POS Invoice", doc.return_against)
    if not source or flt(source.get("additional_discount_percentage")):
        return
    source_discount = flt(source.get("discount_amount"))
    if source_discount <= 0:
        return

    prec = currency_precision(doc)
    source_gross = sum(
        flt(row.get("amount") if row.get("amount") is not None else flt(row.get("qty")) * flt(row.get("rate")))
        for row in (source.get("items") or [])
        if not cint(row.get("is_disposable")) and flt(row.get("qty")) > 0
    )
    returned_gross = sum(
        abs(flt(row.get("qty")) * flt(row.get("rate")))
        for row in (doc.get("items") or [])
        if not cint(row.get("is_disposable")) and flt(row.get("qty")) != 0
    )
    if source_gross <= 0 or returned_gross <= 0:
        return

    prior_returns = frappe.get_all(
        "POS Invoice",
        filters={
            "return_against": source.name,
            "is_return": 1,
            "docstatus": 1,
            "name": ["!=", doc.name or ""],
        },
        fields=["name", "discount_amount", "total"],
    ) or []
    prior_discount = sum(abs(flt(r.get("discount_amount"))) for r in prior_returns)
    prior_gross = sum(abs(flt(r.get("total"))) for r in prior_returns)
    remaining_discount = max(0.0, flt(source_discount - prior_discount, prec))

    if returned_gross + prior_gross >= source_gross - 1e-6:
        prorated = flt(remaining_discount, prec)
    else:
        prorated = min(
            flt(remaining_discount, prec),
            flt(source_discount * (returned_gross / source_gross), prec),
        )

    doc.discount_amount = -abs(prorated) if prorated > 0 else 0.0
