"""Migrate POS approval PIN storage off User.custom_override_pin_hash into Frappe __Auth.

Approval PINs are stored exclusively in Frappe's `__Auth` table via
`frappe.utils.password.update_password`/`check_password` (`doctype="POS Override PIN"`,
`fieldname="pin_hash"`), never as a Custom Field on `User`.
Legacy single-round HMAC hashes on `User` cannot be converted to plaintext PINs, so
this patch deletes the Custom Field metadata and drops the `tabUser` column cleanly.
"""

import frappe


def remove_legacy_user_pin_field() -> None:
    if frappe.db.exists("Custom Field", "User-custom_override_pin_hash"):
        frappe.delete_doc("Custom Field", "User-custom_override_pin_hash", ignore_permissions=True, force=True)

    if frappe.db.has_column("User", "custom_override_pin_hash"):
        frappe.db.sql_ddl("ALTER TABLE `tabUser` DROP COLUMN `custom_override_pin_hash`")


def execute() -> None:
    remove_legacy_user_pin_field()
    frappe.clear_cache(doctype="User")
