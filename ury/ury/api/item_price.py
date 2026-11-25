"""Item Price determination shared by order pricing, menu price upkeep and every price shown before billing.

SAP basis: price determination uses the condition record valid on the pricing
date, and a condition record is used only once released. Candidate filtering
and priority follow ERPNext get_item_price (erpnext 16.53.0,
erpnext/stock/get_item_details.py: _get_item_price_query, _order_item_prices
and the stock-UOM retry in get_price_list_rate_for).

Refusal keys (append-only, emitted via ``coded_message``): PRICE-NO-RELEASE-STATE,
PRICE-NOT-VALID, PRICE-RELEASED-TODAY.
"""

from datetime import date

import frappe
from frappe import _

from ury.brand import coded_message

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
        frappe.throw(
            coded_message(
                "PRICE-NO-RELEASE-STATE",
                _("Item Price workflow {0} has no released (Success) state, so no menu price can be billed.").format(workflow),
            ),
            frappe.ValidationError,
        )
    return field, released


def item_price_rejected_states():
    """Item Price workflow states styled Danger (Frappe's style for Rejected); empty without an active workflow."""
    workflow = frappe.db.get_value("Workflow", _WORKFLOW, "name")
    return _states_styled(workflow, "Danger") if workflow else set()


def price_valid_on(item_code, price_list, posting_date, release_field, released, customer=None, currency=None):
    """The Item Price ERPNext get_price_list_rate_for would use on posting_date, released by the workflow if any.

    The line UOM is the item's selling UOM (sales_uom, else stock_uom), so every caller gets the same UOM.
    Candidates (_get_item_price_query): UOM empty or the line UOM; party-less or this customer's row, never a
    supplier's; no batch (URY lines carry none); valid on posting_date. Priority (_order_item_prices): this
    customer's row, then the latest valid_from, then a row in a UOM before a UOM-less one. ERPNext's Item Price
    check_duplicates refuses a second row with the same UOM, valid_from, party and batch; here the latest created
    row breaks that tie. When no row matches the selling UOM and it differs from the stock UOM, ERPNext retries
    with the stock UOM; so does this. Not modelled: packing units and multiplying a stock-UOM rate by the
    conversion factor (a URY menu line sells in one UOM).
    """
    fields = ["price_list_rate", "valid_from", "valid_upto", "uom", "customer", "supplier", "batch_no", "currency"]
    if release_field:
        fields.append(release_field)
    rows = frappe.db.get_list(
        "Item Price",
        filters={"item_code": item_code, "price_list": price_list},
        fields=fields,
        order_by="creation asc",
    )
    stock_uom = frappe.db.get_value("Item", item_code, "stock_uom")
    uom = frappe.db.get_value("Item", item_code, "sales_uom") or stock_uom
    found = _best_row(rows, uom, posting_date, release_field, released, customer, currency=currency)
    if found is None and uom != stock_uom:
        found = _best_row(rows, stock_uom, posting_date, release_field, released, customer, currency=currency)
    return found


def _best_row(rows, uom, posting_date, release_field, released, customer, currency=None):
    getdate = frappe.utils.getdate
    candidates = []
    for position, row in enumerate(rows):
        valid_from = getattr(row, "valid_from", None)
        valid_upto = getattr(row, "valid_upto", None)
        row_uom = getattr(row, "uom", None) or ""
        row_customer = getattr(row, "customer", None)
        row_currency = getattr(row, "currency", None)
        if row_uom not in ("", uom):
            continue
        if currency and row_currency and row_currency != currency:
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
        priority = (bool(row_customer), getdate(valid_from) if valid_from else date.min, bool(row_uom), position)
        candidates.append((priority, row))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]
