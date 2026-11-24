"""Install approval PIN custom fields and seed-once POS Privilege Rule & POS Reason defaults."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ury.setup_customizations import get_custom_fields

CUSTOM_FIELD_TARGETS = {
    "User": ("custom_override_pin_hash",),
    "POS Profile": ("custom_override_max_attempts", "custom_override_lock_minutes"),
}

# Seed-once client-owned POS Reasons (Amendment 1 #4: discounts use siteops Discount Reason).
DEFAULT_POS_REASONS = (
    # Void & waste
    ("VOID-GUEST", "Guest changed mind", "void_sent_item\nreduce_sent_qty\ncancel_check", 0, 10),
    ("VOID-KEYED", "Wrong item keyed", "void_sent_item\nreduce_sent_qty\ncancel_check", 0, 20),
    ("VOID-KITCHEN", "Kitchen error / out of stock", "void_sent_item\nreduce_sent_qty\ncancel_check", 0, 30),
    ("WASTE-SPOILED", "Spoiled or dropped item (waste)", "void_sent_item\nreduce_sent_qty\ncancel_check", 0, 40),
    # Return / refund
    ("RET-QUALITY", "Item quality complaint", "return", 0, 50),
    ("RET-OVERCHARGE", "Billing error / overcharge", "return", 0, 60),
    # Reopen / adjust closed check
    ("REOPEN-TENDER", "Wrong payment method", "reopen_check\nadjust_closed_check", 0, 70),
    ("REOPEN-SPLIT", "Guest requested split or correction", "reopen_check\nadjust_closed_check", 0, 80),
    # No sale
    ("NOSALE-CHANGE", "Making change for guest", "no_sale", 0, 90),
    ("NOSALE-COUNT", "Manager cash drawer check", "no_sale", 0, 100),
    # Paid out / cash movement
    ("PAIDOUT-SUPPLIES", "Petty cash supplies purchase", "paid_out", 1, 110),
    ("PAIDOUT-DELIVERY", "Vendor COD delivery", "paid_out", 1, 120),
    ("CASH-DROP", "Safe drop / cash pickup", "cash_pickup\ncash_loan", 0, 130),
    # Price override / open item
    ("PRICE-SPECIAL", "Manager agreed price / market special", "price_override\nopen_item", 1, 140),
    # Tax exempt
    ("TAX-DIPLOMAT", "Diplomatic / NGO tax exemption", "tax_exempt", 1, 150),
    ("TAX-EXPORT", "Exempt guest / zero-rated export", "tax_exempt", 1, 160),
    # Reprint
    ("REPRINT-GUEST", "Guest requested copy", "reprint_bill\nreprint_kot", 0, 170),
    ("REPRINT-PRINTER", "Printer jam / paper out", "reprint_bill\nreprint_kot", 0, 180),
    # Catch-all (note mandatory)
    ("OTHER", "Other", "", 1, 999),
)


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


def seed_default_rules(company=None):
    """Seed Simphony/SAP POS Privilege Rule defaults once per company (Amendment 1 #3).

    Every role starts at 0% alone for discounts (enforced with Approval Limit).
    Never overwrites existing rules and never resurrects deleted rules once a
    company has any POS Privilege Rule rows.
    """
    from ury.ury_pos.override import PRIVILEGES

    if not frappe.db.table_exists("POS Privilege Rule"):
        return

    companies = [company] if company else frappe.get_all("Company", pluck="name")
    existing_roles = set(frappe.get_all("Role", pluck="name"))

    for comp in companies:
        if not comp:
            continue
        if frappe.db.exists("POS Privilege Rule", {"company": comp}):
            continue

        for code, spec in PRIVILEGES.items():
            roles = sorted(set(spec.default_perform_roles) | set(spec.default_authorise_roles))
            for role in roles:
                if role not in existing_roles:
                    continue
                if frappe.db.exists(
                    "POS Privilege Rule",
                    {
                        "company": comp,
                        "pos_profile": ["in", ["", None]],
                        "privilege": code,
                        "role": role,
                    },
                ):
                    continue
                frappe.get_doc(
                    {
                        "doctype": "POS Privilege Rule",
                        "company": comp,
                        "pos_profile": "",
                        "privilege": code,
                        "role": role,
                        "may_perform": 1 if role in spec.default_perform_roles else 0,
                        "may_authorise": 1 if role in spec.default_authorise_roles else 0,
                        "max_percent": 0,
                        "max_amount": 0,
                        "requires_reason": 1 if spec.reason_required else 0,
                        "allow_self_approval": 0,
                        "require_clocked_in": 0,
                        "privilege_group": 0,
                    }
                ).insert(ignore_permissions=True)


def seed_default_reasons():
    """Seed client-owned POS Reason defaults once. Never overwrites or resurrects."""
    if not frappe.db.table_exists("POS Reason"):
        return
    if frappe.db.exists("POS Reason", {}):
        return
    for code, description, applies_to, requires_note, sort_order in DEFAULT_POS_REASONS:
        if frappe.db.exists("POS Reason", code):
            continue
        frappe.get_doc(
            {
                "doctype": "POS Reason",
                "code": code,
                "description": description,
                "applies_to": applies_to,
                "requires_note": requires_note,
                "sort_order": sort_order,
                "disabled": 0,
            }
        ).insert(ignore_permissions=True)


def execute():
    ensure_custom_fields()
    seed_default_rules()
    seed_default_reasons()
