"""Custom fields for POS-PR-11 (G23 Cash Drawer, G26 KDS Expo/Rush/VIP/Recall, G28 Backup Printer).

Isolated in its own module and installed idempotently via
`ury.patches.v2_0.setup_kds_expo_routing_drawer` + `setup_customizations.after_install`
to avoid merge collisions with in-flight POS PRs touching `fixtures/custom_field.json`.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def get_kds_device_custom_fields() -> dict[str, list[dict]]:
    return {
        "Mode of Payment": [
            {
                "fieldname": "custom_open_cash_drawer",
                "fieldtype": "Check",
                "label": "Open Cash Drawer",
                "description": (
                    "Automatically trigger the cash drawer kick pulse when settling "
                    "an order with this payment method."
                ),
                "default": "0",
                "insert_after": "enabled",
            },
        ],
        "POS Profile": [
            {
                "fieldname": "custom_drawer_pin",
                "fieldtype": "Select",
                "label": "Cash Drawer Pin",
                "options": "2\n5",
                "default": "2",
                "description": (
                    "RJ11/RJ12 cash drawer kick pin (Pin 2 = Drawer 1, "
                    "Pin 5 = Drawer 2 for dual-drawer splitters)."
                ),
                "insert_after": "qz_host",
                "translatable": 0,
            },
            {
                "fieldname": "custom_drawer_kick_hex",
                "fieldtype": "Data",
                "label": "Custom Drawer Kick Command (Hex)",
                "default": "",
                "description": (
                    "Optional raw ESC/POS hex pulse override (e.g. 1b700019fa for Pin 2 "
                    "or 1b700119fa for Pin 5). Leave empty to derive from Cash Drawer Pin."
                ),
                "insert_after": "custom_drawer_pin",
                "translatable": 0,
            },
            {
                "fieldname": "custom_auto_kick_on_cash_tender",
                "fieldtype": "Check",
                "label": "Auto-Kick Cash Drawer on Settlement",
                "default": "1",
                "description": (
                    "Kick the cash drawer automatically on settlement when a configured "
                    "drawer-opening payment method is tendered."
                ),
                "insert_after": "custom_drawer_kick_hex",
            },
            {
                "fieldname": "custom_kds_recall_window_minutes",
                "fieldtype": "Int",
                "label": "Kitchen Display Recall Window (Minutes)",
                "default": "15",
                "non_negative": 1,
                "description": (
                    "Minutes after serving during which a kitchen ticket can be recalled "
                    "(1-240; empty or 0 uses 15)."
                ),
                "insert_after": "ury_kds_display_window_hours",
            },
        ],
        "POS Invoice": [
            {
                "fieldname": "custom_is_rush",
                "fieldtype": "Check",
                "label": "Rush Order",
                "default": "0",
                "no_copy": 1,
                "description": "Prioritise this check at the top of Kitchen Display screens.",
                "insert_after": "order_type",
            },
            {
                "fieldname": "custom_is_vip",
                "fieldtype": "Check",
                "label": "VIP Order",
                "default": "0",
                "no_copy": 1,
                "description": "Highlight this check with VIP priority on Kitchen Display screens.",
                "insert_after": "custom_is_rush",
            },
        ],
        "URY KOT": [
            {
                "fieldname": "custom_is_rush",
                "fieldtype": "Check",
                "label": "Rush",
                "default": "0",
                "allow_on_submit": 1,
                "no_copy": 1,
                "insert_after": "type",
            },
            {
                "fieldname": "custom_is_vip",
                "fieldtype": "Check",
                "label": "VIP",
                "default": "0",
                "allow_on_submit": 1,
                "no_copy": 1,
                "insert_after": "custom_is_rush",
            },
        ],
        "Network Printer Settings": [
            {
                "fieldname": "custom_backup_printer",
                "fieldtype": "Link",
                "options": "Network Printer Settings",
                "label": "Backup Printer",
                "description": (
                    "Automatic fallback printer used when submitting a job to this "
                    "primary printer fails."
                ),
                "insert_after": "custom_printer_queue_name",
            },
            {
                "fieldname": "custom_raw_escpos_host",
                "fieldtype": "Data",
                "label": "Raw ESC/POS Host",
                "description": (
                    "Optional direct TCP/9100 host for sending raw ESC/POS cash drawer "
                    "kick pulses."
                ),
                "insert_after": "custom_backup_printer",
                "translatable": 0,
            },
            {
                "fieldname": "custom_raw_escpos_port",
                "fieldtype": "Int",
                "label": "Raw ESC/POS Port",
                "default": "9100",
                "non_negative": 1,
                "description": "TCP port for direct raw ESC/POS commands (default 9100).",
                "insert_after": "custom_raw_escpos_host",
            },
        ],
    }


def ensure_kds_device_custom_fields() -> None:
    fields = get_kds_device_custom_fields()
    if frappe.db.exists("DocType", "POS Drawer Event"):
        fields["POS Drawer Event"] = [
            {
                "fieldname": "custom_kick_delivery_status",
                "fieldtype": "Select",
                "label": "Kick Delivery Status",
                "options": "Not Configured\nSent\nFailed",
                "default": "Not Configured",
                "read_only": 1,
                "insert_after": "drawer_kick_sent",
                "translatable": 0,
            },
        ]
    create_custom_fields(fields, update=True)
