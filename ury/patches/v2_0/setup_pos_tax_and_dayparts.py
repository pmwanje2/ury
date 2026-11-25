"""Install custom fields for G42 (order-type tax categories, tax exempt lock) and G40/G41 (serving periods & outlet time stamping)."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields

CUSTOM_FIELD_TARGETS = {
    "POS Profile": ("custom_order_type_tax_categories",),
    "POS Invoice": (
        "custom_serving_period",
        "custom_transaction_local_time",
        "custom_transaction_weekday",
        "custom_tax_exempt",
    ),
    "POS Invoice Item": ("custom_tax_exempt",),
    "Sales Invoice": (
        "custom_serving_period",
        "custom_transaction_local_time",
        "custom_transaction_weekday",
    ),
}


def ensure_custom_fields():
    definitions = get_custom_fields()
    to_create = {}
    for dt, fieldnames in CUSTOM_FIELD_TARGETS.items():
        missing = [
            field
            for field in definitions.get(dt, [])
            if field["fieldname"] in fieldnames
            and not frappe.db.exists("Custom Field", f"{dt}-{field['fieldname']}")
        ]
        if missing:
            to_create[dt] = missing
    if to_create:
        create_custom_fields(to_create)


def execute():
    ensure_custom_fields()
