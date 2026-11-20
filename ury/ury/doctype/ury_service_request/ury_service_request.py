# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

# Staff who answer guest calls (Oracle Simphony service requests go to the
# revenue centre's floor and till devices).
FLOOR_ROLES = ("URY Captain", "URY Cashier", "URY Manager")
# Open -> Acknowledged -> Resolved, forward only.
STATUS_ORDER = {"Open": 0, "Acknowledged": 1, "Resolved": 2}
AUDIT = {"Acknowledged": ("acknowledged_by", "acknowledged_at"), "Resolved": ("resolved_by", "resolved_at")}


def branch_staff(branch):
	"""Enabled users of `branch` (its URY User list) who hold a floor or till role."""
	members = frappe.get_all("URY User", filters={"parent": branch, "parenttype": "Branch"}, pluck="user")
	if not members:
		return []
	enabled = frappe.get_all("User", filters={"name": ["in", members], "enabled": 1}, pluck="name")
	if not enabled:
		return []
	holders = frappe.get_all(
		"Has Role",
		filters={"parent": ["in", enabled], "parenttype": "User", "role": ["in", list(FLOOR_ROLES)]},
		pluck="parent",
	)
	return sorted(set(holders))


class URYServiceRequest(Document):
	def validate(self):
		# Also runs for native workflow saves, which change status without a URY API.
		previous = self.get_doc_before_save() or {}
		before = previous.get("status")
		if before == "Resolved" and self.status != "Resolved":
			frappe.throw(frappe._("URY_SERVICE_REQUEST_ALREADY_RESOLVED"), frappe.ValidationError)
		if before and STATUS_ORDER.get(self.status, 0) < STATUS_ORDER.get(before, 0):
			frappe.throw(frappe._("URY_SERVICE_REQUEST_STATUS_BACKWARD"), frappe.ValidationError)
		# Recorded audit values are never rewritten.
		for fields in AUDIT.values():
			for field in fields:
				if previous.get(field):
					setattr(self, field, previous.get(field))
		if before == self.status or self.status not in AUDIT:
			return
		by, at = AUDIT[self.status]
		if not getattr(self, by, None):
			setattr(self, by, frappe.session.user)
		if not getattr(self, at, None):
			setattr(self, at, frappe.utils.now_datetime())

	def on_trash(self):
		# Simphony keeps service-request history; the audit trail is never deleted.
		frappe.throw(frappe._("URY_SERVICE_REQUEST_DELETE_REFUSED"), frappe.ValidationError)

	def on_update(self):
		# Runs after insert and every save. One user room per branch staff member:
		# nobody outside the table's branch receives it, and guests never do.
		# Clients keep their poll as the fallback.
		branch = frappe.db.get_value("URY Table", self.table, "branch") if self.table else None
		if not branch:
			return
		message = {
			"name": self.name,
			"table": self.table,
			"branch": branch,
			"request_type": self.request_type,
			"status": self.status,
			"requested_at": str(self.requested_at) if self.requested_at else None,
		}
		for user in branch_staff(branch):
			frappe.publish_realtime(event="ury_service_request", message=message, user=user, after_commit=True)
