"""Remove the earlier ownership view before a logical tenant backup."""
import frappe


def execute():
    if frappe.db.sql("""
        SELECT 1 FROM information_schema.VIEWS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ury_kitchen_ticket_owner'
    """):
        frappe.db.sql_ddl("DROP VIEW IF EXISTS ury_kitchen_ticket_owner")
