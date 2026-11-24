"""Set no_copy=1 on POS Invoice merge/split custom fields so native returns stay clean."""

import frappe

FIELDS = (
    "POS Invoice-custom_merged_pos_invoice",
    "POS Invoice-custom_split_from",
    "POS Invoice-custom_merged_total",
)


def execute():
    updated = False
    for name in FIELDS:
        if frappe.db.exists("Custom Field", name):
            if not frappe.db.get_value("Custom Field", name, "no_copy"):
                frappe.db.set_value("Custom Field", name, "no_copy", 1, update_modified=False)
                updated = True
    if updated:
        frappe.clear_cache(doctype="POS Invoice")
