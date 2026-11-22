import frappe
from frappe import _
from frappe.model.document import Document


class URYItemAvailability(Document):
    def validate(self):
        if not self.flags.availability_operation:
            frappe.throw(_("[AVAIL-ROLE] Use the menu availability controls to change this item."), frappe.PermissionError)
        if self.remaining_qty < 0 or self.daily_limit < self.remaining_qty:
            frappe.throw(_("[AVAIL-COUNT-INVALID] Enter a non-negative whole count."))
        if frappe.db.exists(self.doctype, {"branch": self.branch, "item": self.item, "name": ("!=", self.name or "")}):
            frappe.throw(_("[AVAIL-COUNT-INVALID] This branch already has an availability record for this item."))
