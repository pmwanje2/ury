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
