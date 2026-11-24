import frappe
from frappe import _, msgprint
try:
    from frappe.utils import cint, flt
except ImportError:
    def cint(value):
        return int(float(value or 0))

    def flt(value, precision=None):
        num = float(value or 0)
        return round(num, precision) if precision is not None else num

from ury.brand import coded_message
from ury.ury_pos.default_customer import validate_order_type_customers


def validate(doc, method):
    validate_bill_check(doc, method)
    validate_cost_center(doc, method)
    validate_order_type_customers(doc, method)
    validate_room_lookup_method(doc, method)
    validate_paid_out_approval_threshold(doc, method)
    validate_till_cash_thresholds(doc, method)
    validate_print_format(doc, method)
    validate_service_charge_rules(doc, method)


def _validate_service_charge_account(doc, account: str | None, tips_account: str) -> None:
    acct = (account or "").strip()
    if not acct:
        frappe.throw(
            coded_message(
                "SERVICE-CHARGE-ACCOUNT-REQUIRED",
                _("Select a Liability or Income Account for every enabled Service Charge rule."),
            ),
            frappe.ValidationError,
        )
    if tips_account and acct == tips_account:
        frappe.throw(
            coded_message(
                "SERVICE-CHARGE-ACCOUNT-CONFLICT",
                _("Service Charge Account must be separate from Tips Payable Account."),
            ),
            frappe.ValidationError,
        )
    company = (doc.get("company") or "").strip()
    if not company:
        return
    if hasattr(frappe, "db") and callable(getattr(frappe.db, "table_exists", None)) and not frappe.db.table_exists("Account"):
        return
    ledger = frappe.db.get_value(
        "Account", acct, ["company", "root_type", "is_group", "disabled"], as_dict=True
    )
    if (
        not isinstance(ledger, dict)
        or ledger.get("company") != company
        or ledger.get("root_type") not in ("Liability", "Income")
        or cint(ledger.get("is_group")) == 1
        or cint(ledger.get("disabled")) == 1
    ):
        frappe.throw(
            coded_message(
                "SERVICE-CHARGE-ACCOUNT-INVALID",
                _("Service Charge Account {0} must be an enabled non-group Liability or Income account of {1}.").format(
                    acct, company
                ),
            ),
            frappe.ValidationError,
        )


def validate_service_charge_rules(doc, method=None):
    """Validate POS Profile.custom_service_charge_rules (and legacy scalar SC config)."""
    tips_account = (doc.get("custom_tips_payable_account") or "").strip()
    rules = doc.get("custom_service_charge_rules") or []
    seen_keys: set[tuple[str, int]] = set()
    for row in rules:
        if cint(row.get("enabled") if row.get("enabled") is not None else 1) != 1:
            continue
        order_type = (row.get("order_type") or "").strip()
        if not order_type:
            frappe.throw(
                coded_message("SERVICE-CHARGE-RULE-INVALID", _("Service Charge rule Order Type is required.")),
                frappe.ValidationError,
            )
        min_pax = cint(row.get("min_pax"))
        if min_pax < 0:
            frappe.throw(
                coded_message("SERVICE-CHARGE-RULE-INVALID", _("Service Charge Min Covers cannot be negative.")),
                frappe.ValidationError,
            )
        key = (order_type.casefold(), min_pax)
        if key in seen_keys:
            frappe.throw(
                coded_message(
                    "SERVICE-CHARGE-RULE-OVERLAP",
                    _("Duplicate enabled Service Charge rule for Order Type '{0}' and Min Covers {1}.").format(
                        order_type, min_pax
                    ),
                ),
                frappe.ValidationError,
            )
        seen_keys.add(key)

        mode = (row.get("charge_mode") or "Percentage").strip()
        if mode == "Fixed Amount":
            if flt(row.get("fixed_amount")) <= 0:
                frappe.throw(
                    coded_message(
                        "SERVICE-CHARGE-RULE-INVALID",
                        _("Fixed Amount Service Charge rule for '{0}' must have a positive fixed amount.").format(order_type),
                    ),
                    frappe.ValidationError,
                )
        elif mode == "Percentage":
            rate = flt(row.get("rate"))
            if rate <= 0 or rate > 100:
                frappe.throw(
                    coded_message(
                        "SERVICE-CHARGE-RULE-INVALID",
                        _("Percentage Service Charge rule for '{0}' must have a rate between 0 and 100.").format(order_type),
                    ),
                    frappe.ValidationError,
                )
            if flt(row.get("min_amount_floor")) < 0:
                frappe.throw(
                    coded_message(
                        "SERVICE-CHARGE-RULE-INVALID",
                        _("Service Charge minimum floor amount cannot be negative."),
                    ),
                    frappe.ValidationError,
                )
        else:
            frappe.throw(
                coded_message("SERVICE-CHARGE-RULE-INVALID", _("Unsupported Service Charge mode: {0}.").format(mode)),
                frappe.ValidationError,
            )

        _validate_service_charge_account(doc, row.get("account_head"), tips_account)

    if not rules and flt(doc.get("custom_service_charge_rate")) > 0:
        _validate_service_charge_account(doc, doc.get("custom_service_charge_account"), tips_account)


def validate_paid_out_approval_threshold(doc, method=None):
    """Normalize custom_paid_out_approval_threshold so 0 stays '0' (every paid-out
    needs approval) while blank stays None (no limit)."""
    raw = doc.get("custom_paid_out_approval_threshold")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        doc.custom_paid_out_approval_threshold = None
        return
    text = str(raw).strip()
    try:
        value = float(text)
    except ValueError:
        frappe.throw(
            coded_message(
                "CASH-THRESHOLD",
                _("Paid-Out Second-Person Threshold must be blank (no limit) or a non-negative number."),
            ),
            frappe.ValidationError,
        )
    if value < 0:
        frappe.throw(
            coded_message(
                "CASH-THRESHOLD",
                _("Paid-Out Second-Person Threshold cannot be negative."),
            ),
            frappe.ValidationError,
        )
    doc.custom_paid_out_approval_threshold = text


def validate_till_cash_thresholds(doc, method=None):
    from frappe.utils import flt

    for field, label in (
        ("custom_paid_in_approval_threshold", "Paid-In Approval Threshold"),
        ("custom_cash_pull_threshold", "Cash Pull Threshold"),
        ("custom_over_short_tolerance", "Closing Over/Short Tolerance"),
    ):
        if flt(doc.get(field)) < 0:
            frappe.throw(
                coded_message("PROFILE-TOLERANCE", _("{0} cannot be negative.").format(_(label))),
                frappe.ValidationError,
            )


def validate_print_format(doc, method=None):
    """Default blank hospitality POS Profile print_format to the guest-check format and
    refuse disabled or XSS-vulnerable standard formats on hospitality profiles.

    Non-hospitality retail POS Profiles (no restaurant or branch) are left free to
    configure or revert to any enabled retail format.
    """
    from ury.ury.print_format.defaults import (
        GUEST_CHECK_PRINT_FORMAT,
        _active_db,
        is_hospitality_pos_profile,
        is_print_format_disabled,
        is_unsafe_pos_invoice_format,
    )

    pf = (doc.get("print_format") or "").strip()
    is_hosp = is_hospitality_pos_profile(doc)
    if not pf:
        if is_hosp:
            db = _active_db()
            if db is not None and db.sql("SELECT 1 FROM `tabPrint Format` WHERE name=%s LIMIT 1", (GUEST_CHECK_PRINT_FORMAT,)):
                doc.print_format = GUEST_CHECK_PRINT_FORMAT
        return
    if is_print_format_disabled(pf):
        frappe.throw(
            _("Print Format {0} is disabled. Choose an enabled print format.").format(pf),
            frappe.ValidationError,
        )
    if is_hosp and is_unsafe_pos_invoice_format(pf):
        frappe.throw(
            _("Print Format {0} is unsafe for hospitality POS Invoice printing. Choose an enabled guest-check format such as {1}.").format(
                pf, GUEST_CHECK_PRINT_FORMAT
            ),
            frappe.ValidationError,
        )


def validate_room_lookup_method(doc, method=None):
    """The till calls custom_room_lookup_method before a Room Charge, so it
    must be a method the request handler would run: an enabled API Server
    Script or a whitelisted Python method. Blank keeps the free-text box."""
    from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

    path = (doc.get("custom_room_lookup_method") or "").strip()
    if not path:
        return
    if path in (get_server_script_map().get("_api") or {}):
        return
    try:
        fn = frappe.get_attr(path)
    except Exception:
        fn = None
    if fn is None or fn not in frappe.whitelisted:
        frappe.throw(
            coded_message(
                "ROOM-LOOKUP-METHOD-INVALID",
                _("Room lookup method {0} is not a whitelisted method or an enabled API Server Script.").format(path),
            ),
            frappe.ValidationError,
        )


def validate_bill_check(doc, method):
    if getattr(doc, "printer_settings", None) and isinstance(doc.printer_settings, (list, tuple)):
        for row in doc.printer_settings:
            if hasattr(row, "bill") and hasattr(row, "printer"):
                if not getattr(row, "bill", None) or not getattr(row, "printer", None):
                    msgprint(
                        _(
                            "Either Bill is not enabled / Printer is not selected in Printer Settings."
                        )
                    )


def validate_cost_center(doc, method):
    pass
