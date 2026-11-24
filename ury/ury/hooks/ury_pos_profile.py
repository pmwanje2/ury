import frappe
from frappe import _, msgprint

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
