"""Current party's checks on a restaurant table. NULL means the roster predates tracking."""
import json
from collections import defaultdict
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


def _transfer_arrivals(frappe, names, table, since):
    """Arrival is the recorded change of table, independent of the invoice's opening time."""
    if not names:
        return {}
    rows = frappe.get_all(
        "Version", filters={"ref_doctype": "POS Invoice", "docname": ("in", names),
                            "creation": (">=", since), "data": ("like", '%"restaurant_table"%')},
        fields=["docname", "creation", "data"], order_by="creation asc",
    )
    arrivals = {}
    for row in rows:
        try:
            data = json.loads(row.data)
        except (TypeError, ValueError, RecursionError):
            continue
        changes = data.get("changed") if isinstance(data, dict) else None
        if isinstance(changes, list) and any(
            isinstance(edit, list) and len(edit) == 3 and edit[0] == "restaurant_table" and edit[2] == table
            for edit in changes
        ):
            arrivals[row.docname] = row.creation
    return arrivals


def record_table_transfer(frappe, invoice, source, destination):
    # save() normally writes this Version. Direct floor updates can use db_set(),
    # which skips versioning. Keep the same evidence in the transfer transaction.
    saved = frappe.db.get_all("POS Invoice", filters={"name": invoice.name, "restaurant_table": destination},
                              pluck="modified")
    if saved and not _transfer_arrivals(frappe, [invoice.name], destination, saved[0]):
        frappe.get_doc({
            "doctype": "Version", "ref_doctype": "POS Invoice", "docname": invoice.name,
            "data": json.dumps({"changed": [["restaurant_table", source, destination]]}),
        }).insert(ignore_permissions=True)


def _split_family(checks, seeds):
    # A parent can be off-table. Use it as a relationship key, but only add
    # actual on-table records to the roster. Keep group and parent keys distinct.
    links = defaultdict(set)
    for check in checks:
        keys = [("parent", check.name), ("parent", check.custom_split_from), ("group", check.custom_split_group)]
        for key in keys:
            if key[1]:
                links[key].add(check.name)
                links[check.name].add(key)
    visited = set(seeds)
    pending = list(visited)
    while pending:
        neighbors = links[pending.pop()] - visited
        visited.update(neighbors)
        pending.extend(neighbors)
    return {name for name in visited if isinstance(name, str)}


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
    paid = frappe.get_all(
        "POS Invoice", filters={"docstatus": 1, "status": "Paid", "restaurant_table": table.name},
        fields=["name", "creation", "custom_split_from", "custom_split_group"],
    )
    paid = [row for row in paid if row.name not in departed]
    if not paid:
        return checks | seated

    clock = table.get("latest_invoice_time")
    if clock is None:
        clock = frappe.db.get_value("URY Table", table.name, "latest_invoice_time")
    start = _legacy_seating_start(clock, drafts)
    if start is None:
        tickets = frappe.get_all(
            "URY KOT",
            filters={"restaurant_table": table.name, "docstatus": 1, "type": ("not in", CANCELLATIONS)},
            fields=["name", "invoice"],
        )
        ledgers = {}
        owners = {current_ticket_owner(frappe, row.name, row.invoice, ledgers=ledgers).name for row in tickets}
        checks.update(row.name for row in paid if row.name in owners)
    else:
        # The floor clock identifies the check that opened the seating. For
        # transferred checks the seating began when it arrived at this table.
        clock_checks = [row.name for row in [*drafts, *paid] if row.creation and
                        row.creation.time() == start.time() and row.creation <= start]
        arrivals = _transfer_arrivals(frappe, clock_checks, table.name, start)
        if arrivals:
            anchor = max(arrivals, key=arrivals.get)
            checks.add(anchor)
            start = arrivals[anchor]
        checks.update(row.name for row in paid if row.creation and row.creation >= start)

    return _split_family([*drafts, *paid], checks | seated)


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
