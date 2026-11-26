"""Regression coverage for chained taxes, discount edits, and dirty seating."""

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.pos_invoice.pos_invoice import POSInvoice

from ury.ury.api import table_reservation
from ury.ury.class_overrides.pos_invoice import CustomPOSInvoice
from ury.ury.doctype.ury_order import ury_order
from ury.ury_pos.split_balances import allocate_split_balances, set_split_balance


def _insert(doctype, name, **fields):
    doc = frappe.get_doc({"doctype": doctype, "name": name, **fields})
    doc.set_user_and_timestamp()
    doc.db_insert()
    for child in doc.get_all_children():
        child.db_insert()
    return doc


class TestPOSPR9R5(unittest.TestCase):
    def setUp(self):
        savepoint = "pos_pr9_r5"
        frappe.db.savepoint(savepoint)
        self.addCleanup(lambda: frappe.db.rollback(save_point=savepoint))

    def test_chained_tax_uses_transaction_weights_before_fx_rounding(self):
        for currency, places in (("UGX", 0), ("USD", 2), ("KWD", 3)):
            for charge_type, taxable_units in (("On Previous Row Amount", 1), ("On Previous Row Total", 3)):
                with self.subTest(currency=currency, charge_type=charge_type):
                    unit = Decimal(1).scaleb(-places)
                    rows = [frappe._dict(name=name, qty=1, net_amount=2 * unit, base_net_amount=unit)
                            for name in ("item-a", "item-b")]
                    check = frappe._dict(
                        items=rows, currency=currency, conversion_rate=0.5, net_total=4 * unit,
                        total_taxes_and_charges=4 * unit, grand_total=8 * unit,
                    )
                    check.taxes = [
                        frappe._dict(name="quantity", charge_type="On Item Quantity", idx=1),
                        frappe._dict(name="chain", charge_type=charge_type, idx=2, row_id=1),
                    ]
                    for tax in check.taxes:
                        tax.update(tax_amount_after_discount_amount=2 * unit,
                                   base_tax_amount_after_discount_amount=unit)
                    base_taxable_units = ((taxable_units + 1) // 2, taxable_units // 2)
                    check.item_wise_tax_details = [
                        frappe._dict(
                            item_row=row.name, tax_row=tax.name, amount=unit if index == 0 else 0,
                            rate=unit if tax.name == "quantity" else Decimal(100) / taxable_units,
                            taxable_amount=unit * base_taxable_units[index] if tax.name == "chain" else 0,
                        )
                        for tax in check.taxes for index, row in enumerate(rows)
                    ]
                    parts = allocate_split_balances(check, {"item-b": 1}, places, places)
                    for part in parts:
                        dues = [Decimal(str(row["tax_amount_after_discount_amount"])) for row in part["taxes"]]
                        self.assertEqual(dues, [unit, unit])
                        detail = next(row for row in part["details"] if row["tax"] == 1)
                        self.assertEqual(Decimal(str(detail["taxable_amount"])), unit * taxable_units)
                        self.assertEqual(Decimal(str(part["header"]["grand_total"])), 4 * unit)

    def test_explicit_discount_apply_and_remove_refreshes_saved_totals(self):
        profile = frappe._dict(name="R5 Till", company="R5 Company", currency="USD")

        def native_calculate(doc):
            doc.discount_amount = doc.additional_discount_percentage / 10
            doc.grand_total = doc.rounded_total = doc.base_grand_total = 10 - doc.discount_amount
            doc.items[0].stock_qty = 0.5

        def apply_discount(doc, _profile, spec, **kwargs):
            doc.additional_discount_percentage = 0 if spec["clear"] else spec["percent"]
            doc.calculate_taxes_and_totals()
            return {"grand_total": doc.grand_total}

        get_precision = frappe.get_precision

        def stock_precision(doctype, field):
            if (doctype, field) == ("Stock Ledger Entry", "actual_qty"):
                return 4
            return get_precision(doctype, field)

        with (
            patch.object(frappe, "get_precision", side_effect=stock_precision),
            patch.object(POSInvoice, "calculate_taxes_and_totals", native_calculate),
            patch.object(CustomPOSInvoice, "set_paid_amount"),
            patch.object(CustomPOSInvoice, "set_outstanding_amount"),
            patch.object(ury_order, "_enforce_order_access"),
            patch.object(ury_order, "_sync_service_charge"),
            patch.object(ury_order, "enforce_remove_discount_auth", return_value=None),
            patch.object(ury_order, "apply_discount_to_invoice", side_effect=apply_discount),
            patch.object(frappe, "get_cached_doc", return_value=profile),
        ):
            for initial, expected in ((0, 9), (10, 10)):
                with self.subTest(initial_discount=initial):
                    name = f"R5 Discount {initial}"
                    doc = CustomPOSInvoice({
                        "doctype": "POS Invoice", "name": name, "currency": "USD", "company": profile.company,
                        "pos_profile": profile.name, "conversion_rate": 1, "custom_split_group": "R5 Split",
                        "additional_discount_percentage": initial, "discount_amount": initial / 10,
                        "grand_total": 10 - initial / 10, "items": [{
                            "name": f"R5 Line {initial}", "item_code": "Soup", "qty": 1, "rate": 10,
                            "conversion_factor": 0.5, "stock_qty": 0.4999, "net_amount": 10,
                        }],
                    })
                    part, _ = allocate_split_balances(doc, {}, 2, 2)
                    set_split_balance(doc, part, [doc.items[0].name])
                    doc.set_user_and_timestamp()
                    doc.db_insert()
                    doc.items[0].db_insert()
                    bill = CustomPOSInvoice("POS Invoice", name)
                    bill.check_permission = lambda *_: None

                    def save_bill():
                        bill.calculate_taxes_and_totals()
                        bill.db_update()
                        bill.update_children()

                    bill.save = save_bill
                    with patch.object(frappe, "get_doc", return_value=bill):
                        if initial:
                            summary = ury_order.remove_check_discount(name, profile.name)
                        else:
                            summary = ury_order.apply_check_discount(name, profile.name, discount_percent=10)

                    self.assertEqual(summary["grand_total"], expected)
                    stored = CustomPOSInvoice("POS Invoice", name)
                    self.assertEqual(stored.grand_total, expected)
                    stored.calculate_taxes_and_totals()
                    self.assertEqual(stored.discount_amount, 10 - expected)
                    self.assertEqual(stored.grand_total, expected)
                    self.assertEqual(stored.items[0].stock_qty, 0.4999)

    def test_change_to_dine_in_cannot_seat_at_dirty_table(self):
        branch = _insert("Branch", "R5 Test Branch", branch="R5 Test Branch", custom_block_dirty_table_seating=1)
        table = _insert("URY Table", "R5 Dirty Table", branch=branch.name, occupied=0, custom_table_state="Dirty")
        bill = frappe.get_doc({
            "doctype": "POS Invoice", "name": "R5 Test Check", "branch": branch.name,
            "order_type": "Take Away", "pos_profile": "R5 Till",
        })
        bill.set_missing_values = lambda **kwargs: None
        bill.calculate_taxes_and_totals = lambda: None
        bill.add_comment = lambda *args: None
        bill.save = lambda: None
        profile = frappe._dict(custom_edit_order_type=1)
        with (
            patch.object(frappe, "get_doc", side_effect=[bill, profile]),
            patch.object(ury_order, "_enforce_order_access", return_value={}),
            patch.object(ury_order, "_dayparts_mod",
                         return_value=SimpleNamespace(resolve_menu_and_price_list=lambda *a, **k: {})),
            patch.object(ury_order, "_tax_mod",
                         return_value=SimpleNamespace(apply_order_type_tax=lambda *a, **k: {})),
            patch.object(table_reservation, "check_table_reservation", return_value=None),
            self.assertRaisesRegex(frappe.ValidationError, "TABLE-DIRTY-NEEDS-BUSSING"),
        ):
            ury_order.change_order_type(bill.name, "Dine In", table=table.name)
        self.assertEqual(bill.order_type, "Take Away")
        self.assertEqual(frappe.db.get_value("URY Table", table.name, "occupied"), 0)
