import frappe
from frappe import _

import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from ury.ury.doctype.ury_order.ury_order import _free_tables_if_no_open_invoices, check_split_target_access

no_cache = 1

base_template_path = "www/printview.html"
standard_format = "templates/print_formats/standard.html"

from frappe.www.printview import validate_print_permission


class PrintResult(dict):
    """Print job result that compares equal to its status string.

    Lets callers keep checking ``result == "Success"`` (legacy behaviour)
    while the engine returns a rich dict.
    """

    def __eq__(self, other):
        if isinstance(other, str):
            return self.get("status") == other
        return super().__eq__(other)

    def __str__(self):
        return self.get("status") or super().__str__()


def _mark_invoice_printed(invoice_name, was_printed=None, update_modified=True):
	"""Mark a POS Invoice as printed.

	The printing time is recorded only on the first print (the 0 -> 1 transition
	of invoice_printed), so reprints do not reset the time-limit warning.
	Pass ``was_printed`` when the caller has already read invoice_printed.
	"""
	if was_printed is None:
		was_printed = frappe.db.get_value("POS Invoice", invoice_name, "invoice_printed")
	values = {"invoice_printed": 1}
	if not was_printed:
		values["custom_printing_time"] = frappe.utils.now_datetime()
	frappe.db.set_value(
		"POS Invoice", invoice_name, values, update_modified=update_modified
	)


@frappe.whitelist()
def network_printing(
    doctype,
    name,
    printer_setting,
    print_format=None,
    doc=None,
    no_letterhead=0,
    approval_token=None,
    reason=None,
    reason_note=None,
    _reprint_meta=None,
):
    target = frappe.get_doc(doctype, name)
    validate_print_permission(target)
    reprint_meta = _reprint_meta
    if doctype == "POS Invoice":
        check_split_target_access(target, "write")
        doc = target
        if reprint_meta is None:
            from ury.ury_pos.floor_actions import authorize_and_count_bill_print

            reprint_meta = authorize_and_count_bill_print(
                name,
                approval_token=approval_token,
                reason=reason,
                reason_note=reason_note,
                pos_profile=getattr(doc, "pos_profile", None),
            )
        if reprint_meta and reprint_meta.get("is_reprint"):
            doc.copy_number = reprint_meta.get("copy_number")

    restaurant_table = None
    if doctype == "POS Invoice":
        restaurant_table = frappe.db.get_value("POS Invoice", name, "restaurant_table")

    if doctype == "POS Invoice":
        from ury.ury.print_format.defaults import (
            resolve_safe_pos_print_format,
            validate_pos_invoice_print_format,
        )

        if print_format:
            validate_pos_invoice_print_format(target, print_format)
        else:
            pos_profile = getattr(doc, "pos_profile", None) or frappe.db.get_value("POS Invoice", name, "pos_profile")
            if pos_profile:
                print_format = frappe.db.get_value("POS Profile", pos_profile, "print_format")
            default_meta_fmt = (
                getattr(frappe.get_meta("POS Invoice"), "default_print_format", None)
                if hasattr(frappe, "get_meta")
                else None
            )
            print_format = resolve_safe_pos_print_format(print_format, default_format=default_meta_fmt)

    extra_metadata = {
        "invoice": name,
        "table": restaurant_table,
        "restaurant_table": restaurant_table,
        "job_owner": getattr(frappe.session, "user", "Administrator"),
    }
    if reprint_meta:
        extra_metadata.update(
            {
                "print_number": reprint_meta.get("print_number"),
                "is_reprint": bool(reprint_meta.get("is_reprint")),
                "copy_number": reprint_meta.get("copy_number"),
                "reprint_label": reprint_meta.get("reprint_label") or "",
                "reprint_reason": reprint_meta.get("reason") or "",
                "reprint_approver": reprint_meta.get("approver") or "",
            }
        )

    printer_doc = frappe.get_doc("Network Printer Settings", printer_setting)
    result = printer_doc.print_doc(
        doctype=doctype,
        name=name,
        print_format=print_format,
        doc=doc,
        no_letterhead=no_letterhead,
        job_type="BILL",
        extra_metadata=extra_metadata,
    )

    if isinstance(result, dict) and result.get("status") == "Success":
        result["invoice"] = name

        from ury.ury.api.ury_fulfilment_posting_service import trigger_manufacture_on_bill_print
        trigger_manufacture_on_bill_print(name)

    return PrintResult(result) if isinstance(result, dict) else result


def _bill_printers(pos_profile, table):
    bill_printers = []

    if table:
        room = frappe.db.get_value("URY Table", table, "restaurant_room")
        if room:
            bill_printers = frappe.get_all(
                "URY Printer Settings",
                filters={"parent": room, "parenttype": "URY Room", "bill": 1},
                pluck="printer",
                order_by="idx"
            )
    else:
        bill_printers = frappe.get_all(
            "URY Printer Settings",
            filters={"parent": pos_profile, "parenttype": "POS Profile", "bill": 1},
            pluck="printer",
            order_by="idx"
        )

    if not bill_printers and pos_profile:
        bill_printers = frappe.get_all(
            "URY Printer Settings",
            filters={"parent": pos_profile, "parenttype": "POS Profile", "bill": 1},
            pluck="printer",
            order_by="idx"
        )

    return bill_printers


@frappe.whitelist()
def select_network_printer(
    pos_profile,
    invoice_id,
    print_format=None,
    approval_token=None,
    reason=None,
    reason_note=None,
):
    invoice_doc = frappe.get_doc("POS Invoice", invoice_id)
    if not frappe.has_permission("POS Invoice", "write", doc=invoice_doc):
        frappe.throw(_("Not permitted to print this invoice"), frappe.PermissionError)

    check_split_target_access(invoice_doc, "write")
    from ury.ury_pos.floor_actions import authorize_and_count_bill_print

    reprint_meta = authorize_and_count_bill_print(
        invoice_id,
        approval_token=approval_token,
        reason=reason,
        reason_note=reason_note,
        pos_profile=pos_profile or getattr(invoice_doc, "pos_profile", None),
    )

    table = frappe.db.get_value("POS Invoice", invoice_id, "restaurant_table")
    from ury.ury.print_format.defaults import (
        resolve_safe_pos_print_format,
        validate_pos_invoice_print_format,
    )

    if print_format:
        validate_pos_invoice_print_format(invoice_doc, print_format)
    else:
        if pos_profile:
            print_format = frappe.db.get_value("POS Profile", pos_profile, "print_format")
        default_meta_fmt = (
            getattr(frappe.get_meta("POS Invoice"), "default_print_format", None)
            if hasattr(frappe, "get_meta")
            else None
        )
        print_format = resolve_safe_pos_print_format(print_format, default_format=default_meta_fmt)

    print_jobs = []
    bill_printers = _bill_printers(pos_profile, table)

    if bill_printers:
        for printer in bill_printers:
            print_result = network_printing(
                "POS Invoice", invoice_id, printer, print_format, _reprint_meta=reprint_meta
            )
            print_jobs.append(print_result)

    any_succeeded = any(
        (isinstance(job, dict) and job.get("status") == "Success") or job == "Success"
        for job in print_jobs
    )

    if not bill_printers:
        return None

    return PrintResult({
        "status": "Success" if any_succeeded else "Failure",
        "print_jobs": print_jobs,
        "invoice": invoice_id,
        "print_number": reprint_meta.get("print_number"),
        "is_reprint": bool(reprint_meta.get("is_reprint")),
        "reprint_label": reprint_meta.get("reprint_label") or "",
    })


@frappe.whitelist()
def qz_print_update(invoice, approval_token=None, reason=None, reason_note=None):
    invoice_doc = frappe.get_doc("POS Invoice", invoice)
    if not frappe.has_permission("POS Invoice", "write", doc=invoice_doc):
        frappe.throw(_("Not permitted to print this invoice"), frappe.PermissionError)

    check_split_target_access(invoice_doc, "write")
    from ury.ury_pos.floor_actions import authorize_and_count_bill_print

    reprint_meta = authorize_and_count_bill_print(
        invoice,
        approval_token=approval_token,
        reason=reason,
        reason_note=reason_note,
        pos_profile=getattr(invoice_doc, "pos_profile", None),
    )

    try:
        table = frappe.db.get_value("POS Invoice", invoice, "restaurant_table")
        
        if table == None or table == "":
            # Update invoice_printed
            _mark_invoice_printed(invoice, update_modified=False)
            
            # Validate the update
            new_invoice_printed = frappe.db.get_value("POS Invoice", invoice, "invoice_printed")
            if new_invoice_printed != 1:
                return {"status": "Failure"}                
        else:
            invoice_printed = frappe.db.get_value("POS Invoice", invoice, "invoice_printed")

            if invoice_printed == 0:
                # Update invoice_printed
                _mark_invoice_printed(invoice, was_printed=0, update_modified=False)

                _free_tables_if_no_open_invoices(table)
                # Validate both updates
                new_invoice_printed = frappe.db.get_value("POS Invoice", invoice, "invoice_printed")
                
                if new_invoice_printed != 1:
                    return {"status": "Failure"}
        
        from ury.ury.api.ury_fulfilment_posting_service import trigger_manufacture_on_bill_print
        trigger_manufacture_on_bill_print(invoice)

        if reprint_meta and reprint_meta.get("is_reprint"):
            return {
                "status": "Success",
                "print_number": reprint_meta.get("print_number"),
                "is_reprint": True,
                "reprint_label": reprint_meta.get("reprint_label") or "",
            }
        return {"status": "Success"}
        
    except (frappe.PermissionError, frappe.ValidationError):
        raise
    except Exception as e:
        frappe.log_error(message=e, title="Print Fail")
        frappe.throw(_("Error while printing order",e))                   
        return {"status": "Failure"}


@frappe.whitelist()
def print_pos_page(doctype, name, print_format, approval_token=None, reason=None, reason_note=None):
    doc_to_check = frappe.get_doc(doctype, name)
    if not frappe.has_permission(doctype, "write", doc=doc_to_check):
        frappe.throw(_("Not permitted to print this document"), frappe.PermissionError)

    reprint_meta = None
    if doctype == "POS Invoice":
        from ury.ury.print_format.defaults import (
            resolve_safe_pos_print_format,
            validate_pos_invoice_print_format,
        )

        check_split_target_access(doc_to_check, "write")
        if print_format:
            validate_pos_invoice_print_format(doc_to_check, print_format)
        else:
            default_meta_fmt = (
                getattr(frappe.get_meta("POS Invoice"), "default_print_format", None)
                if hasattr(frappe, "get_meta")
                else None
            )
            print_format = resolve_safe_pos_print_format(None, default_format=default_meta_fmt)
        from ury.ury_pos.floor_actions import authorize_and_count_bill_print

        reprint_meta = authorize_and_count_bill_print(
            name,
            approval_token=approval_token,
            reason=reason,
            reason_note=reason_note,
            pos_profile=getattr(doc_to_check, "pos_profile", None),
        )
    data = {
        "name": name,
        "doctype": doctype,
        "print_format": print_format,
        "is_reprint": bool(reprint_meta.get("is_reprint")) if reprint_meta else False,
        "copy_number": reprint_meta.get("copy_number") if reprint_meta else None,
        "reprint_label": reprint_meta.get("reprint_label") if reprint_meta else "",
    }

    restaurant_table, branch, name = frappe.db.get_value(
        "POS Invoice", name, ["restaurant_table", "branch", "name"]
    )
    print_channel = "{}_{}".format("print", branch)
    frappe.publish_realtime(print_channel, {"data": data})

    invoice_printed = frappe.db.get_value("POS Invoice", name, "invoice_printed")

    if invoice_printed == 0:
        _mark_invoice_printed(name, was_printed=0)

        if restaurant_table:
            _free_tables_if_no_open_invoices(restaurant_table)

    from ury.ury.api.ury_fulfilment_posting_service import trigger_manufacture_on_bill_print
    trigger_manufacture_on_bill_print(name)


@frappe.whitelist()
def qz_certificate():
    if frappe.session.user == "Guest":
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    site_config = frappe.get_site_config()
    qz_key_value = site_config.get("qz_cert")

    return qz_key_value


# Roles allowed to request QZ Tray signatures. The private key never leaves
# the server; these users may only ask the server to sign a payload.
QZ_SIGNING_ROLES = {
    "Administrator",
    "System Manager",
    "URY Admin",
    "URY Manager",
    "URY Cashier",
    # Kitchen station screens print their own KOTs through QZ (Mosaic station printing).
    "Restaurant Kitchen",
}


def _get_qz_private_key():
    """Read the QZ private key server-side.

    `qz_private_key` in site_config may hold the PEM contents directly or a
    path relative to the site's `private` folder.
    """
    key_value = (frappe.get_site_config().get("qz_private_key") or "").strip()

    if not key_value:
        frappe.throw(_("QZ private key is not configured in site config"))

    if key_value.startswith("-----BEGIN"):
        return key_value.encode()

    private_root = os.path.abspath(frappe.get_site_path("private"))
    key_path = os.path.abspath(frappe.get_site_path("private", key_value))

    if os.path.commonpath([key_path, private_root]) != private_root:
        frappe.throw(_("Invalid QZ private key path in site config"))

    with open(key_path, "rb") as key_file:
        return key_file.read()


@frappe.whitelist()
def signature_promise(toSign=None):
    """Server-side QZ Tray signing endpoint (sign-only)."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    if not QZ_SIGNING_ROLES.intersection(frappe.get_roles()):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    if not toSign:
        frappe.throw(_("Missing payload to sign"))

    private_key = serialization.load_pem_private_key(
        _get_qz_private_key(), password=None
    )

    signature = private_key.sign(
        toSign.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA512(),
    )

    return base64.b64encode(signature).decode("ascii")


@frappe.whitelist()
def get_print_job_status(print_job_id):
    """Return the current metadata for a URY Print Job.

    The job is a Virtual DocType backed by JSON files. If the job is not
    found, a structured failure response is returned instead of raising.
    """
    if not print_job_id:
        return {"status": "Failure", "message": "print_job_id is required"}

    try:
        doc = frappe.get_doc("URY Print Job", print_job_id)
        return {
            "status": "Success",
            "print_job": doc.as_dict(),
        }
    except frappe.DoesNotExistError:
        return {
            "status": "Failure",
            "message": f"Print job {print_job_id} not found",
        }
    except Exception as e:
        frappe.logger("printing").warning(
            {"event": "get_print_job_status_failed", "print_job_id": print_job_id},
            exc_info=True,
        )
        return {"status": "Failure", "message": str(e)}


def _assert_drawer_profile_access(pos_profile: str):
    """Fail-closed authorization and branch isolation check for cash drawer endpoints."""
    from ury.brand import coded_message

    user = getattr(getattr(frappe, "session", None), "user", None)
    if not user or user == "Guest":
        frappe.throw(
            coded_message("DRAWER-KICK-NOT-PERMITTED", _("Sign in to operate the cash drawer.")),
            frappe.PermissionError,
        )

    if not pos_profile:
        frappe.throw(
            coded_message("DRAWER-KICK-PROFILE-REQUIRED", _("A POS Profile is required.")),
            frappe.ValidationError,
        )

    profile_doc = frappe.get_doc("POS Profile", pos_profile)
    roles = set(frappe.get_roles() or [])
    is_admin = user == "Administrator" or "System Manager" in roles or "URY Admin" in roles

    if not is_admin:
        if not frappe.has_permission("POS Profile", "read", doc=profile_doc):
            frappe.throw(
                coded_message(
                    "DRAWER-KICK-NOT-PERMITTED",
                    _("You are not permitted to operate the cash drawer for this POS Profile."),
                ),
                frappe.PermissionError,
            )

        assigned_users = [
            row.user
            for row in (getattr(profile_doc, "applicable_for_users", None) or [])
            if getattr(row, "user", None)
        ]
        if assigned_users and user not in assigned_users:
            frappe.throw(
                coded_message(
                    "DRAWER-KICK-NOT-ASSIGNED",
                    _("You are not assigned to this POS Profile."),
                ),
                frappe.PermissionError,
            )

        profile_branch = (
            profile_doc.get("branch")
            if hasattr(profile_doc, "get")
            else getattr(profile_doc, "branch", None)
        )
        if profile_branch:
            from ury.ury_pos.api import getBranch

            try:
                user_branch = getBranch()
            except Exception:
                frappe.throw(
                    coded_message(
                        "DRAWER-KICK-BRANCH-REQUIRED",
                        _("Your user account is not assigned to a branch for this POS Profile."),
                    ),
                    frappe.PermissionError,
                )
            if user_branch and user_branch != profile_branch:
                frappe.throw(
                    coded_message(
                        "DRAWER-KICK-OTHER-BRANCH",
                        _("You are not permitted to operate a cash drawer in another branch."),
                    ),
                    frappe.PermissionError,
                )

    return profile_doc


def mode_of_payment_opens_drawer(mode_of_payment: str) -> bool:
    """Return True strictly when `Mode of Payment.custom_open_cash_drawer` is enabled.

    Never hardcodes tender names such as "Cash".
    """
    from frappe.utils import cint

    if not mode_of_payment or not str(mode_of_payment).strip():
        return False
    val = frappe.db.get_value(
        "Mode of Payment", str(mode_of_payment).strip(), "custom_open_cash_drawer"
    )
    return bool(cint(val))


def get_drawer_opening_modes(pos_profile: str) -> list[str]:
    """Return all Mode of Payment names on `pos_profile` configured to open the cash drawer."""
    profile_doc = frappe.get_doc("POS Profile", pos_profile)
    modes = []
    for row in getattr(profile_doc, "payments", None) or []:
        mop = getattr(row, "mode_of_payment", None)
        if mop and mode_of_payment_opens_drawer(mop) and mop not in modes:
            modes.append(mop)
    return modes


@frappe.whitelist()
def get_drawer_kick_config(pos_profile):
    """Return cash drawer hardware pin/hex and configured drawer-opening payment methods."""
    from frappe.utils import cint
    from ury.ury.printing.service import build_drawer_kick_bytes

    profile_doc = _assert_drawer_profile_access(pos_profile)
    raw_pin = (
        profile_doc.get("custom_drawer_pin")
        if hasattr(profile_doc, "get")
        else getattr(profile_doc, "custom_drawer_pin", None)
    )
    raw_hex = (
        profile_doc.get("custom_drawer_kick_hex")
        if hasattr(profile_doc, "get")
        else getattr(profile_doc, "custom_drawer_kick_hex", None)
    )
    auto_kick = (
        profile_doc.get("custom_auto_kick_on_cash_tender")
        if hasattr(profile_doc, "get")
        else getattr(profile_doc, "custom_auto_kick_on_cash_tender", 1)
    )
    resolved_pin = cint(raw_pin) if raw_pin not in (None, "") else 2
    if resolved_pin not in (2, 5):
        resolved_pin = 2
    _, normalized_hex = build_drawer_kick_bytes(
        drawer_pin=resolved_pin, hex_command=raw_hex or None
    )
    return {
        "pos_profile": pos_profile,
        "drawer_pin": resolved_pin,
        "hex": normalized_hex,
        "auto_kick_on_cash_tender": 1 if auto_kick is None else cint(auto_kick),
        "drawer_opening_modes": get_drawer_opening_modes(pos_profile),
    }


def _allowed_drawer_printers(pos_profile: str, profile_doc) -> set[str]:
    """Return bill printers configured on `pos_profile` or rooms in `profile_doc.branch`."""
    allowed: set[str] = set()
    for printer in _bill_printers(pos_profile, None) or []:
        if isinstance(printer, str) and printer.strip():
            allowed.add(printer.strip())

    for row in getattr(profile_doc, "printer_settings", None) or []:
        printer = getattr(row, "printer", None)
        if getattr(row, "bill", 0) and isinstance(printer, str) and printer.strip():
            allowed.add(printer.strip())

    profile_branch = (
        profile_doc.get("branch")
        if hasattr(profile_doc, "get")
        else getattr(profile_doc, "branch", None)
    )
    if isinstance(profile_branch, str) and profile_branch.strip():
        rooms = frappe.get_all(
            "URY Room",
            filters={"branch": profile_branch.strip()},
            pluck="name",
        ) or []
        room_names = [r for r in rooms if isinstance(r, str) and r.strip()]
        if room_names:
            room_printers = frappe.get_all(
                "URY Printer Settings",
                filters={
                    "parent": ["in", room_names],
                    "parenttype": "URY Room",
                    "bill": 1,
                },
                pluck="printer",
            ) or []
            for printer in room_printers:
                if isinstance(printer, str) and printer.strip():
                    allowed.add(printer.strip())

    return allowed


def _update_drawer_event_delivery(drawer_event: str | None, delivery_status: str) -> None:
    if not drawer_event:
        return
    try:
        if not frappe.db.exists("DocType", "POS Drawer Event"):
            return
        if not frappe.db.exists("POS Drawer Event", drawer_event):
            return
        updates = {"drawer_kick_sent": 1 if delivery_status == "Sent" else 0}
        meta = frappe.get_meta("POS Drawer Event")
        if meta and hasattr(meta, "has_field") and meta.has_field("custom_kick_delivery_status"):
            updates["custom_kick_delivery_status"] = delivery_status
        frappe.db.set_value("POS Drawer Event", drawer_event, updates, update_modified=False)
    except Exception:
        frappe.logger("printing").warning(
            {"event": "drawer_event_status_update_failed", "drawer_event": drawer_event},
            exc_info=True,
        )


@frappe.whitelist(methods=["POST"])
def kick_cash_drawer(
    pos_profile,
    drawer_pin=None,
    reason=None,
    hex_command=None,
    drawer_event=None,
    printer_setting=None,
):
    """Send an ESC/POS cash drawer kick pulse to the profile's network/CUPS thermal printer.

    Gracefully returns `Not Configured` or `Failed` when no hardware printer is
    attached or reachable — never crashes checkout or till workflows.
    """
    from frappe.utils import cint
    from ury.brand import coded_message
    from ury.ury.printing.service import build_drawer_kick_bytes, send_raw_escpos

    if frappe.request and getattr(frappe.request, "method", None) != "POST":
        frappe.throw(
            coded_message("DRAWER-KICK-POST-ONLY", _("POST requests only")),
            frappe.PermissionError,
        )

    profile_doc = _assert_drawer_profile_access(pos_profile)

    profile_pin = (
        profile_doc.get("custom_drawer_pin")
        if hasattr(profile_doc, "get")
        else getattr(profile_doc, "custom_drawer_pin", None)
    )
    profile_hex = (
        profile_doc.get("custom_drawer_kick_hex")
        if hasattr(profile_doc, "get")
        else getattr(profile_doc, "custom_drawer_kick_hex", None)
    )

    resolved_pin_raw = drawer_pin if drawer_pin not in (None, "") else (profile_pin or 2)
    resolved_hex_raw = hex_command if (hex_command is not None and str(hex_command).strip()) else (profile_hex or None)
    try:
        resolved_pin = cint(resolved_pin_raw) if resolved_pin_raw not in (None, "") else 2
        raw_bytes, normalized_hex = build_drawer_kick_bytes(
            drawer_pin=resolved_pin,
            hex_command=resolved_hex_raw,
        )
    except ValueError as exc:
        frappe.throw(
            coded_message("DRAWER-KICK-INVALID-COMMAND", str(exc)),
            frappe.ValidationError,
        )

    target_printer_setting = (
        str(printer_setting).strip()
        if printer_setting is not None and str(printer_setting).strip()
        else None
    )
    if target_printer_setting:
        allowed_printers = _allowed_drawer_printers(pos_profile, profile_doc)
        if target_printer_setting not in allowed_printers:
            frappe.throw(
                coded_message(
                    "DRAWER-OTHER-BRANCH",
                    _("Selected printer is not configured for this POS Profile or branch."),
                ),
                frappe.PermissionError,
            )
    else:
        printers = _bill_printers(pos_profile, None)
        if printers:
            target_printer_setting = printers[0]

    delivery = {
        "status": "Not Configured",
        "transport": "none",
        "message": "No hardware printer is attached to this POS Profile.",
    }
    if target_printer_setting:
        try:
            printer_doc = frappe.get_doc("Network Printer Settings", target_printer_setting)
            raw_host = (
                printer_doc.get("custom_raw_escpos_host")
                if hasattr(printer_doc, "get")
                else getattr(printer_doc, "custom_raw_escpos_host", None)
            )
            raw_port = (
                printer_doc.get("custom_raw_escpos_port")
                if hasattr(printer_doc, "get")
                else getattr(printer_doc, "custom_raw_escpos_port", 9100)
            )
            delivery = send_raw_escpos(
                raw_bytes,
                host=raw_host or None,
                port=cint(raw_port) or 9100,
                cups_server_ip=getattr(printer_doc, "server_ip", None),
                cups_port=cint(getattr(printer_doc, "port", 631)) or 631,
                cups_printer_name=getattr(printer_doc, "printer_name", None),
            )
        except Exception as exc:
            delivery = {
                "status": "Failed",
                "transport": "none",
                "message": str(exc),
            }

    _update_drawer_event_delivery(drawer_event, delivery["status"])

    frappe.logger("printing").info(
        {
            "event": "cash_drawer_kick",
            "pos_profile": pos_profile,
            "user": getattr(getattr(frappe, "session", None), "user", None),
            "drawer_pin": resolved_pin,
            "hex": normalized_hex,
            "reason": reason or "",
            "status": delivery["status"],
            "transport": delivery["transport"],
            "printer_setting": target_printer_setting,
        }
    )

    return {
        "status": delivery["status"],
        "transport": delivery["transport"],
        "drawer_pin": resolved_pin,
        "hex": normalized_hex,
        "bytes_length": len(raw_bytes),
        "pos_profile": pos_profile,
        "reason": reason or "",
        "message": delivery.get("message") or "",
    }


