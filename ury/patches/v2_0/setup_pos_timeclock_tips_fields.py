"""Install POS Timeclock (G36) and Tips Declaration (G38) custom fields idempotently."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.ury_pos.timeclock_tips_fields import get_timeclock_tips_custom_fields


def ensure_custom_fields():
    definitions = get_timeclock_tips_custom_fields()
    to_create = {}
    for dt, fields in definitions.items():
        missing = [
            f
            for f in fields
            if not frappe.db.exists("Custom Field", f"{dt}-{f['fieldname']}")
        ]
        if missing:
            to_create[dt] = missing
    if to_create:
        create_custom_fields(to_create)


def execute():
    ensure_custom_fields()
