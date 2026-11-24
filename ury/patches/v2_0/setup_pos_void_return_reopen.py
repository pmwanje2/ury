"""Install B5a void/waste/return/reopen custom fields idempotently (never overwrites client edits)."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields

CUSTOM_FIELD_TARGETS = {
    "POS Profile": ("custom_unreferenced_return_roles",),
    "POS Invoice": (
        "custom_reopened_from",
        "custom_return_approver",
        "custom_waste_stock_entry",
    ),
    "URY KOT": (
        "custom_waste_stock_entry",
        "custom_void_reason_code",
    ),
}


def ensure_custom_fields():
    definitions = get_custom_fields()
    to_create = {}
    for dt, fieldnames in CUSTOM_FIELD_TARGETS.items():
        missing = [
            f
            for f in definitions.get(dt, [])
            if f["fieldname"] in fieldnames and not frappe.db.exists("Custom Field", f"{dt}-{f['fieldname']}")
        ]
        if missing:
            to_create[dt] = missing
    if to_create:
        create_custom_fields(to_create)


def execute():
    ensure_custom_fields()
