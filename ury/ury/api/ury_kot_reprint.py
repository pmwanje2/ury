import frappe
from frappe import _
from frappe.utils import cint

from ury.ury_pos.api import getBranch
from ury.ury.doctype.ury_order.ury_order import _order_ownership_flags


@frappe.whitelist()
def reprint_kot(invoice_number, approval_token=None, reason=None, reason_note=None):
    from ury.ury_pos.privileged_auth import (
        _is_valid_str,
        assert_not_offline_replay,
        authorize_or_perform,
        has_privilege_rules,
    )

    assert_not_offline_replay()

    # Authorization checks run outside the existing try/except below so a
    # frappe.PermissionError or POS-AUTH/POS-REASON error isn't swallowed and
    # rewritten into the generic "unexpected error" message.
    pos_invoice = frappe.get_doc("POS Invoice", invoice_number)

    if not frappe.has_permission("POS Invoice", "read", doc=pos_invoice):
        frappe.throw(_("Not permitted to view this order"), frappe.PermissionError)

    try:
        user_branch = getBranch()
    except frappe.ValidationError:
        if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
            user_branch = None
        else:
            raise

    if user_branch and pos_invoice.branch and pos_invoice.branch != user_branch:
        frappe.throw(
            _("Not permitted to reprint KOT for orders outside your active branch"),
            frappe.PermissionError,
        )

    if not pos_invoice.pos_profile:
        frappe.throw(f"POS Profile not found for Invoice {invoice_number}.")

    # Captain ownership/elevated-access check, reusing Phase 2's ownership
    # formula as starting fallback when no POS Privilege Rule rows exist.
    flags = _order_ownership_flags(pos_invoice, pos_invoice.pos_profile)
    legacy_allowed = bool(flags["is_mine"] or flags["has_elevated_access"])
    raw_company = pos_invoice.get("company")
    company = raw_company if _is_valid_str(raw_company) else None
    prior_reprints = cint(pos_invoice.get("custom_kot_reprint_count") or 0) if isinstance(
        pos_invoice.get("custom_kot_reprint_count"), (int, float, str)
    ) else 0
    next_reprint_number = prior_reprints + 1
    policy_active = has_privilege_rules(company, "reprint_kot")

    auth = None
    if policy_active or approval_token or reason is not None or not legacy_allowed:
        auth = authorize_or_perform(
            privilege="reprint_kot",
            pos_profile=pos_invoice.pos_profile,
            company=company,
            skip_profile_company_lookup=not isinstance(raw_company, (str, type(None))),
            reference_doctype="POS Invoice",
            reference_name=invoice_number,
            claim={
                "action": "reprint_kot",
                "invoice": invoice_number,
                "reprint_number": next_reprint_number,
            },
            token=approval_token,
            reason=reason,
            reason_note=reason_note,
            require_reason=None if (policy_active or approval_token or reason is not None) else False,
            allow_fallback_predicate=legacy_allowed,
            deny_message=_("Not permitted to reprint KOT for another Captain's order."),
        )

    try:
        pos_profile, restaurant_table, order_type = frappe.db.get_value(
            "POS Invoice", invoice_number, ["pos_profile", "restaurant_table","order_type"]
        )
        if not pos_profile:
            frappe.throw(f"POS Profile not found for Invoice {invoice_number}.")

        enable_kot_reprint, kot_print_format, table_order_printer, parcel_order_printer = frappe.db.get_value(
            "POS Profile", pos_profile,
            ["custom_enable_kot_reprint", "custom_reprint_kot_format", "custom_table_order_printer", "custom_parcel_order_printer"]
        )

        # A verified Manager PIN (or explicit reprint_kot privilege) can re-fire
        # a jammed kitchen ticket even if free KOT reprint is toggled off.
        if not cint(enable_kot_reprint) and not (auth and auth.get("via_pin")):
            frappe.throw("KOT Reprint is disabled in POS Profile.")

        if not kot_print_format:
            frappe.throw("No KOT Reprint Print Format is set in POS Profile.")
        
        printer = table_order_printer if order_type == "Dine In" else parcel_order_printer

        if not printer:
            frappe.throw("No printer is assigned for reprinting KOT.")

        if auth is not None:
            reprint_meta = {
                "invoice": invoice_number,
                "is_reprint": True,
                "reprint_number": next_reprint_number,
                "reprint_label": f"RE-FIRE / REPRINT #{next_reprint_number}",
                "reprint_reason": auth.get("reason") or "",
                "reprint_approver": auth.get("approver") or "",
            }
            print_kot(printer, invoice_number, kot_print_format, extra_metadata=reprint_meta)
            if isinstance(pos_invoice.get("custom_kot_reprint_count"), (int, float)):
                frappe.db.set_value(
                    "POS Invoice",
                    invoice_number,
                    "custom_kot_reprint_count",
                    next_reprint_number,
                    update_modified=False,
                )
        else:
            print_kot(printer, invoice_number, kot_print_format)

        return "Success"

    except (frappe.PermissionError, frappe.ValidationError):
        raise
    except Exception as e:
        error_message = f"KOT Reprint Error for Invoice {invoice_number}: {str(e)}"
        frappe.log_error(error_message, "KOT Reprint Error")
        frappe.throw("An unexpected error occurred while reprinting KOT. Please check logs.")


def print_by_server(doctype, name, printer_setting, print_format=None, **kwargs):
    printer_doc = frappe.get_doc("Network Printer Settings", printer_setting)
    extra_metadata = kwargs.get("extra_metadata") or {
        "invoice": name,
        "is_reprint": True,
        "reprint_label": "RE-FIRE / REPRINT",
    }
    return printer_doc.print_doc(
        doctype=doctype,
        name=name,
        print_format=print_format,
        job_type=kwargs.get("job_type", "KOT_REPRINT"),
        extra_metadata=extra_metadata,
    )


def print_kot(printer, docname, kot_print_format, restaurant_table=None, order_type=None, extra_metadata=None):
    try:
        kwargs = {"extra_metadata": extra_metadata} if extra_metadata else {}
        print_by_server("POS Invoice", docname, printer, kot_print_format, **kwargs)
    except Exception as e:
        frappe.log_error(f"KOT Reprint Error: {e}", "KOT Reprint Error")