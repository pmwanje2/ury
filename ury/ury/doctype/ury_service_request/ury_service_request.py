# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class URYServiceRequest(Document):
	def validate(self):
		# Native workflow saves change status without going through a URY API.
		if self.status != "Resolved":
			return
		previous = self.get_doc_before_save()
		if previous and previous.get("status") == "Resolved":
			return
		if not self.resolved_by:
			self.resolved_by = frappe.session.user
		if not self.resolved_at:
			self.resolved_at = frappe.utils.now_datetime()

	def on_update(self):
		# Runs after insert and every save. The doctype room only admits sockets
		# that pass read permission, so guests never receive this; clients filter
		# by branch and keep their poll as the fallback.
		frappe.publish_realtime(
			event="ury_service_request",
			message={
				"name": self.name,
				"table": self.table,
				"branch": frappe.db.get_value("URY Table", self.table, "branch") if self.table else None,
				"request_type": self.request_type,
				"status": self.status,
				"requested_at": str(self.requested_at) if self.requested_at else None,
			},
			room="doctype:URY Service Request",
			doctype="URY Service Request",
			after_commit=True,
		)
