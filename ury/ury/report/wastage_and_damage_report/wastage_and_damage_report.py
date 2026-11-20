# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Wastage and Damage Report.

Maximus fork: a Script Report (was a Query Report) so it supplies its own defaults for the
optional `status` and `department` filters. Desk omits blank filters from the request, and
without these defaults the SQL raised KeyError. Same SQL and columns as the Query Report.
"""

import frappe

OPTIONAL_FILTERS = ("status", "department")

QUERY = """
SELECT
    w.name AS "Reference:Link/URY Wastage:160",
    w.posting_date AS "Posting Date:Date:100",
    w.branch AS "Branch:Link/Branch:120",
    w.department AS "Department:Link/URY Production Department:140",
    wi.item_code AS "Item Code:Link/Item:140",
    wi.item_name AS "Item Name::180",
    ROUND(wi.qty, 3) AS "Wasted Qty:Float:100",
    wi.uom AS "UOM::70",
    ROUND(IFNULL(ABS(sle.stock_value_difference), IFNULL(wi.amount, 0)), 2) AS "Total Cost:Currency:110",
    w.reason_category AS "Reason::120",
    w.reason_notes AS "Notes::160",
    w.status AS "Status::90",
    w.captured_by AS "Captured By::110",
    w.approved_by AS "Decided By::110"
FROM `tabURY Wastage` w
JOIN `tabWastage Item` wi ON wi.parent = w.name
LEFT JOIN `tabStock Ledger Entry` sle
       ON sle.voucher_no = w.stock_entry
      AND sle.item_code = wi.item_code
      AND sle.is_cancelled = 0
WHERE
    w.branch = %(branch)s
    AND w.posting_date >= %(start_date)s
    AND w.posting_date <= %(end_date)s
    AND (%(status)s = '' OR w.status = %(status)s)
    AND (%(department)s = '' OR w.department = %(department)s)
ORDER BY w.posting_date DESC, wi.item_code
"""


def execute(filters=None):
	filters = dict(filters or {})
	for fieldname in OPTIONAL_FILTERS:
		filters[fieldname] = filters.get(fieldname) or ""
	data = frappe.db.sql(QUERY, filters)
	columns = [column[0] for column in frappe.db.get_description()]
	return columns, [list(row) for row in data]
