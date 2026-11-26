"""Current party's checks on a restaurant table. NULL means the roster predates tracking."""
import json
from datetime import datetime, timedelta

from ury.ury.api.kitchen_ticket_history import CANCELLATIONS, current_ticket_owner


def _legacy_seating_start(clock, drafts):
    from frappe.utils import get_time, now_datetime

    if clock is None:
        return min((draft.creation for draft in drafts), default=None)
    # The legacy column stores only a TIME. A matching draft supplies the date
    # even for long-running seatings; otherwise use the most recent occurrence.
    time = get_time(clock)
    matches = [draft.creation for draft in drafts if draft.creation.time() == time]
    if matches:
        return max(matches)
    now = now_datetime()
    start = datetime.combine(now.date(), time)
    return start - timedelta(days=1) if start > now else start


def _legacy_checks(frappe, table):
    """Recover drafts, seated reservation bindings and effective owners of kitchen work at this table."""
    from ury.ury.api.table_reservation import open_checks_on

    drafts = open_checks_on([table.name])
    checks = {row.name for row in drafts}
    reservations = frappe.get_all(
        "URY Table Reservation",
        filters={"reserved_table": table.name, "pos_invoice": ("is", "set")},
        fields=["status", "pos_invoice"],
    )
    seated = {row.pos_invoice for row in reservations if row.status == "Seated"}
    departed = {row.pos_invoice for row in reservations if row.status in ("Completed", "Cancelled")}
    tickets = frappe.get_all(
        "URY KOT",
        filters={"restaurant_table": table.name, "docstatus": 1, "type": ("not in", CANCELLATIONS)},
        fields=["name", "invoice"],
    )
    ledgers = {}
    owners = {current_ticket_owner(frappe, row.name, row.invoice, ledgers=ledgers).name for row in tickets}
    owners.discard(None)
    if owners:
        paid = frappe.get_all(
            "POS Invoice",
            filters={"name": ("in", sorted(owners)), "docstatus": 1, "status": "Paid",
                     "restaurant_table": table.name},
            fields=["name", "creation", "custom_split_from", "custom_split_group"],
        )
        # The dashboard's table projection omits the clock. Fetch it only for
        # legacy recovery; normal seating membership never depends on a timestamp.
        clock = table.get("latest_invoice_time")
        if clock is None:
            clock = frappe.db.get_value("URY Table", table.name, "latest_invoice_time")
        start = _legacy_seating_start(clock, drafts)
        roots = {draft.custom_split_from or draft.name for draft in drafts}
        groups = {draft.custom_split_group for draft in drafts if draft.custom_split_group}
        for check in paid:
            if check.name in departed:
                continue
            same_bill = (check.custom_split_from or check.name) in roots or check.custom_split_group in groups
            if start is None or same_bill or (check.creation and check.creation >= start):
                checks.add(check.name)
    return checks | seated


def table_checks(frappe, table):
    try:
        names = json.loads(table.seating_checks)
    except (ValueError, TypeError, RecursionError):
        names = None
    if isinstance(names, list) and all(isinstance(name, str) and name.strip() for name in names):
        return {name.strip() for name in names}
    return _legacy_checks(frappe, table) if table.occupied else set()


def update_members(frappe, table, *, add=(), remove=()):
    if not table:
        return
    from ury.ury.api.table_reservation import current_floor_rows

    rows = current_floor_rows(
        "URY Table", {"name": table}, fields=["name", "occupied", "seating_checks", "latest_invoice_time"],
        for_update=True,
    )
    for row in rows:
        members = table_checks(frappe, row)
        updated = (members - set(remove)) | set(add)
        encoded = json.dumps(sorted(updated))
        if row.seating_checks != encoded:
            frappe.db.set_value("URY Table", table, "seating_checks", encoded,
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
