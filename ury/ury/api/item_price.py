"""Item Price determination shared by order pricing, menu price upkeep and every price shown before billing.

SAP basis: price determination uses the condition record valid on the pricing
date, and a condition record is used only once released. Candidate filtering
and priority follow ERPNext get_item_price (erpnext/stock/get_item_details.py,
_get_item_price_query and _order_item_prices): the UOM, the party, no batch, the priority.
"""

from datetime import date

import frappe
from frappe import _

_WORKFLOW = {"document_type": "Item Price", "is_active": 1}


def _states_styled(workflow, style):
    states = [row.state for row in frappe.get_cached_doc("Workflow", workflow).states]
    return set(frappe.get_all("Workflow State", filters={"name": ["in", states], "style": style}, pluck="name"))


def item_price_release_states():
    """Return the Item Price workflow state field and its released states.

    Returns (None, None) when the site has no active Item Price workflow, so
    every Item Price is released. A released state is one whose Workflow State
    style is Success (Frappe's style for Approved). An active workflow with no
    such state refuses pricing instead of billing an unreleased price.
    """
    workflow = frappe.db.get_value("Workflow", _WORKFLOW, "name")
    if not workflow:
        return None, None
    field = frappe.db.get_value("Workflow", _WORKFLOW, "workflow_state_field")
    released = _states_styled(workflow, "Success")
    if not released:
        frappe.throw(_("Item Price workflow {0} has no released (Success) state, so no menu price can be billed.").format(workflow))
    return field, released


def item_price_rejected_states():
    """Item Price workflow states styled Danger (Frappe's style for Rejected); empty without an active workflow."""
    workflow = frappe.db.get_value("Workflow", _WORKFLOW, "name")
    return _states_styled(workflow, "Danger") if workflow else set()


def selling_uom(item_code):
    """ERPNext's selling UOM default (get_item_details: the Item's sales UOM, else its stock UOM)."""
    return frappe.db.get_value("Item", item_code, "sales_uom") or frappe.db.get_value("Item", item_code, "stock_uom")


def price_valid_on(item_code, price_list, posting_date, release_field, released, customer=None, uom=None):
    """The Item Price ERPNext would price with on posting_date, released by the workflow if one is active.

    Candidates, as in ERPNext: UOM empty or uom, party-less or this customer's (never a supplier's),
    no batch (URY lines carry none), valid on posting_date. Priority, as in ERPNext: this customer's
    row, then the latest valid_from, then the latest created.
    """
    fields = ["price_list_rate", "valid_from", "valid_upto", "uom", "customer", "supplier", "batch_no"]
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
        row_customer = getattr(row, "customer", None)
        if (getattr(row, "uom", None) or "") not in ("", uom):
            continue
        if getattr(row, "supplier", None) or getattr(row, "batch_no", None):
            continue
        if row_customer and row_customer != customer:
            continue
        if valid_from and getdate(valid_from) > posting_date:
            continue
        if valid_upto and getdate(valid_upto) < posting_date:
            continue
        if release_field and getattr(row, release_field, None) not in released:
            continue
        candidates.append(((bool(row_customer), getdate(valid_from) if valid_from else date.min, position), row))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]
