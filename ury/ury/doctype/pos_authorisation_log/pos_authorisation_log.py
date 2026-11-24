import frappe
from frappe.model.document import Document


class POSAuthorisationLog(Document):
    def validate(self):
        if (
            not self.is_new()
            or not self.flags.get("pos_override_server_insert")
            or not self.flags.ignore_permissions
        ):
            frappe.throw(
                frappe._("Authorisation log entries are server-managed and cannot be modified.")
                + " [POS-AUTH-NOT-ELIGIBLE]",
                frappe.PermissionError,
            )

    def on_trash(self):
        frappe.throw(
            frappe._("Authorisation log history is retained and cannot be deleted.")
            + " [POS-AUTH-NOT-ELIGIBLE]",
            frappe.PermissionError,
        )
