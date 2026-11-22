import frappe


def execute():
    frappe.db.add_unique("URY Item Availability", ["branch", "item"], constraint_name="unique_branch_item_availability")
