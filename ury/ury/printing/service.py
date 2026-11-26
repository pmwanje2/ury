import os
import re
import socket
import tempfile

import frappe
from frappe.utils import now_datetime
from frappe.www.printview import validate_print_permission

from ury.ury.printing.print_job_monitor import register_print_job

ESC_POS_DRAWER_KICK_HEX = {
    2: "1b700019fa",  # ESC p 0 25 250 (Drawer 1 / Pin 2)
    5: "1b700119fa",  # ESC p 1 25 250 (Drawer 2 / Pin 5)
}
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def build_drawer_kick_bytes(
    drawer_pin: int | str | None = 2,
    hex_command: str | None = None,
) -> tuple[bytes, str]:
    """Return validated `(raw_bytes, normalized_hex)` for an ESC/POS cash drawer kick."""
    if hex_command is not None and str(hex_command).strip():
        cleaned = re.sub(r"[\s:,-]+", "", str(hex_command).strip()).lower()
        if cleaned.startswith("0x"):
            cleaned = cleaned[2:]
        if (
            not cleaned
            or len(cleaned) % 2 != 0
            or len(cleaned) > 64
            or not _HEX_RE.match(cleaned)
        ):
            raise ValueError("Drawer kick hex command must be an even-length hexadecimal string (2-64 chars).")
        return bytes.fromhex(cleaned), cleaned

    try:
        pin = int(drawer_pin) if drawer_pin not in (None, "") else 2
    except (TypeError, ValueError) as exc:
        raise ValueError("Drawer pin must be 2 or 5.") from exc

    if pin not in ESC_POS_DRAWER_KICK_HEX:
        raise ValueError(f"Unsupported cash drawer pin '{pin}'. Use Pin 2 or Pin 5.")

    normalized_hex = ESC_POS_DRAWER_KICK_HEX[pin]
    return bytes.fromhex(normalized_hex), normalized_hex


def send_raw_escpos(
    raw_bytes: bytes,
    *,
    host: str | None = None,
    port: int = 9100,
    cups_server_ip: str | None = None,
    cups_port: int = 631,
    cups_printer_name: str | None = None,
    timeout: float = 3.0,
) -> dict:
    """Deliver raw ESC/POS bytes over direct TCP/9100 or a CUPS raw queue.

    Never raises for missing/offline hardware: returns a structured status dict
    (`Sent`, `Failed`, or `Not Configured`) so checkout/till flows never crash.
    """
    if not isinstance(raw_bytes, (bytes, bytearray)) or len(raw_bytes) == 0:
        return {
            "status": "Failed",
            "transport": "none",
            "message": "Empty raw ESC/POS command payload.",
        }

    payload = bytes(raw_bytes)

    if host and str(host).strip():
        target_host = str(host).strip()
        target_port = int(port or 9100)
        try:
            with socket.create_connection((target_host, target_port), timeout=timeout) as sock:
                sock.sendall(payload)
            return {
                "status": "Sent",
                "transport": "tcp",
                "target": f"{target_host}:{target_port}",
                "bytes_sent": len(payload),
            }
        except Exception as exc:
            frappe.logger("printing").warning(
                {
                    "event": "raw_escpos_tcp_failed",
                    "host": target_host,
                    "port": target_port,
                    "error": str(exc),
                }
            )
            return {
                "status": "Failed",
                "transport": "tcp",
                "target": f"{target_host}:{target_port}",
                "message": str(exc),
            }

    if cups_server_ip and cups_printer_name:
        tmp_path = None
        try:
            import cups

            cups.setServer(str(cups_server_ip).strip())
            cups.setPort(int(cups_port or 631))
            conn = cups.Connection()
            with tempfile.NamedTemporaryFile(prefix="pos-escpos-", suffix=".bin", delete=False) as tmp:
                tmp.write(payload)
                tmp_path = tmp.name
            cups_job_id = conn.printFile(
                str(cups_printer_name).strip(),
                tmp_path,
                "cash-drawer-kick",
                {"raw": "true"},
            )
            return {
                "status": "Sent",
                "transport": "cups",
                "target": str(cups_printer_name).strip(),
                "cups_job_id": cups_job_id,
                "bytes_sent": len(payload),
            }
        except Exception as exc:
            frappe.logger("printing").warning(
                {
                    "event": "raw_escpos_cups_failed",
                    "server_ip": cups_server_ip,
                    "printer_name": cups_printer_name,
                    "error": str(exc),
                }
            )
            return {
                "status": "Failed",
                "transport": "cups",
                "target": str(cups_printer_name).strip(),
                "message": str(exc),
            }
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    return {
        "status": "Not Configured",
        "transport": "none",
        "message": "No network or CUPS printer is configured for raw ESC/POS commands.",
    }


def is_print_status_tracking_disabled(pos_profile: str | None = None) -> bool:
    """Check whether print status and printer monitoring are disabled for the given profile or globally."""
    if pos_profile:
        return bool(frappe.db.get_value("POS Profile", pos_profile, "custom_disable_print_status_tracking"))

    # If no specific profile was given, check active user's pos_profile if available in session
    return False


def _make_print_job_id(printer_name: str = "Printer", cups_job_id: int | None = None) -> str:
    """Generate a unique print job identity formatted as Printer Name + CUPS Job ID."""
    clean_printer = str(printer_name or "Printer").strip().replace(" ", "_")
    if cups_job_id is not None:
        return f"{clean_printer}-{cups_job_id}"
    return f"{clean_printer}-{now_datetime().strftime('%Y%m%d%H%M%S')}-{frappe.generate_hash(length=4)}"


def _resolve_backup_printer_setting(
    printer_setting: str,
    explicit_backup: str | None = None,
    target_doc=None,
) -> str | None:
    """Resolve a backup `Network Printer Settings` record for automatic failover."""
    candidates = []
    if explicit_backup and str(explicit_backup).strip():
        candidates.append(str(explicit_backup).strip())

    try:
        nps_backup = frappe.db.get_value(
            "Network Printer Settings", printer_setting, "custom_backup_printer"
        )
        if isinstance(nps_backup, str) and nps_backup.strip():
            candidates.append(nps_backup.strip())
    except Exception:
        pass

    production = getattr(target_doc, "production", None) if target_doc is not None else None
    if isinstance(production, str) and production.strip():
        try:
            unit_backup = frappe.db.get_value(
                "URY Production Unit", production.strip(), "custom_backup_printer"
            )
            if isinstance(unit_backup, str) and unit_backup.strip():
                if frappe.db.exists("Network Printer Settings", unit_backup.strip()):
                    candidates.append(unit_backup.strip())
        except Exception:
            pass

    for candidate in candidates:
        if candidate and candidate != printer_setting:
            return candidate
    return None


def _record_kot_failover_comment(target_doc, backup_printer: str) -> None:
    if target_doc is None:
        return
    try:
        if hasattr(target_doc, "add_comment"):
            target_doc.add_comment("Comment", f"[KOT-FAILOVER-PRINTED] {backup_printer}")
    except Exception:
        frappe.logger("printing").warning(
            {
                "event": "kot_failover_comment_failed",
                "kot": getattr(target_doc, "name", None),
                "backup_printer": backup_printer,
            },
            exc_info=True,
        )


def _submit_single_print_job(
    doctype: str,
    name: str,
    printer_setting: str,
    print_format: str | None = None,
    doc=None,
    no_letterhead: int = 0,
    job_type: str = "BILL",
    extra_metadata: dict | None = None,
    copies: int = 1,
    letterhead: str | None = None,
) -> tuple[dict, bool, object]:
    """Attempt one printer submission. Returns `(result_dict, permission_denied, target_doc)`."""
    try:
        import cups
    except ImportError:
        frappe.logger("printing").error("Failed to import cups library")
        return {"status": "Failure", "message": "Failed to import cups"}, False, doc

    target_doc = doc if doc is not None else frappe.get_doc(doctype, name)
    try:
        validate_print_permission(target_doc)
    except Exception as e:
        return {"status": "Failure", "message": f"Print permission denied: {e}"}, True, target_doc

    try:
        print_settings = frappe.get_doc("Network Printer Settings", printer_setting)
    except Exception as e:
        return (
            {
                "status": "Failure",
                "message": f"Network Printer Settings '{printer_setting}' not found: {e}",
            },
            False,
            target_doc,
        )

    try:
        cups.setServer(print_settings.server_ip)
        cups.setPort(print_settings.port)
        conn = cups.Connection()
    except Exception as e:
        frappe.logger("printing").error(
            f"Failed to connect to printer at {print_settings.server_ip}:{print_settings.port}: {e}"
        )
        return {"status": "Failure", "message": f"Failed to connect to printer: {e}"}, False, target_doc

    resolved_format = print_format
    resolved_letterhead = letterhead
    if doctype == "POS Invoice":
        from ury.ury.print_format.defaults import (
            resolve_safe_pos_print_format,
            validate_pos_invoice_print_format,
        )

        pos_profile = getattr(target_doc, "pos_profile", None) or frappe.db.get_value("POS Invoice", name, "pos_profile")
        if resolved_format:
            validate_pos_invoice_print_format(target_doc, resolved_format)
        else:
            if pos_profile:
                resolved_format = frappe.db.get_value("POS Profile", pos_profile, "print_format")
            default_meta_fmt = frappe.get_meta("POS Invoice").default_print_format if hasattr(frappe, "get_meta") else None
            resolved_format = resolve_safe_pos_print_format(resolved_format, default_format=default_meta_fmt)
        if not no_letterhead and not resolved_letterhead:
            doc_lh = getattr(target_doc, "letter_head", None)
            if isinstance(doc_lh, str) and doc_lh.strip():
                resolved_letterhead = doc_lh.strip()
            elif isinstance(pos_profile, str) and pos_profile.strip():
                prof_lh = frappe.db.get_value("POS Profile", pos_profile.strip(), "letter_head")
                if isinstance(prof_lh, str) and prof_lh.strip():
                    resolved_letterhead = prof_lh.strip()

    get_print_kwargs = dict(doc=doc, no_letterhead=no_letterhead, as_pdf=True)
    if doctype == "POS Invoice":
        # Guest-check formats inline their CSS and local logos; ignore unreachable printview CSS assets on worker boxes.
        get_print_kwargs["pdf_options"] = {
            "load-error-handling": "ignore",
            "load-media-error-handling": "ignore",
        }
    if resolved_letterhead:
        get_print_kwargs["letterhead"] = resolved_letterhead
    pdf_content = frappe.get_print(
        doctype,
        name,
        resolved_format,
        **get_print_kwargs,
    )

    file_path = os.path.join("/", "tmp", f"frappe-pdf-{frappe.generate_hash()}.pdf")
    copy_id = extra_metadata['print_job_id'] if job_type == 'BILL_COPY' else None
    handing_off = False
    try:
        with open(file_path, "wb") as f:
            f.write(pdf_content)

        cups_job_id = None
        print_count = max(1, int(copies or 1))
        for _ in range(print_count):
            handing_off = True
            cups_job_id = conn.printFile(print_settings.printer_name, file_path, copy_id or name, {})

        print_job_id = copy_id or _make_print_job_id(print_settings.printer_name, cups_job_id)

        # Determine owner: extra_metadata -> frappe.session.user -> target_doc.owner -> Administrator
        user_owner = (
            (extra_metadata.get("job_owner") or extra_metadata.get("owner") or extra_metadata.get("user"))
            if extra_metadata else None
        )
        if not user_owner:
            session_user = getattr(frappe, "session", None) and getattr(frappe.session, "user", None)
            if session_user and session_user != "Guest":
                user_owner = session_user
            else:
                user_owner = getattr(target_doc, "owner", None) or "Administrator"

        # Determine table: extra_metadata -> target_doc.restaurant_table -> target_doc.table
        table_name = (
            (extra_metadata.get("table") or extra_metadata.get("restaurant_table")) if extra_metadata else None
        ) or getattr(target_doc, "restaurant_table", None) or getattr(target_doc, "table", None)

        metadata = {
            "print_job_id": print_job_id,
            "cups_job_id": cups_job_id,
            "job_type": job_type,
            "reference_doctype": doctype,
            "reference_name": name,
            "printer": printer_setting,
            "printer_name": print_settings.printer_name,
            "server_ip": print_settings.server_ip,
            "port": print_settings.port,
            "file_path": file_path,
            "status": "SUBMITTED",
            "created_at": frappe.utils.now(),
            "job_owner": user_owner,
            "table": table_name,
            "restaurant_table": table_name,
        }

        if extra_metadata:
            metadata.update(extra_metadata)

        frappe.logger("printing").info(
            {
                "event": "print_job_submitted",
                "job_type": job_type,
                **metadata,
            }
        )

        # Check if tracking is disabled for the profile
        pos_profile = (
            (extra_metadata.get("pos_profile") if extra_metadata else None)
            or getattr(target_doc, "pos_profile", None)
            or (frappe.db.get_value("POS Invoice", name, "pos_profile") if doctype == "POS Invoice" else None)
        )
        if not copy_id and is_print_status_tracking_disabled(pos_profile):
            frappe.logger("printing").info(
                {
                    "event": "print_job_submitted_tracking_disabled",
                    "job_type": job_type,
                    "printer": printer_setting,
                    "pos_profile": pos_profile,
                    "cups_job_id": cups_job_id,
                }
            )
            return (
                {
                    "status": "Success",
                    "tracking_disabled": True,
                    "cups_job_id": cups_job_id,
                    "print_job_id": print_job_id,
                    "printer": printer_setting,
                    "job_type": job_type,
                    "reference_doctype": doctype,
                    "reference_name": name,
                    "file_path": file_path,
                },
                False,
                target_doc,
            )

        try:
            # File-store persistence and Redis monitor registration are handled
            # by register_print_job. The poller reads from the file store.
            registered = register_print_job(metadata)
            if copy_id and not registered:
                raise RuntimeError("Copy monitoring could not be registered")

            frappe.enqueue(
                "ury.ury.printing.print_job_poller.poll_single_print_job",
                print_job_id=print_job_id,
                queue="default",
                timeout=60,
                now=frappe.flags.in_test and not copy_id,
                enqueue_after_commit=bool(copy_id),
            )
        except Exception:
            if copy_id:
                raise
            frappe.logger("printing").warning(
                {"event": "print_job_register_failed", "print_job_id": print_job_id},
                exc_info=True,
            )

        return (
            {
                "status": "Success",
                "cups_job_id": cups_job_id,
                "print_job_id": print_job_id,
                "printer": printer_setting,
                "job_type": job_type,
                "reference_doctype": doctype,
                "reference_name": name,
                "file_path": file_path,
            },
            False,
            target_doc,
        )
    except Exception as e:
        frappe.logger("printing").error(f"Failed to submit print job: {e}", exc_info=True)
        return (
            {"status": "Unknown" if copy_id and handing_off else "Failure", "message": str(e)},
            False,
            target_doc,
        )


def submit_and_monitor_print_job(
    doctype: str,
    name: str,
    printer_setting: str,
    print_format: str | None = None,
    doc=None,
    no_letterhead: int = 0,
    job_type: str = "BILL",
    extra_metadata: dict | None = None,
    copies: int = 1,
    letterhead: str | None = None,
    backup_printer: str | None = None,
    allow_backup_fallback: bool = True,
) -> dict:
    """Submit a document to CUPS and register a Print Job for monitoring.

    If the primary printer fails (for a non-permission error) and a backup printer
    is configured on `Network Printer Settings.custom_backup_printer` or
    `URY Production Unit.custom_backup_printer`, automatically retries once on
    the backup printer and records `[KOT-FAILOVER-PRINTED] <backup>` on KOT docs.
    """
    result, permission_denied, target_doc = _submit_single_print_job(
        doctype=doctype,
        name=name,
        printer_setting=printer_setting,
        print_format=print_format,
        doc=doc,
        no_letterhead=no_letterhead,
        job_type=job_type,
        extra_metadata=extra_metadata,
        copies=copies,
        letterhead=letterhead,
    )

    if (
        result.get("status") == "Failure"
        and not permission_denied
        and allow_backup_fallback
    ):
        fallback = _resolve_backup_printer_setting(
            printer_setting, explicit_backup=backup_printer, target_doc=target_doc
        )
        if fallback:
            fallback_metadata = dict(extra_metadata or {})
            fallback_metadata["primary_printer"] = printer_setting
            fallback_metadata["failover_used"] = True
            fallback_result, _perm, _doc = _submit_single_print_job(
                doctype=doctype,
                name=name,
                printer_setting=fallback,
                print_format=print_format,
                doc=doc,
                no_letterhead=no_letterhead,
                job_type=job_type,
                extra_metadata=fallback_metadata,
                copies=copies,
                letterhead=letterhead,
            )
            if fallback_result.get("status") == "Success":
                fallback_result["failover_used"] = True
                fallback_result["primary_printer"] = printer_setting
                fallback_result["backup_printer"] = fallback
                if doctype == "URY KOT":
                    _record_kot_failover_comment(target_doc, fallback)
                return fallback_result
            return {
                "status": "Failure",
                "primary_printer": printer_setting,
                "backup_printer": fallback,
                "failover_attempted": True,
                "message": (
                    f"Primary printer '{printer_setting}' failed ({result.get('message')}); "
                    f"backup printer '{fallback}' also failed ({fallback_result.get('message')})."
                ),
            }

    return result
