"""Install credit-limit override, room-charge stay, and offline tender custom fields."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields

CUSTOM_FIELD_TARGETS = {
    "Mode of Payment": (
        "custom_allow_offline",
        "custom_offline_requires_reference",
    ),
    "POS Profile": (
        "custom_offline_tender_limit",
    ),
    "POS Invoice": (
        "custom_room_charge_stay",
        "custom_override_approver",
        "custom_override_reason",
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


def seed_default_offline_cash():
    """Seed-once: mark Cash as offline-eligible only if no Mode of Payment has been configured yet."""
    if not frappe.db.table_exists("Mode of Payment"):
        return
    if not frappe.get_meta("Mode of Payment").has_field("custom_allow_offline"):
        return
    if frappe.db.exists("Mode of Payment", {"custom_allow_offline": 1}):
        return
    if frappe.db.exists("Mode of Payment", {"name": "Cash", "type": "Cash"}):
        frappe.db.set_value("Mode of Payment", "Cash", "custom_allow_offline", 1, update_modified=False)


def execute():
    ensure_custom_fields()
    seed_default_offline_cash()
