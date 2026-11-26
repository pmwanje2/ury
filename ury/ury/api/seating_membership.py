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


def _version_changes(raw):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return
    changes = data.get("changed") if isinstance(data, dict) else None
    if isinstance(changes, list):
        yield from (edit for edit in changes if isinstance(edit, list) and len(edit) == 3)


def _occupancy_boundary(frappe, table):
    """Find the latest recorded release or seating, even on sites without roster Versions."""
    dirty_since = frappe.db.get_value("URY Table", table, "custom_dirty_since")
    filters = {"ref_doctype": "URY Table", "docname": table}
    if dirty_since:
        filters["creation"] = (">=", dirty_since)

    offset = 0
    while True:
        rows = frappe.get_all(
            "Version", filters=filters,
            or_filters=[["data", "like", '%"occupied"%'], ["data", "like", '%"custom_dirty_since"%']],
            fields=["creation", "data"], order_by="creation desc, name desc", start=offset, page_length=32,
        )
        for row in rows:
            for field, before, after in _version_changes(row.data):
                if field == "occupied":
                    vacant = (0, "0", None, "")
                    seated = (1, "1")
                    if (before in vacant and after in seated) or (before in seated and after in vacant):
                        data = json.loads(row.data)
                        if after in seated and data.get("continuing_visit") is True:
                            continue
                        reference = data.get("updater_reference")
                        opener = (reference.get("docname") if after in seated and isinstance(reference, dict)
                                  and reference.get("doctype") == "POS Invoice" else None)
                        return row.creation, opener
                if field == "custom_dirty_since" and not before and after:
                    return row.creation, None
        if len(rows) < 32:
            return dirty_since, None
        offset += len(rows)


def _transfer_arrivals(frappe, names, table, since=None):
    """Arrival is the recorded change of table, independent of the invoice's opening time."""
    if not names:
        return {}
    filters = {"ref_doctype": "POS Invoice", "docname": ("in", names), "data": ("like", '%"restaurant_table"%')}
    if since is not None:
        filters["creation"] = (">=", since)
    rows = frappe.get_all(
        "Version", filters=filters,
        fields=["docname", "creation", "data"], order_by="creation asc",
    )
    arrivals = {}
    for row in rows:
        if any(field == "restaurant_table" and after == table for field, _, after in _version_changes(row.data)):
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


def _split_family(frappe, checks, seeds):
    # Walk invoice relationships independently of table assignment. An off-table
    # parent can connect two on-table generations without owning a place here.
    eligible = {row.name for row in checks}
    frontier = [row for row in checks if row.name in seeds]
    seen = set(seeds)
    while frontier:
        parents = {row.custom_split_from for row in frontier if row.custom_split_from}
        groups = {row.custom_split_group for row in frontier if row.custom_split_group}
        relatives = {"custom_split_from": ("in", sorted(parents | {row.name for row in frontier}))}
        if parents:
            relatives["name"] = ("in", sorted(parents))
        if groups:
            relatives["custom_split_group"] = ("in", sorted(groups))
        rows = frappe.get_all("POS Invoice", filters={"docstatus": ("in", [0, 1])}, or_filters=relatives,
                              fields=["name", "custom_split_from", "custom_split_group"])
        frontier = [row for row in rows if row.name not in seen]
        seen.update(row.name for row in frontier)
    return seeds | (eligible & seen)


def _last_join(frappe, table, anchors, since):
    # A younger walk-in can postdate every transfer. A recorded roster
    # expansion proves which checks were already here when that check joined.
    changes = frappe.get_all(
        "Version", filters={"ref_doctype": "URY Table", "docname": table,
                            "creation": (">=", since), "data": ("like", '%"seating_checks"%')},
        pluck="data", order_by="creation desc, name desc", limit=1,
    )
    if changes:
        try:
            for field, _, after in json.loads(changes[0])["changed"]:
                if field == "seating_checks":
                    joined = _roster_names(after)
                    if joined and anchors & joined:
                        return joined
        except (KeyError, TypeError, ValueError, RecursionError):
            pass
    return set()


def _legacy_checks(frappe, table):
    """Recover drafts, seated reservation bindings and effective owners of kitchen work at this table."""
    from ury.ury.api.table_reservation import open_checks_on

    drafts = open_checks_on([table.name])
    checks = {row.name for row in drafts}
    reservations = frappe.get_all(
        "URY Table Reservation",
        filters={"reserved_table": table.name, "pos_invoice": ("is", "set")},
        fields=["status", "pos_invoice", "modified"],
    )
    seated = {row.pos_invoice for row in reservations if row.status == "Seated"}
    departed = {row.pos_invoice for row in reservations if row.status in ("Completed", "Cancelled")}
    paid = frappe.get_all(
        "POS Invoice", filters={"docstatus": 1, "status": "Paid"},
        or_filters={"restaurant_table": table.name, "custom_merged_tables": ("like", f"%{table.name}%")},
        fields=["name", "creation", "modified", "restaurant_table", "custom_merged_tables", "custom_split_from",
                "custom_split_group", "invoice_printed", "custom_printing_time"],
    )
    on_table = [row for row in paid if table.name in invoice_tables(row)]
    paid = [row for row in on_table if row.name not in departed]
    boundary, opener = _occupancy_boundary(frappe, table.name)
    if boundary is not None:
        # A known occupancy episode takes precedence over scheduled reservations,
        # split relationships and the opening time of the latest check.
        candidates = [*drafts, *paid]
        arrivals = _transfer_arrivals(frappe, [row.name for row in candidates], table.name, boundary)
        return checks | {
            row.name for row in paid
            if row.name == opener or row.name in arrivals or (row.creation and row.creation >= boundary)
        }

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
        # The clock can identify a younger check, or the original opening
        # time of a check that arrived at this table much later.
        clock_checks = {row.name for row in [*drafts, *paid] if row.creation and
                        row.creation.time() == start.time() and row.creation <= start}
        # A booking can precede the previous party's release. A seated
        # reservation's last update is evidence of activity on this visit.
        visits = [row.modified for row in reservations if row.status == "Seated" and row.modified]
        transfers = _transfer_arrivals(frappe, sorted(checks | {row.name for row in paid}), table.name)
        anchors = [transfers[name] for name in clock_checks if name in transfers]
        if anchors:
            start = max(anchors)
        printed = {row.name: row.custom_printing_time or row.modified for row in on_table if row.invoice_printed}
        prior_prints = {name: when for name, when in printed.items() if when and when < start}
        if prior_prints:
            # Some legacy print adapters only set invoice_printed. Settlement's
            # modified time supplies the last known close if no print time exists.
            release = max(prior_prints.values())
            return checks | {
                row.name for row in paid if row.name not in prior_prints
                and (transfers.get(row.name, row.creation) > release or
                     (not row.invoice_printed and row.modified >= start))
            }
        window = min([start, *visits])
        arrivals = {name: when for name, when in transfers.items() if when >= window}
        checks.update(row.name for row in paid if not row.invoice_printed and
                      row.name in transfers and row.modified >= start)
        checks.update(row.name for row in paid if row.creation and row.creation >= start)

        # A directly restored occupied flag can coexist with the last party's
        # released state. Only infer additional arrivals for a seated visit.
        state = frappe.db.get_value("URY Table", table.name, "custom_table_state")
        if seated or state in ("Seated", "Check Printed"):
            checks.update(name for name, arrival in arrivals.items() if arrival >= window)
        checks.update(name for name in clock_checks if arrivals.get(name) == start)
        companions = _last_join(frappe, table.name, clock_checks | seated, start)
        checks.update(row.name for row in paid if row.name in companions)

    return _split_family(frappe, [*drafts, *paid], checks | seated)


def _roster_names(raw):
    try:
        names = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if isinstance(names, list) and all(isinstance(name, str) and name.strip() for name in names):
        return {name.strip() for name in names}
    return None


def table_checks(frappe, table):
    names = _roster_names(table.seating_checks)
    if names is not None:
        return names
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
            if _roster_names(row.seating_checks) and updated - members:
                frappe.get_doc({
                    "doctype": "Version", "ref_doctype": "URY Table", "docname": table,
                    "data": json.dumps({"changed": [["seating_checks", row.seating_checks, encoded]]}),
                }).insert(ignore_permissions=True)
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
