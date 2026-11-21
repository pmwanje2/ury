"""Install only the two hold/fire fields on existing sites; no flag is enabled."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields


def execute():
    definitions = get_custom_fields()
    create_custom_fields({
        doctype: [field for field in definitions[doctype] if field["fieldname"] == name]
        for doctype, name in (
            ("POS Profile", "ury_enable_hold_fire"),
            ("POS Invoice Item", "ury_held"),
        )
    })
