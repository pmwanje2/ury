"""Regression coverage for Add Check bussing and native split precision."""

import unittest
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.controllers.selling_controller import SellingController

from ury.ury.api import table_lifecycle
from ury.ury.doctype.ury_order import ury_order
from ury.ury_pos.split_balances import allocate_split_balances


def _insert(doctype, name, **fields):
    doc = frappe.get_doc({"doctype": doctype, "name": name, **fields})
    doc.set_user_and_timestamp()
    doc.db_insert()
    for child in doc.get_all_children():
        child.db_insert()
    return doc


def _invoice(name, **fields):
    return frappe.get_doc({"doctype": "POS Invoice", "name": name, "conversion_rate": 0.5, **fields})


class TestPOSPR9R4(unittest.TestCase):
    def setUp(self):
        savepoint = "pos_pr9_r4"
        frappe.db.savepoint(savepoint)
        self.addCleanup(lambda: frappe.db.rollback(save_point=savepoint))

    def test_add_check_keeps_the_dirty_state_and_timestamp_set_by_discard(self):
        dirty_since = datetime(2026, 1, 12, 14, 30)
        branch = _insert("Branch", "R4 Test Branch", branch="R4 Test Branch",
                         custom_enable_bussing_state=1, custom_block_dirty_table_seating=1,
                         custom_table_release_trigger=table_lifecycle.ON_SETTLEMENT)
        room = _insert("URY Room", "R4 Test Room", branch=branch.name)
        tables = [
            _insert("URY Table", f"R4 Table {number}", branch=branch.name, restaurant_room=room.name,
                    occupied=1, custom_table_state="Seated")
            for number in (1, 2)
        ]
        for number, table in enumerate(tables, 1):
            _insert("POS Invoice", f"R4 Check {number}", branch=branch.name, company="R4 Test Company",
                    restaurant_table=table.name, items=[{"name": f"R4 Line {number}", "item_code": "Soup", "qty": 1}])
        destination, source = [frappe.get_doc("POS Invoice", f"R4 Check {number}") for number in (1, 2)]
        destination.save = lambda: None
        destination.calculate_taxes_and_totals = lambda: None
        destination.set_paid_amount = lambda: None
        destination.set_outstanding_amount = lambda: None

        with (
            patch.dict(frappe.conf, developer_mode=True),
            patch.object(frappe.local, "doc_events_hooks", None, create=True),
            patch.object(frappe.db, "commit"),
            patch.object(table_lifecycle, "now_datetime", return_value=dirty_since),
            patch.object(ury_order, "_validate_and_collect_source_tenders", return_value=([], None)),
            patch.object(ury_order, "get_branch_stock_policy",
                         return_value=SimpleNamespace(reservation_control_enabled=False)),
        ):
            ury_order.consolidate_checks(destination, [source])
        self.assertEqual(frappe.db.get_value("POS Invoice", source.name, "docstatus"), 2)
        self.assertEqual(frappe.db.get_value("URY Table", tables[1].name, [
            "custom_table_state", "custom_dirty_since", "occupied",
        ]), ("Dirty", dirty_since, 0))
        with self.assertRaisesRegex(frappe.ValidationError, "TABLE-DIRTY-NEEDS-BUSSING"):
            table_lifecycle.assert_table_seatable(tables[1].name, branch.name)
        self.assertEqual(frappe.db.get_value("URY Table", tables[0].name, "custom_table_state"), "Seated")

    def test_quantity_tax_uses_transaction_weights_before_fx_rounding(self):
        check = _invoice(
            "R4 Tax Check", currency="USD", net_total=101, base_net_total=50.5,
            total_taxes_and_charges=0.02, base_total_taxes_and_charges=0.01,
            grand_total=101.02, base_grand_total=50.51,
            items=[
                {"name": "small", "item_code": "Soup", "qty": 1, "net_amount": 1, "base_net_amount": 0.5},
                {"name": "large", "item_code": "Stew", "qty": 1, "net_amount": 100, "base_net_amount": 50},
            ],
            taxes=[{
                "name": "quantity-tax", "charge_type": "On Item Quantity", "account_head": "Per-unit Tax",
                "tax_amount": 0.02, "tax_amount_after_discount_amount": 0.02,
                "base_tax_amount": 0.01, "base_tax_amount_after_discount_amount": 0.01,
            }],
            item_wise_tax_details=[
                {"item_row": "small", "tax_row": "quantity-tax", "rate": 0.01, "amount": 0.01},
                {"item_row": "large", "tax_row": "quantity-tax", "rate": 0.01, "amount": 0},
            ],
        )

        source, child = allocate_split_balances(check, {"large": 1}, 2, 2)
        self.assertEqual([part["taxes"][0]["tax_amount_after_discount_amount"] for part in (source, child)],
                         [0.01, 0.01])
        self.assertEqual([part["details"][0]["amount"] for part in (source, child)], [0.01, 0.01])
        self.assertEqual([part["details"][0]["base_amount"] for part in (source, child)], [0.01, 0])
        self.assertEqual([part["header"]["grand_total"] for part in (source, child)], [1.01, 100.01])

    def test_six_decimal_stock_ledger_conserves_the_original_quantity(self):
        check = _invoice("R4 Stock Check", items=[
            {"name": "stock", "item_code": "Soup", "qty": 1, "stock_qty": 0.333333},
        ])
        get_default = frappe.db.get_default
        posting = SimpleNamespace(
            is_internal_transfer=lambda: False, is_return=0, docstatus=1,
            get_sl_entries=lambda _row, fields: frappe._dict(fields),
        )

        def stock_default(key, *args, **kwargs):
            return 6 if key == "float_precision" else get_default(key, *args, **kwargs)

        with patch.object(frappe.db, "get_default", side_effect=stock_default):
            self.assertEqual(frappe.get_precision("Stock Ledger Entry", "actual_qty"), 6)
            parts = allocate_split_balances(check, {"stock": 0.5}, 2, 2)
            ledger_names = []
            for part in parts:
                stock_qty = part["items"]["stock"]["stock_qty"]
                fields = SellingController.get_sle_for_source_warehouse(posting, frappe._dict(qty=stock_qty))
                entry = _insert("Stock Ledger Entry", frappe.generate_hash(length=12), **fields)
                ledger_names.append(entry.name)
        quantities = [Decimal(str(row.actual_qty)) for row in frappe.get_all(
            "Stock Ledger Entry", filters={"name": ["in", ledger_names]}, fields=["actual_qty"],
        )]
        self.assertCountEqual(quantities, [Decimal("-0.166667"), Decimal("-0.166666")])
        self.assertEqual(sum(quantities), Decimal("-0.333333"))
