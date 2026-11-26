"""Current party's checks on a restaurant table. NULL means the roster predates tracking."""
import json

from ury.ury.api.kitchen_ticket_history import CANCELLATIONS, current_ticket_owner


def _legacy_checks(frappe, table):
    """Recover drafts, seated reservation bindings and effective owners of kitchen work at this table."""
    from ury.ury.api.table_reservation import open_checks_on

    checks = {row.name for row in open_checks_on([table])}
    reservations = frappe.get_all(
        "URY Table Reservation",
        filters={"reserved_table": table, "pos_invoice": ("is", "set")},
        fields=["status", "pos_invoice"],
    )
    seated = {row.pos_invoice for row in reservations if row.status == "Seated"}
    departed = {row.pos_invoice for row in reservations if row.status in ("Completed", "Cancelled")}
    tickets = frappe.get_all(
        "URY KOT",
        filters={"restaurant_table": table, "docstatus": 1, "type": ("not in", CANCELLATIONS)},
        fields=["name", "invoice"],
    )
    ledgers = {}
    owners = {current_ticket_owner(frappe, row.name, row.invoice, ledgers=ledgers).name for row in tickets}
    owners.discard(None)
    if owners:
        paid = frappe.get_all(
            "POS Invoice",
            filters={"name": ("in", sorted(owners)), "docstatus": 1, "status": "Paid",
                     "restaurant_table": table},
            pluck="name",
        )
        checks.update(set(paid) - departed)
    return checks | seated


def table_checks(frappe, table):
    if table.seating_checks:
        return set(json.loads(table.seating_checks))
    return _legacy_checks(frappe, table.name) if table.occupied else set()


def update_members(frappe, table, *, add=(), remove=()):
    if not table:
        return
    from ury.ury.api.table_reservation import current_floor_rows

    rows = current_floor_rows("URY Table", {"name": table}, fields=["name", "occupied", "seating_checks"],
                              for_update=True)
    for row in rows:
        members = table_checks(frappe, row)
        updated = (members - set(remove)) | set(add)
        if updated != members or not row.seating_checks:
            frappe.db.set_value("URY Table", table, "seating_checks", json.dumps(sorted(updated)),
                                update_modified=False)


def invoice_tables(doc):
    if not doc or not doc.get("restaurant_table"):
        return set()
    merged = (doc.get("custom_merged_tables") or "").split(",")
    return {doc.restaurant_table} | {table.strip() for table in merged if table.strip()}


def invoice_updated(doc, method=None):
    import frappe

    if method in ("on_cancel", "on_discard"):
        before, after = invoice_tables(doc), set()
    elif doc.docstatus == 0:
        before, after = invoice_tables(doc.get_doc_before_save()), invoice_tables(doc)
    else:
        return
    for table in sorted(before | after):
        if table not in after:
            update_members(frappe, table, remove=[doc.name])
        elif table not in before or not doc.get("invoice_printed"):
            update_members(frappe, table, add=[doc.name])
