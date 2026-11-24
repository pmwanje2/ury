import frappe
from frappe import _, msgprint

from ury.brand import coded_message
from ury.ury_pos.default_customer import validate_order_type_customers


def validate(doc, method):
    validate_bill_check(doc, method)
    validate_cost_center(doc, method)
    validate_order_type_customers(doc, method)
    validate_room_lookup_method(doc, method)
    validate_till_cash_thresholds(doc, method)


def validate_till_cash_thresholds(doc, method=None):
    from frappe.utils import flt

    for field, label in (
        ("custom_paid_out_approval_threshold", "Paid-Out Approval Threshold"),
        ("custom_paid_in_approval_threshold", "Paid-In Approval Threshold"),
        ("custom_cash_pull_threshold", "Cash Pull Threshold"),
        ("custom_over_short_tolerance", "Closing Over/Short Tolerance"),
    ):
        if flt(doc.get(field)) < 0:
            frappe.throw(
                coded_message("PROFILE-TOLERANCE", _("{0} cannot be negative.").format(_(label))),
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
