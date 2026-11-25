"""Seed outlet policy settings and migrate the legacy covers column."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.ury_pos.covers import COVER_WHITESPACE, DEFAULT_MAX_COVERS, MAX_STORED_COVERS
from ury.ury_pos.floor_pin import FLOOR_PIN_SETTINGS

DEFAULT_QZ_ROLES = (
    "Administrator", "System Manager", "URY Admin", "URY Manager",
    "URY Cashier", "Restaurant Kitchen",
)
DEFAULT_AREA_TYPES = ("AC", "NON-AC", "Indoor", "Outdoor", "Rooftop", "Private Dining", "Bar Area")

POS_COVERS_FIELD = {
    "fieldname": "no_of_pax", "fieldtype": "Int", "label": "Pax", "length": 0,
    "insert_after": "column_break_rwbwf", "read_only": 1, "translatable": 0,
    "default": "1",
}

LEGACY_COVERS_FIELD = {
    "fieldname": "custom_legacy_covers", "fieldtype": "Small Text", "label": "Original Covers",
    "insert_after": "no_of_pax", "hidden": 1, "read_only": 1, "no_copy": 1, "translatable": 0,
}

PIN_LABELS = {
    "min_length": "Minimum Floor PIN Length",
    "max_length": "Maximum Floor PIN Length",
    "max_attempts": "Maximum Failed Floor PIN Attempts",
    "lockout_minutes": "Floor PIN Lockout (Minutes)",
    "session_minutes": "Floor PIN Session (Minutes)",
}
JOB_CODE = {
    "fieldname": "custom_pos_job_code", "fieldtype": "Link", "options": "Designation",
    "label": "POS Job Code", "read_only": 1, "no_copy": 1,
}


def _convert_legacy_covers():
    if frappe.db.get_value("Custom Field", "POS Invoice-no_of_pax", "fieldtype") != "Data":
        return

    # Preserve the exact original text in a hidden, off-row field before converting.
    # Historical values with no exact Int representation remain available for audit.
    create_custom_fields({"POS Invoice": [LEGACY_COVERS_FIELD]})
    frappe.db.sql("""
        UPDATE `tabPOS Invoice`
        SET custom_legacy_covers = no_of_pax
        WHERE custom_legacy_covers IS NULL
    """)
    frappe.db.sql("""
        UPDATE `tabPOS Invoice`
        SET no_of_pax = CASE
          WHEN REGEXP_REPLACE(custom_legacy_covers, %(edge_whitespace)s, '') REGEXP '^[+]?[0-9]+([.]0+)?$'
          THEN CASE
            WHEN CAST(REGEXP_REPLACE(custom_legacy_covers, %(edge_whitespace)s, '') AS DECIMAL(65, 0))
                 <= %(max_covers)s
            THEN CAST(REGEXP_REPLACE(custom_legacy_covers, %(edge_whitespace)s, '') AS DECIMAL(65, 0))
            ELSE 0
          END
          ELSE 0
        END
    """, {
        "edge_whitespace": f"^[{COVER_WHITESPACE}]+|[{COVER_WHITESPACE}]+$",
        "max_covers": MAX_STORED_COVERS,
    })


def ensure_employee_job_code():
    if frappe.db.exists("DocType", "Employee Checkin"):
        create_custom_fields({"Employee Checkin": [{**JOB_CODE, "insert_after": "log_type"}]})


def execute():
    _convert_legacy_covers()
    create_custom_fields({"POS Invoice": [POS_COVERS_FIELD]}, ignore_validate=True)
    profile_fields = [
        dict(fieldname="custom_max_covers", fieldtype="Int", label="Maximum Covers",
             insert_after="paid_limit", non_negative=1, default=str(DEFAULT_MAX_COVERS),
             description="Default maximum guests per check. Use 0 for no limit."),
        dict(fieldname="custom_order_type_cover_limits", fieldtype="Table", label="Covers by Order Type",
             options="POS Order Type Cover Limit", insert_after="custom_max_covers"),
        dict(fieldname="custom_pos_job_codes", fieldtype="Table MultiSelect", label="POS Job Codes",
             options="POS Job Code Option", insert_after="custom_require_clock_in_to_order",
             description="Job codes staff can select at clock-in. Leave empty to keep job selection optional."),
    ]
    insert_after = "custom_floor_pin_login"
    for name, (default, low, high) in FLOOR_PIN_SETTINGS.items():
        fieldname = f"custom_floor_pin_{name}"
        profile_fields.append(dict(
            fieldname=fieldname, fieldtype="Int", label=PIN_LABELS[name], default=str(default),
            non_negative=1, insert_after=insert_after, description=f"Allowed range: {low}–{high}.",
        ))
        insert_after = fieldname

    create_custom_fields({
        "POS Profile": profile_fields,
        "POS Opening Entry": [{**JOB_CODE, "insert_after": "user"}],
        "POS Closing Entry": [{**JOB_CODE, "insert_after": "user"}],
    })
    ensure_employee_job_code()

    used_types = frappe.get_all("URY Room", pluck="room_type", distinct=True)
    for index, name in enumerate(dict.fromkeys((*DEFAULT_AREA_TYPES, *filter(None, used_types)))):
        if not frappe.db.exists("Dining Area Type", name):
            frappe.get_doc(doctype="Dining Area Type", area_type=name, sort_order=index).insert()

    print_settings = frappe.get_single("POS Print Settings")
    if not print_settings.qz_signing_roles_initialized:
        if not print_settings.qz_signing_roles:
            print_settings.qz_signing_roles = "\n".join(DEFAULT_QZ_ROLES)
        print_settings.qz_signing_roles_initialized = 1
        print_settings.save(ignore_permissions=True)
