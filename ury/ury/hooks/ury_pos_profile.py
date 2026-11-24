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
