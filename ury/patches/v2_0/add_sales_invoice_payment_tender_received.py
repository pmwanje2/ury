"""Install server-only custom_tender_received on Sales Invoice Payment; existing rows stay 0 (not received)."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields

FIELD_NAME = "Sales Invoice Payment-custom_tender_received"


def execute():
    if frappe.db.exists("Custom Field", FIELD_NAME):
        current = frappe.db.get_value(
            "Custom Field",
            FIELD_NAME,
            ["no_copy", "read_only", "default"],
            as_dict=True,
        )
        if (
            current
            and current.get("no_copy") == 1
            and current.get("read_only") == 1
            and str(current.get("default")) == "0"
        ):
            return

    definitions = get_custom_fields()
    create_custom_fields(
        {
            "Sales Invoice Payment": [
                field
                for field in definitions["Sales Invoice Payment"]
                if field["fieldname"] == "custom_tender_received"
            ]
        }
    )
