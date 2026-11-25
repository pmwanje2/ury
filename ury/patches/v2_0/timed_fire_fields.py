"""Install timed-fire settings and line metadata on existing sites."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields


def execute():
    fields = {
        "POS Profile": {
            "default_fire_delay_minutes", "auto_fire_next_course_on_bump", "require_course_fire_confirmation",
        },
        "POS Invoice Item": {"fire_at", "fire_confirmation_pending"},
    }
    definitions = get_custom_fields()
    create_custom_fields({
        doctype: [field for field in definitions[doctype] if field["fieldname"] in names]
        for doctype, names in fields.items()
    })
