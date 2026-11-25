"""Dedicated custom-field definitions for POS Timeclock (G36) & Shift Tips Declaration (G38).

Pure Python module (zero top-level `import frappe`) so site-less host unit tests
can inspect definitions directly and patches / setup_customizations share a single source of truth.
"""

from __future__ import annotations


def get_timeclock_tips_custom_fields() -> dict[str, list[dict]]:
    return {
        "POS Profile": [
            {
                "fieldname": "custom_require_clock_in_to_order",
                "fieldtype": "Check",
                "label": "Require Clock-In to Order",
                "default": "0",
                "insert_after": "custom_override_lock_minutes",
                "description": (
                    "When enabled and HR attendance is installed, staff with an active Employee "
                    "record must clock in before taking orders, settling bills, or opening a shift."
                ),
            },
            {
                "fieldname": "custom_block_clock_out_with_open_work",
                "fieldtype": "Check",
                "label": "Block Clock-Out with Open Work",
                "default": "1",
                "insert_after": "custom_require_clock_in_to_order",
                "description": (
                    "Prevent staff from clocking out at the register while they own open draft checks "
                    "or an open till shift."
                ),
            },
            {
                "fieldname": "custom_require_tip_declaration",
                "fieldtype": "Check",
                "label": "Require Tip Declaration at Close",
                "default": "0",
                "insert_after": "custom_require_over_short_reason",
                "description": (
                    "Require cash tip declarations to be recorded for all shift servers before "
                    "submitting a shift close."
                ),
            },
            {
                "fieldname": "custom_tips_salary_component",
                "fieldtype": "Data",
                "label": "Tips Payroll Component",
                "insert_after": "custom_require_tip_declaration",
                "translatable": 0,
                "description": (
                    "Optional payroll earning component used by HR/Accounts Managers when posting "
                    "unpaid net shift tips to payroll."
                ),
            },
        ],
        "POS Closing Entry": [
            {
                "fieldname": "custom_tip_declarations_section",
                "fieldtype": "Section Break",
                "label": "Tips Declaration",
                "insert_after": "custom_over_short_reason",
                "collapsible": 0,
            },
            {
                "fieldname": "custom_tip_declarations",
                "fieldtype": "Table",
                "label": "Tip Declarations",
                "options": "POS Tip Declaration",
                "insert_after": "custom_tip_declarations_section",
                "no_copy": 1,
                "description": (
                    "Server cash tip declarations and net charged tip reconciliation for this shift. "
                    "Direct cash tips do not affect cash drawer expected cash or Over/Short variance."
                ),
            },
        ],
    }


def custom_field_fixture_names() -> set[str]:
    names: set[str] = set()
    for dt, fields in get_timeclock_tips_custom_fields().items():
        for field in fields:
            names.add(f"{dt}-{field['fieldname']}")
    return names
