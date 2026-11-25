import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from ury.brand import coded_message


class URYModifierGroup(Document):
	def validate(self):
		if not (self.display_title or "").strip():
			self.display_title = self.group_name

		self.min_selections = max(0, cint(self.min_selections))
		self.max_selections = max(0, cint(self.max_selections))

		if cint(self.required) and self.min_selections < 1:
			self.min_selections = 1

		if self.max_selections > 0 and self.max_selections < self.min_selections:
			frappe.throw(
				coded_message(
					"MODIFIER-CONFIG",
					_("Maximum selections ({0}) cannot be less than minimum selections ({1}).").format(
						self.max_selections, self.min_selections
					),
				),
				frappe.ValidationError,
			)

		seen_codes = set()
		for row in self.get("options") or []:
			code = (row.option_code or row.item or row.label or "").strip()
			if not code:
				frappe.throw(
					coded_message("MODIFIER-CONFIG", _("Each option requires a code or label.")),
					frappe.ValidationError,
				)
			row.option_code = code
			if not (row.label or "").strip():
				row.label = row.item or code
			if code in seen_codes:
				frappe.throw(
					coded_message(
						"MODIFIER-CONFIG",
						_("Duplicate option code '{0}' in group '{1}'.").format(code, self.group_name),
					),
					frappe.ValidationError,
				)
			seen_codes.add(code)

			if flt(row.upcharge_amount) < 0:
				frappe.throw(
					coded_message("MODIFIER-CONFIG", _("Upcharge amount cannot be negative.")),
					frappe.ValidationError,
				)
			if flt(row.qty) <= 0:
				row.qty = 1
			if row.price_mode == "Menu Item Price" and not row.item:
				frappe.throw(
					coded_message(
						"MODIFIER-CONFIG",
						_("Option '{0}' uses Menu Item Price and must link to an item.").format(row.label),
					),
					frappe.ValidationError,
				)
