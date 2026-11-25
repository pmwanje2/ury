# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class URYRoom(Document):
    def before_validate(self):
        if self.is_new() and not self.room_type:
            types = frappe.get_all("Dining Area Type", pluck="name", order_by="sort_order, name", limit=1)
            self.room_type = types[0] if types else None
