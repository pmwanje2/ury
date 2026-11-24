import frappe
from frappe.model.document import Document

from ury.brand import coded_message


class URYBillReprintLog(Document):
    def validate(self):
        if not self.is_new() or not self.flags.ignore_permissions:
            frappe.throw(coded_message('COPY-001', 'Reprint attempts are worker-managed.'), frappe.PermissionError)

    def on_trash(self):
        frappe.throw(coded_message('COPY-001', 'Reprint history is retained.'), frappe.PermissionError)
