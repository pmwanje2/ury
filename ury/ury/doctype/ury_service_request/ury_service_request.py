# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class URYServiceRequest(Document):
	# Service request creation and resolution logic live in
	# ury/ury/api/self_ordering.py (added in a later phase), not here.

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
