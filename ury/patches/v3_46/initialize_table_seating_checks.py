import frappe

from ury.ury.api.seating_membership import update_members


def execute():
    for table in frappe.get_all("URY Table", filters={"seating_checks": ("is", "not set")},
                                pluck="name", order_by="name"):
        update_members(frappe, table)
