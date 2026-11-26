import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from ury.brand import coded_message
from ury.ury.api.seating_membership import record_table_transfer, update_members

ON_SETTLEMENT = "On Settlement (Hospitality Default)"
_POLICY_FIELDS = [
    "custom_table_release_trigger", "custom_enable_bussing_state", "custom_block_dirty_table_seating",
]


def table_policy(branch):
    if not branch or not callable(getattr(frappe.db, "has_column", None)):
        return {}
    if frappe.db.has_column("Branch", _POLICY_FIELDS[0]) not in (True, 1):
        return {}
    return frappe.db.get_value("Branch", branch, _POLICY_FIELDS, as_dict=True) or {}


def holds_until_settlement(branch):
    return table_policy(branch).get(_POLICY_FIELDS[0]) == ON_SETTLEMENT


def assert_outlet_access(branch):
    from ury.ury_pos.api import getBranch

    if not branch or frappe.session.user == "Guest":
        frappe.throw(_("Not permitted to manage this outlet."), frappe.PermissionError)
    if frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles():
        return
    try:
        active_branch = getBranch()
    except frappe.ValidationError:
        active_branch = None
    if active_branch != branch:
        frappe.throw(_("Not permitted to manage this outlet."), frappe.PermissionError)


def dirty_table_error():
    frappe.throw(coded_message("TABLE-DIRTY-NEEDS-BUSSING", _("Mark this table clean before seating.")))


def assert_table_seatable(table, branch):
    if table and cint(table_policy(branch).get("custom_block_dirty_table_seating")):
        if frappe.db.get_value("URY Table", table, "custom_table_state", for_update=True) == "Dirty":
            dirty_table_error()


def set_table_state(table, state, *, since=None, clear_cluster=False, check=None, continuing_visit=False):
    locked = frappe.db.sql(
        "SELECT occupied FROM `tabURY Table` WHERE name = %s FOR UPDATE", (table,), pluck=True
    )
    occupied = state in ("Seated", "Check Printed")
    values = {"custom_table_state": state, "occupied": int(occupied)}
    if occupied and check:
        update_members(frappe, table, add=[check])
    if not occupied:
        values["seating_checks"] = "[]"
        values["latest_invoice_time"] = None
    elif since:
        # Invoice creation may be a datetime or an ISO timestamp; the floor clock is TIME(6).
        values["latest_invoice_time"] = str(since).replace("T", " ").rsplit(" ", 1)[-1]
    if state == "Dirty":
        previous = frappe.db.get_value("URY Table", table, "custom_dirty_since")
        values["custom_dirty_since"] = previous or now_datetime()
    else:
        values["custom_dirty_since"] = None
    if clear_cluster:
        values["merged_with"] = None
    frappe.db.set_value("URY Table", table, values, update_modified=False)
    if locked == [int(not occupied)]:
        diff = {"changed": [["occupied", int(not occupied), int(occupied)]]}
        if occupied and continuing_visit:
            # A transfer off this table found checks or a seated reservation still here.
            diff["continuing_visit"] = True
        if occupied and since and not check:
            # Normal order sync supplies the opening invoice's creation time.
            matches = [row.name for row in open_checks_on([table]) if row.creation == get_datetime(since)]
            if len(matches) == 1:
                check = matches[0]
        if occupied and check:
            # The invoice transfer receipt can precede the table write in the same transaction.
            diff["updater_reference"] = {"doctype": "POS Invoice", "docname": check}
        frappe.get_doc({
            "doctype": "Version", "ref_doctype": "URY Table", "docname": table,
            "data": frappe.as_json(diff),
        }).insert(ignore_permissions=True)


def open_checks_on(tables):
    from ury.ury.api.table_reservation import open_checks_on as find_checks
    return find_checks(tables)


def update_table_after_check(tables, branch, *, printed=False, exclude_invoice=None):
    """Return whether the outlet's configured lifecycle handled this event."""
    policy = table_policy(branch)
    hold = policy.get("custom_table_release_trigger") == ON_SETTLEMENT
    bussing = bool(cint(policy.get("custom_enable_bussing_state")))
    if not tables or (printed and not hold) or not (hold or bussing):
        return False
    for table in sorted(set(tables)):
        frappe.db.get_value("URY Table", table, "name", for_update=True)
    checks = [row for row in open_checks_on(tables) if not exclude_invoice or row.name != exclude_invoice]
    if checks:
        state = "Check Printed" if hold and any(cint(row.invoice_printed) for row in checks) else "Seated"
    elif printed:
        return True
    else:
        state = "Dirty" if bussing else "Available"
    for table in tables:
        if checks:
            set_table_state(table, state)
        else:
            # Clear split/merge links only once the last check closes.
            # Dirty Since is retained when another hook repeats the transition.
            set_table_state(table, state, **({"clear_cluster": True} if len(tables) > 1 else {}))
    return True


def update_tables_after_transfer(source, destination, invoice):
    from ury.ury.api.table_reservation import current_floor_rows, open_checks_on as find_checks

    policy = table_policy(invoice.branch)
    hold = policy.get("custom_table_release_trigger") == ON_SETTLEMENT
    state = "Check Printed" if hold and cint(invoice.get("invoice_printed")) else "Seated"
    record_table_transfer(frappe, invoice, source, destination)
    set_table_state(destination, state, since=invoice.creation, check=invoice.name)
    update_members(frappe, source, remove=[invoice.name])

    siblings = find_checks([source], for_update=True)
    if siblings:
        state = "Check Printed" if hold and any(cint(row.get("invoice_printed")) for row in siblings) else "Seated"
    elif current_floor_rows("URY Table Reservation",
            {"reserved_table": source, "status": "Seated"}, fields=["name"], for_update=True):
        state = "Seated"
    else:
        state = "Dirty" if cint(policy.get("custom_enable_bussing_state")) else "Available"
    set_table_state(source, state, continuing_visit=True)


def on_check_closed(doc, method=None):
    table = doc.get("restaurant_table")
    if table:
        from ury.ury.doctype.ury_order.ury_order import _get_table_group

        return update_table_after_check(
            _get_table_group(table, doc.get("custom_merged_tables")), doc.get("branch"),
            exclude_invoice=doc.name,
        )


@frappe.whitelist(methods=["POST"])
def mark_table_clean(table):
    branch = frappe.db.get_value("URY Table", table, "branch")
    assert_outlet_access(branch)
    doc = frappe.get_doc("URY Table", table, for_update=True)
    if doc.branch != branch:
        frappe.throw(_("Not permitted to manage this outlet."), frappe.PermissionError)
    doc.check_permission("write")
    if doc.custom_table_state != "Dirty" or doc.occupied or open_checks_on([table]):
        frappe.throw(_("Only an empty, dirty table can be marked clean."))
    set_table_state(table, "Available")
    return {"name": table, "custom_table_state": "Available"}
