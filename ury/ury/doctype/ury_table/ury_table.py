# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document
from frappe.model.naming import make_autoname
import re
class URYTable(Document):
    def before_save(self):
        # Lifecycle transitions write directly under the table lock and may leave
        # modified unchanged. A Desk form can therefore carry stale floor state.
        previous = self.get_doc_before_save()
        if previous:
            for field in ("occupied", "custom_table_state", "custom_dirty_since", "latest_invoice_time", "seating_checks"):
                self.set(field, previous.get(field))
        else:
            self.seating_checks = "[]"

    def autoname(self):
        prefix = re.sub("-+", "-", self.restaurant.replace(" ", "-"))
        self.name = make_autoname(prefix + "-.##")
