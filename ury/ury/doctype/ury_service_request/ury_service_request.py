# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class URYServiceRequest(Document):
	def validate(self):
		# Native workflow saves change status without going through a URY API.
		if self.status != "Resolved" or self.resolved_by:
			return
		previous = self.get_doc_before_save()
		if previous and previous.get("status") == "Resolved":
			return
		self.resolved_by = frappe.session.user
		self.resolved_at = frappe.utils.now_datetime()
