"""Install the opt-in field only; never activate a profile or a programme."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields


def execute():
    create_custom_fields({"POS Profile": [
        field for field in get_custom_fields()["POS Profile"] if field["fieldname"] == "ury_enable_loyalty"
    ]})
