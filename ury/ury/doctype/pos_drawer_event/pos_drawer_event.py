"""Insert-only audit log for POS drawer and till control events (No Sale, Threshold Exception,
Till Reopen, Force Close)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ury.brand import coded_message


class POSDrawerEvent(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw(
                coded_message("DRAWER-IMMUTABLE", _("Drawer audit events cannot be modified after recording.")),
                frappe.ValidationError,
            )
        if not getattr(self.flags, "pos_drawer_server_insert", False):
            frappe.throw(
                coded_message("DRAWER-IMMUTABLE", _("Drawer audit events can only be created by the till server.")),
                frappe.PermissionError,
            )

    def on_trash(self):
        frappe.throw(
            coded_message("DRAWER-IMMUTABLE", _("Drawer audit events cannot be deleted.")),
            frappe.ValidationError,
        )
