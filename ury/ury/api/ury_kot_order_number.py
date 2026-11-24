import re

import frappe


def _invoice_sequence_number(value, amended_from=None):
    """Extract the trailing numeric sequence from an invoice name, stripping
    Frappe amendment suffixes such as '-1' or '-2' without breaking padded
    naming series like 'POS-INV-2026-00105'."""
    text = str(value or "").strip()
    if not text:
        return None
    if isinstance(amended_from, str) and amended_from.strip():
        text = re.sub(r"-\d+$", "", text)
    elif re.search(r"\d{3,}-[1-9]\d{0,2}$", text):
        text = re.sub(r"-[1-9]\d{0,2}$", "", text)
    match = re.search(r"(\d+)$", text)
    if not match:
        return None
    return int(match.group(1)[-5:])


def set_order_number(doc, event):
    pos_profile = doc.pos_profile
    if doc.order_type == "Aggregators":
        last_invoice = frappe.get_value(
            "POS Opening Entry",
            {"pos_profile": pos_profile, "status": "Open"},
            "custom_ury_last_aggregator_invoice",
        )
    else:
        last_invoice = frappe.get_value(
            "POS Opening Entry",
            {"pos_profile": pos_profile, "status": "Open"},
            "custom_ury_last_invoice",
        )
    if last_invoice:
        last_invoice_number = _invoice_sequence_number(last_invoice)
        current_invoice_number = _invoice_sequence_number(doc.name, getattr(doc, "amended_from", None))
        if last_invoice_number is None or current_invoice_number is None:
            return

        order_number = current_invoice_number - last_invoice_number
        if order_number > 0:
            if doc.order_type == "Aggregators":
                order_number = "AGR - " + str(order_number)
            frappe.db.set_value(
                "POS Invoice",
                doc.name,
                "custom_ury_order_number",
                order_number,
                update_modified=False,
            )
        else:
            frappe.db.set_value(
                "POS Invoice",
                doc.name,
                "custom_ury_order_number",
                current_invoice_number,
                update_modified=False,
            )
    else:
        pos_open_name = frappe.get_value(
            "POS Opening Entry",
            {"pos_profile": pos_profile, "status": "Open"},
            "name",
        )

        if doc.order_type == "Aggregators":
            aggregator_invoice = frappe.get_last_doc(
                "POS Invoice", filters={"pos_profile": doc.pos_profile, "order_type": "Aggregators"}
            )
            aggregator_invoice_number = _invoice_sequence_number(
                aggregator_invoice.name, getattr(aggregator_invoice, "amended_from", None)
            )
            if aggregator_invoice_number is not None and pos_open_name:
                aggregator_last_order_number = aggregator_invoice_number - 1
                frappe.db.set_value(
                    "POS Opening Entry",
                    pos_open_name,
                    "custom_ury_last_aggregator_invoice",
                    aggregator_last_order_number,
                )
        else:
            invoice = frappe.get_last_doc(
                "POS Invoice", filters={"pos_profile": doc.pos_profile, "order_type": ["!=", "Aggregators"]}
            )

            invoice_number = _invoice_sequence_number(
                invoice.name, getattr(invoice, "amended_from", None)
            )
            if invoice_number is not None and pos_open_name:
                last_order_number = invoice_number - 1
                frappe.db.set_value(
                    "POS Opening Entry", pos_open_name, "custom_ury_last_invoice", last_order_number
                )

        default_value = "AGR - 1" if doc.order_type == "Aggregators" else "1"
        frappe.db.set_value(
            "POS Invoice",
            doc.name,
            "custom_ury_order_number",
            default_value,
            update_modified=False,
        )


def set_last_invoice_in_pos_open(doc, event):
    try:
        invoice = frappe.get_last_doc(
            "POS Invoice", filters={"pos_profile": doc.pos_profile, "order_type": ["!=", "Aggregators"]}
        )
        doc.custom_ury_last_invoice = invoice.name
    except Exception:
        pass
    try:
        aggregator_invoice = frappe.get_last_doc(
            "POS Invoice", filters={"pos_profile": doc.pos_profile, "order_type": "Aggregators"}
        )
        doc.custom_ury_last_aggregator_invoice = aggregator_invoice.name
    except Exception:
        pass
