# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class URYRestaurant(Document):
    def validate(self):
        from ury.ury_pos.dayparts import validate_serving_periods

        validate_serving_periods(self.get("custom_serving_periods") or [])

