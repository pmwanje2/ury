# Copyright (c) 2023, Tridz Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class URYRestaurant(Document):
    def validate(self):
        from ury.ury_pos.dayparts import validate_serving_periods

        validate_serving_periods(self.get("custom_serving_periods") or [])
        if self.dine_in_invoice_series and self.dine_in_invoice_series.strip():
            from frappe.model.naming import NamingSeries

            self.dine_in_invoice_series = self.dine_in_invoice_series.strip()
            NamingSeries(self.dine_in_invoice_series).validate()

