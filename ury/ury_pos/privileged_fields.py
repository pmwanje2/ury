"""Custom fields required by Scope e+f privileged POS actions (data, not code).

Kept in its own module and installed idempotently via
`ury.patches.v2_0.setup_pos_privileged_actions` + `setup_customizations.after_install`
so merging B2a (`feat/pos-manager-authorisation`) and #145 (`feat/pos-tips-service-cash`)
causes zero field collisions.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def get_privileged_action_custom_fields() -> dict[str, list[dict]]:
    return {
        "Item": [
            {
                "fieldname": "custom_is_open_priced",
                "fieldtype": "Check",
                "label": "Open-Priced Menu Item",
                "description": "Simphony Menu Item Class Opt 1: allows entering a market/catch-weight/special price at the till.",
                "default": "0",
                "insert_after": "is_sales_item",
            },
            {
                "fieldname": "custom_allow_zero_price",
                "fieldtype": "Check",
                "label": "Allow Zero Price",
                "description": "Simphony Opt 8 / SAP Allow Zero Price: permits 0.00 price at the till without an Item Price row.",
                "default": "0",
                "insert_after": "custom_is_open_priced",
            },
            {
                "fieldname": "custom_open_item_require_reference",
                "fieldtype": "Check",
                "label": "Open Item Reference Required",
                "description": "Simphony Opt 5: prompts for dish name/reference printed on KOT and guest check.",
                "default": "1",
                "insert_after": "custom_allow_zero_price",
            },
            {
                "fieldname": "custom_halo_max_price",
                "fieldtype": "Currency",
                "label": "Open Item Max Price (HALO)",
                "description": "Above this amount, a manager PIN is required.",
                "default": "0",
                "insert_after": "custom_open_item_require_reference",
            },
            {
                "fieldname": "custom_min_price",
                "fieldtype": "Currency",
                "label": "Minimum Allowed Price",
                "description": "Below this amount, a manager PIN is required.",
                "default": "0",
                "insert_after": "custom_halo_max_price",
            },
            {
                "fieldname": "custom_privilege_group",
                "fieldtype": "Int",
                "label": "Privilege Group",
                "description": "Simphony Privilege Group 0-3 (0 = unrestricted).",
                "default": "0",
                "non_negative": 1,
                "insert_after": "custom_min_price",
            },
        ],
        "POS Profile": [
            {
                "fieldname": "custom_service_charge_account",
                "fieldtype": "Link",
                "options": "Account",
                "label": "Service Charge Account",
                "insert_after": "account_for_change_amount",
            },
            {
                "fieldname": "custom_service_charge_rate",
                "fieldtype": "Percent",
                "label": "Service Charge Rate (%)",
                "default": "0",
                "insert_after": "custom_service_charge_account",
            },
            {
                "fieldname": "custom_service_charge_min_pax",
                "fieldtype": "Int",
                "label": "Service Charge Min Covers",
                "default": "0",
                "non_negative": 1,
                "insert_after": "custom_service_charge_rate",
            },
            {
                "fieldname": "custom_service_charge_order_types",
                "fieldtype": "Small Text",
                "label": "Service Charge Order Types",
                "default": "Dine In",
                "description": "One order type per line (or comma-separated).",
                "insert_after": "custom_service_charge_min_pax",
            },
            {
                "fieldname": "custom_service_charge_taxable",
                "fieldtype": "Check",
                "label": "Service Charge Is Taxable",
                "default": "0",
                "insert_after": "custom_service_charge_order_types",
            },
            {
                "fieldname": "custom_service_charge_description",
                "fieldtype": "Data",
                "label": "Service Charge Label",
                "default": "Service Charge",
                "insert_after": "custom_service_charge_taxable",
            },
            {
                "fieldname": "custom_service_charge_rules",
                "fieldtype": "Table",
                "options": "POS Service Charge Rule",
                "label": "Service Charge Rules",
                "description": "Order-type and party-size service charge rules (Percentage or Fixed Amount, Pre/Post-discount, Taxable).",
                "insert_after": "custom_service_charge_description",
            },
            {
                "fieldname": "custom_tax_exempt_category",
                "fieldtype": "Link",
                "options": "Tax Category",
                "label": "Default Tax Exempt Category",
                "insert_after": "custom_service_charge_rules",
            },
            {
                "fieldname": "custom_exempt_item_tax_template",
                "fieldtype": "Link",
                "options": "Item Tax Template",
                "label": "Default Exempt Item Tax Template",
                "insert_after": "custom_tax_exempt_category",
            },
            # Note: POS Profile-custom_tips_payable_account is canonically defined in
            # ury/fixtures/custom_field.json (#145). Do not duplicate it here.
            {
                "fieldname": "custom_max_tip_percent",
                "fieldtype": "Percent",
                "label": "Max Tip Percent Before PIN",
                "default": "30",
                "insert_after": "custom_exempt_item_tax_template",
            },
            {
                "fieldname": "custom_max_free_bill_prints",
                "fieldtype": "Int",
                "label": "Max Free Bill Prints",
                "description": "Free pro-forma guest check prints before requiring manager PIN (Simphony default: 1).",
                "default": "1",
                "non_negative": 1,
                "insert_after": "custom_max_tip_percent",
            },
            {
                "fieldname": "custom_max_free_closed_copies",
                "fieldtype": "Int",
                "label": "Max Free Closed Receipt Copies",
                "description": "Free closed-receipt copies before requiring manager PIN (default: 0).",
                "default": "0",
                "non_negative": 1,
                "insert_after": "custom_max_free_bill_prints",
            },
            {
                "fieldname": "custom_max_splits_before_pin",
                "fieldtype": "Int",
                "label": "Max Splits Before Manager PIN",
                "description": "Simphony Opt 102: 0 = unlimited, 1 = re-splitting an already-split check needs manager PIN.",
                "default": "0",
                "non_negative": 1,
                "insert_after": "custom_max_free_closed_copies",
            },
        ],
        "POS Invoice": [
            {
                "fieldname": "custom_is_complimentary",
                "fieldtype": "Check",
                "label": "Complimentary Check",
                "default": "0",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "cancel_reason",
            },
            {
                "fieldname": "custom_comp_type",
                "fieldtype": "Select",
                "options": "\nComplimentary\nHouse\nNon-Revenue",
                "label": "Comp Type",
                "length": 64,
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_is_complimentary",
            },
            {
                "fieldname": "custom_comp_reason",
                "fieldtype": "Small Text",
                "label": "Comp Reason",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_comp_type",
            },
            {
                "fieldname": "custom_comp_approver",
                "fieldtype": "Link",
                "options": "User",
                "length": 80,
                "label": "Comp Approver",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_comp_reason",
            },
            {
                "fieldname": "custom_tax_exempt",
                "fieldtype": "Check",
                "label": "Tax Exempt",
                "default": "0",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_comp_approver",
            },
            {
                "fieldname": "custom_tax_exempt_reason",
                "fieldtype": "Small Text",
                "label": "Tax Exempt Reason",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_tax_exempt",
            },
            {
                "fieldname": "custom_tax_exempt_certificate",
                "fieldtype": "Small Text",
                "label": "Tax Exempt Certificate / Reference",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_tax_exempt_reason",
            },
            {
                "fieldname": "custom_tax_exempt_approver",
                "fieldtype": "Link",
                "options": "User",
                "length": 80,
                "label": "Tax Exempt Approver",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_tax_exempt_certificate",
            },
            {
                "fieldname": "custom_tax_exempt_customer",
                "fieldtype": "Link",
                "options": "Customer",
                "length": 80,
                "label": "Tax Exempt Customer",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_tax_exempt_approver",
            },
            {
                "fieldname": "custom_efris_tax_category",
                "fieldtype": "Data",
                "label": "EFRIS Tax Category",
                "description": "URA EFRIS tax category code (02 Zero-rated, 03 Exempt, 04 Deemed).",
                "length": 64,
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_tax_exempt_customer",
            },
            {
                "fieldname": "custom_service_charge_waived",
                "fieldtype": "Check",
                "label": "Service Charge Waived",
                "default": "0",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_efris_tax_category",
            },
            {
                "fieldname": "custom_service_charge_override_rate",
                "fieldtype": "Percent",
                "label": "Service Charge Override Rate",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_service_charge_waived",
            },
            {
                "fieldname": "custom_service_charge_override_amount",
                "fieldtype": "Currency",
                "label": "Service Charge Override Amount",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_service_charge_override_rate",
            },
            {
                "fieldname": "custom_service_charge_reason",
                "fieldtype": "Small Text",
                "label": "Service Charge Reason",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_service_charge_override_amount",
            },
            {
                "fieldname": "custom_service_charge_approver",
                "fieldtype": "Link",
                "options": "User",
                "length": 80,
                "label": "Service Charge Approver",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_service_charge_reason",
            },
            # Note: POS Invoice-custom_charged_tip and POS Invoice-custom_tip_server are
            # canonically defined in ury/fixtures/custom_field.json (#145). Do not duplicate them here.
            {
                "fieldname": "custom_tip_adjustment_je",
                "fieldtype": "Link",
                "options": "Journal Entry",
                "label": "Tip Adjustment Journal Entry",
                "length": 64,
                "read_only": 1,
                "allow_on_submit": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_service_charge_approver",
            },
            {
                "fieldname": "custom_bill_print_count",
                "fieldtype": "Int",
                "label": "Bill Print Count",
                "default": "0",
                "read_only": 1,
                "allow_on_submit": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "invoice_printed",
            },
            {
                "fieldname": "custom_kot_reprint_count",
                "fieldtype": "Int",
                "label": "KOT Reprint Count",
                "default": "0",
                "read_only": 1,
                "allow_on_submit": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_bill_print_count",
            },
        ],
        "POS Invoice Item": [
            {
                "fieldname": "custom_original_price_list_rate",
                "fieldtype": "Currency",
                "label": "Original Price List Rate",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "price_list_rate",
            },
            {
                "fieldname": "custom_price_overridden",
                "fieldtype": "Check",
                "label": "Price Overridden",
                "default": "0",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_original_price_list_rate",
            },
            {
                "fieldname": "custom_price_override_reason",
                "fieldtype": "Small Text",
                "label": "Price Override Reason",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_price_overridden",
            },
            {
                "fieldname": "custom_price_override_approver",
                "fieldtype": "Link",
                "options": "User",
                "label": "Price Override Approver",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_price_override_reason",
            },
            {
                "fieldname": "custom_is_open_item",
                "fieldtype": "Check",
                "label": "Open Item",
                "default": "0",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_price_override_approver",
            },
            {
                "fieldname": "custom_open_item_reference",
                "fieldtype": "Data",
                "label": "Open Item Reference",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_is_open_item",
            },
            {
                "fieldname": "custom_is_complimentary",
                "fieldtype": "Check",
                "label": "Complimentary Line",
                "default": "0",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_open_item_reference",
            },
            {
                "fieldname": "custom_comp_type",
                "fieldtype": "Select",
                "options": "\nComplimentary\nHouse\nNon-Revenue",
                "label": "Line Comp Type",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "custom_is_complimentary",
            },
            {
                "fieldname": "custom_comp_reason",
                "fieldtype": "Small Text",
                "label": "Line Comp Reason",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_comp_type",
            },
            {
                "fieldname": "custom_comp_approver",
                "fieldtype": "Link",
                "options": "User",
                "label": "Line Comp Approver",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_comp_reason",
            },
            {
                "fieldname": "custom_efris_tax_category",
                "fieldtype": "Data",
                "label": "EFRIS Tax Category",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "insert_after": "custom_comp_approver",
            },
        ],
        "Tax Category": [
            {
                "fieldname": "custom_efris_tax_category",
                "fieldtype": "Select",
                "options": "\n01\n02\n03\n04",
                "label": "EFRIS Tax Category",
                "description": "URA EFRIS tax category code (01 Standard, 02 Zero-rated, 03 Exempt, 04 Deemed).",
                "insert_after": "disabled",
            },
        ],
        "Item Tax Template": [
            {
                "fieldname": "custom_efris_tax_category",
                "fieldtype": "Select",
                "options": "\n01\n02\n03\n04",
                "label": "EFRIS Tax Category",
                "description": "URA EFRIS tax category code (01 Standard, 02 Zero-rated, 03 Exempt, 04 Deemed).",
                "insert_after": "disabled",
            },
        ],
        "Sales Taxes and Charges": [
            {
                "fieldname": "custom_pos_charge_kind",
                "fieldtype": "Select",
                "options": "\nService Charge\nService Charge Tax\nCharged Tip",
                "label": "POS Charge Kind",
                "hidden": 1,
                "no_copy": 0,
                "print_hide": 1,
                "insert_after": "charge_type",
            },
        ],
        "URY Table Reservation": [
            {
                "fieldname": "custom_cancel_approver",
                "fieldtype": "Link",
                "options": "User",
                "label": "Cancellation Approver",
                "read_only": 1,
                "no_copy": 1,
                "insert_after": "cancel_reason",
            },
        ],
    }


def ensure_order_type_options() -> None:
    """Ensure `Room Service` is an allowed Select option on POS/Sales Invoice `order_type`."""
    for dt in ("POS Invoice", "Sales Invoice"):
        cf_name = f"{dt}-order_type"
        if not frappe.db.exists("Custom Field", cf_name):
            continue
        current = frappe.db.get_value("Custom Field", cf_name, "options") or ""
        lines = current.splitlines()
        if "Room Service" not in lines:
            updated = (
                f"{current}\nRoom Service"
                if current
                else "\nDine In\nTake Away\nDelivery\nPhone In\nRoom Service\nAggregators"
            )
            frappe.db.set_value("Custom Field", cf_name, "options", updated, update_modified=False)
            clear_cache = getattr(frappe, "clear_cache", None)
            if callable(clear_cache):
                clear_cache(doctype=dt)


def ensure_privileged_action_custom_fields() -> None:
    fields = get_privileged_action_custom_fields()
    filtered = {
        dt: rows
        for dt, rows in fields.items()
        if frappe.db.exists("DocType", dt)
    }
    if filtered:
        create_custom_fields(filtered, ignore_validate=True)
    ensure_order_type_options()
