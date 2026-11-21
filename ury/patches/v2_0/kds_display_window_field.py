"""Install the kitchen display window setting on existing sites; the default keeps 3 hours."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields


def execute():
	definitions = get_custom_fields()
	create_custom_fields({
		"POS Profile": [
			field for field in definitions["POS Profile"]
			if field["fieldname"] == "ury_kds_display_window_hours"
		],
	})
