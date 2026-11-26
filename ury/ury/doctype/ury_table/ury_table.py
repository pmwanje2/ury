# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname
import re
class URYTable(Document):
    def before_save(self):
        # Membership can change under the table lock without a Desk timestamp change.
        # A form opened before that change must not overwrite the server-owned roster.
        previous = self.get_doc_before_save()
        if previous and self.seating_checks != previous.seating_checks:
            self.occupied = previous.occupied
        self.seating_checks = previous.seating_checks if previous and self.occupied else "[]"

    def autoname(self):
        prefix = re.sub("-+", "-", self.restaurant.replace(" ", "-"))
        self.name = make_autoname(prefix + "-.##")
