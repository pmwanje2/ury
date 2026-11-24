"""Idempotent patch adding POS-PR-6 custom fields for:
- G25: Mode of Payment reference capture configuration
- G22: Foreign currency tender, FX vault account, Sales Invoice Payment FX snapshot,
       currency-wise POS Closing Entry Detail, and POS Closing Entry FX sweep JE link
- G46: POS Profile offline peer relay URL & collision policy, POS Invoice offline conflict flag
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


CUSTOM_FIELDS = {
    "Mode of Payment": [
        {
            "fieldname": "custom_reference_mode",
            "fieldtype": "Select",
            "label": "Payment Reference Mode",
            "options": "\nHidden\nOptional\nRequired",
            "default": "Hidden",
            "insert_after": "type",
            "description": "Controls whether POS prompts for a payment reference / auth code for this tender.",
        },
        {
            "fieldname": "custom_reference_label",
            "fieldtype": "Data",
            "label": "Payment Reference Label",
            "insert_after": "custom_reference_mode",
            "depends_on": "eval:doc.custom_reference_mode && doc.custom_reference_mode != 'Hidden'",
            "translatable": 0,
        },
        {
            "fieldname": "custom_reference_placeholder",
            "fieldtype": "Data",
            "label": "Payment Reference Placeholder",
            "insert_after": "custom_reference_label",
            "depends_on": "eval:doc.custom_reference_mode && doc.custom_reference_mode != 'Hidden'",
            "translatable": 0,
        },
        {
            "fieldname": "custom_reference_pattern",
            "fieldtype": "Data",
            "label": "Payment Reference Pattern (Regex)",
            "insert_after": "custom_reference_placeholder",
            "depends_on": "eval:doc.custom_reference_mode && doc.custom_reference_mode != 'Hidden'",
            "description": "Optional full-match regex pattern (max 128 chars) validated on server and POS.",
            "translatable": 0,
        },
        {
            "fieldname": "custom_reference_date_required",
            "fieldtype": "Check",
            "label": "Reference Date Required",
            "default": "0",
            "insert_after": "custom_reference_pattern",
            "depends_on": "eval:doc.custom_reference_mode && doc.custom_reference_mode != 'Hidden'",
        },
        {
            "fieldname": "custom_tender_currency",
            "fieldtype": "Link",
            "label": "Tender Currency",
            "options": "Currency",
            "insert_after": "custom_reference_date_required",
            "description": "Leave blank for home currency. When set, POS accepts tender in this currency and sweeps to custom_fx_vault_account at close.",
        },
        {
            "fieldname": "custom_fx_rate_type",
            "fieldtype": "Select",
            "label": "FX Rate Type",
            "options": "Buying\nMid/Selling",
            "default": "Buying",
            "insert_after": "custom_tender_currency",
            "depends_on": "eval:doc.custom_tender_currency",
        },
    ],
    "Mode of Payment Account": [
        {
            "fieldname": "custom_fx_vault_account",
            "fieldtype": "Link",
            "label": "FX Vault Account",
            "options": "Account",
            "insert_after": "default_account",
            "in_list_view": 1,
            "description": "Per-company foreign currency cash/bank account (in Tender Currency). default_account stays in POS Profile currency as FX Till Clearing.",
        },
    ],
    "POS Profile": [
        {
            "fieldname": "custom_allow_fx_rate_override",
            "fieldtype": "Check",
            "label": "Allow Manager FX Rate Override",
            "default": "0",
            "insert_after": "custom_override_lock_minutes",
            "description": "Allow a manager with the fx_rate_override PIN privilege to override an FX rate at tender time.",
        },
        {
            "fieldname": "custom_max_fx_variance_pct",
            "fieldtype": "Percent",
            "label": "Max FX Override Variance (%)",
            "default": "0",
            "insert_after": "custom_allow_fx_rate_override",
            "depends_on": "eval:doc.custom_allow_fx_rate_override == 1",
            "description": "Maximum allowed percentage deviation from the fresh Currency Exchange rate (0 = no variance cap).",
        },
        {
            "fieldname": "custom_fx_clearing_loss_account",
            "fieldtype": "Link",
            "label": "FX Clearing Gain/Loss Account",
            "options": "Account",
            "insert_after": "custom_max_fx_variance_pct",
            "description": "Optional account for sub-minor-unit FX rounding differences at till close (defaults to Company Exchange Gain/Loss Account).",
        },
        {
            "fieldname": "custom_offline_peer_relay_url",
            "fieldtype": "Data",
            "label": "Offline Peer Relay URL",
            "insert_after": "ury_offline_capture",
            "depends_on": "eval:doc.ury_offline_capture == 1",
            "description": "Optional LAN relay URL (http(s):// or ws(s)://) for cross-device waiter offline outbox visibility.",
            "translatable": 0,
        },
        {
            "fieldname": "custom_offline_collision_policy",
            "fieldtype": "Select",
            "label": "Offline Table Collision Policy",
            "options": "Auto Split Check\nMerge Rounds",
            "default": "Auto Split Check",
            "insert_after": "custom_offline_peer_relay_url",
            "depends_on": "eval:doc.ury_offline_capture == 1",
            "description": "How to reconcile an offline waiter order when another open check already exists on the same table.",
        },
    ],
    "Sales Invoice Payment": [
        {
            "fieldname": "custom_tender_currency",
            "fieldtype": "Link",
            "label": "Tender Currency",
            "options": "Currency",
            "insert_after": "amount",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_tender_amount",
            "fieldtype": "Float",
            "label": "Tender Amount (Foreign)",
            "precision": "4",
            "insert_after": "custom_tender_currency",
            "no_copy": 1,
        },
        {
            "fieldname": "custom_exchange_rate",
            "fieldtype": "Float",
            "label": "Tender Exchange Rate",
            "precision": "9",
            "insert_after": "custom_tender_amount",
            "read_only": 1,
            "no_copy": 1,
        },
        {
            "fieldname": "custom_exchange_rate_date",
            "fieldtype": "Date",
            "label": "Tender Exchange Rate Date",
            "insert_after": "custom_exchange_rate",
            "read_only": 1,
            "no_copy": 1,
        },
    ],
    "POS Closing Entry Detail": [
        {
            "fieldname": "custom_tender_currency",
            "fieldtype": "Link",
            "label": "Tender Currency",
            "options": "Currency",
            "insert_after": "mode_of_payment",
            "read_only": 1,
        },
        {
            "fieldname": "custom_expected_foreign_amount",
            "fieldtype": "Float",
            "label": "Expected (Foreign)",
            "precision": "4",
            "insert_after": "custom_tender_currency",
            "read_only": 1,
        },
        {
            "fieldname": "custom_closing_foreign_amount",
            "fieldtype": "Float",
            "label": "Closing (Foreign)",
            "precision": "4",
            "insert_after": "custom_expected_foreign_amount",
        },
        {
            "fieldname": "custom_foreign_difference",
            "fieldtype": "Float",
            "label": "Difference (Foreign)",
            "precision": "4",
            "insert_after": "custom_closing_foreign_amount",
            "read_only": 1,
        },
    ],
    "POS Closing Entry": [
        {
            "fieldname": "custom_fx_vault_journal_entry",
            "fieldtype": "Link",
            "label": "FX Vault Sweep Journal Entry",
            "options": "Journal Entry",
            "insert_after": "branch",
            "read_only": 1,
            "no_copy": 1,
            "allow_on_submit": 1,
        },
    ],
    "POS Invoice": [
        {
            "fieldname": "custom_offline_conflict",
            "fieldtype": "Check",
            "label": "Offline Collision Reconciled",
            "default": "0",
            "insert_after": "ury_offline_captured_at",
            "read_only": 1,
            "no_copy": 1,
        },
    ],
}


def execute():
    create_custom_fields(CUSTOM_FIELDS, update=True)
    # Seed default fx_rate_override privilege rules for existing companies if override module is available.
    try:
        from ury.ury_pos.override import ensure_company_rules

        for company in frappe.get_all("Company", pluck="name") or []:
            ensure_company_rules(company)
    except Exception:
        pass
