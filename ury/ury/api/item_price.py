"""Item Price determination shared by order pricing and menu price upkeep.

SAP basis: price determination uses the condition record valid on the pricing
date, and a condition record is used only once released.
"""

from datetime import date

import frappe
from frappe import _


def item_price_release_states():
    """Return the Item Price workflow state field and its released states.

    Returns (None, None) when the site has no active Item Price workflow, so
    every Item Price is released. A released state is one whose Workflow State
    style is Success (Frappe's style for Approved). An active workflow with no
    such state refuses pricing instead of billing an unreleased price.
    """
    workflow_filters = {"document_type": "Item Price", "is_active": 1}
    workflow = frappe.db.get_value("Workflow", workflow_filters, "name")
    if not workflow:
        return None, None
    field = frappe.db.get_value("Workflow", workflow_filters, "workflow_state_field")
    states = [row.state for row in frappe.get_cached_doc("Workflow", workflow).states]
    released = set(frappe.get_all(
        "Workflow State", filters={"name": ["in", states], "style": "Success"}, pluck="name",
    ))
    if not released:
        frappe.throw(_("Item Price workflow {0} has no released (Success) state, so no menu price can be billed.").format(workflow))
    return field, released


def price_valid_on(item_code, price_list, posting_date, release_field, released):
    """The Item Price valid and released on posting_date; the latest valid_from wins."""
    fields = ["price_list_rate", "valid_from", "valid_upto"]
    if release_field:
        fields.append(release_field)
    rows = frappe.db.get_list(
        "Item Price",
        filters={"item_code": item_code, "price_list": price_list},
        fields=fields,
        order_by="creation asc",
    )
    getdate = frappe.utils.getdate
    candidates = []
    for position, row in enumerate(rows):
        valid_from = getattr(row, "valid_from", None)
        valid_upto = getattr(row, "valid_upto", None)
        if valid_from and getdate(valid_from) > posting_date:
            continue
        if valid_upto and getdate(valid_upto) < posting_date:
            continue
        if release_field and getattr(row, release_field, None) not in released:
            continue
        candidates.append(((getdate(valid_from) if valid_from else date.min), position, row))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[:2])[2]
