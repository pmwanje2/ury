"""Review regressions for split tax, stock posting and kitchen ownership."""

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.controllers.selling_controller import SellingController
from erpnext.controllers.taxes_and_totals import process_item_wise_tax_details

from ury.ury.doctype.ury_order import ury_order as order
from ury.ury_pos.split_balances import (
    allocate_split_balances,
    get_saved_split_balance,
    restore_split_balance,
    set_split_balance,
)


def _check(name, **fields):
    doc = frappe.get_doc({
        "doctype": "POS Invoice", "name": name, "branch": "Split Outlet",
        "company": "Split Company", "currency": "USD", "conversion_rate": 0.5,
        **fields,
    })
    doc.set_paid_amount = lambda: None
    doc.set_outstanding_amount = lambda: None
    return doc


def _line(name, *, qty=1, stock_qty=1, amount=10, item_code="Soup", **fields):
    return {
        "name": name, "qty": qty, "stock_qty": stock_qty, "item_code": item_code,
        "conversion_factor": stock_qty / qty, "amount": amount, "net_amount": amount,
        "base_amount": amount / 2, "base_net_amount": amount / 2, **fields,
    }


class TestPOSPR9R3(unittest.TestCase):
    def setUp(self):
        self.savepoint = "pos_pr9_r3"
        frappe.db.savepoint(self.savepoint)
        self.addCleanup(lambda: frappe.db.rollback(save_point=self.savepoint))

    def test_native_item_wise_tax_details_keep_exempt_check_tax_free(self):
        doc = _check(
            "TAX-ROOT", items=[_line("taxable"), _line("exempt")],
            net_total=20, base_net_total=10, grand_total=21.80, base_grand_total=10.9,
            total_taxes_and_charges=1.80, base_total_taxes_and_charges=0.9,
            taxes=[{
                "name": "vat-row", "account_head": "VAT",
                "tax_amount": 1.80, "base_tax_amount": 0.9,
                "tax_amount_after_discount_amount": 1.80,
                "base_tax_amount_after_discount_amount": 0.9,
            }],
            item_wise_tax_details=[
                {"item_row": "taxable", "tax_row": "vat-row", "rate": 18, "amount": 0.9, "taxable_amount": 5},
                {"item_row": "exempt", "tax_row": "vat-row", "rate": 0, "amount": 0, "taxable_amount": 5},
            ],
        )
        taxable, exempt = allocate_split_balances(doc, {"exempt": 1}, 2, 3)
        self.assertEqual((taxable["header"]["grand_total"], exempt["header"]["grand_total"]), (11.80, 10.00))
        self.assertEqual((taxable["header"]["base_grand_total"], exempt["header"]["base_grand_total"]), (5.9, 5.0))
        self.assertEqual(taxable["details"][0]["amount"], 1.80)
        self.assertEqual(taxable["details"][0]["base_amount"], 0.9)
        self.assertEqual(exempt["details"][0]["amount"], 0)

        child = _check("TAX-CHILD", items=[_line("new-taxable")], taxes=[
            {"name": "new-vat-row", "account_head": "VAT"},
        ])
        set_split_balance(child, taxable, ["taxable"])
        restore_split_balance(child, get_saved_split_balance(child))
        process_item_wise_tax_details(child)
        detail = frappe.db.get_value("Item Wise Tax Detail", {"parent": child.name}, [
            "item_row", "tax_row", "rate", "amount", "taxable_amount",
        ])
        self.assertEqual(detail[:3], ("new-taxable", "new-vat-row", 18))
        self.assertEqual(tuple(Decimal(str(value)) for value in detail[3:]), (Decimal("0.900"), Decimal("5.000")))

    def test_converted_split_quantities_conserve_posted_ledger_units(self):
        doc = _check("STOCK-ROOT", items=[_line("stock-line", stock_qty=0.5)])
        native_precision = frappe.get_precision

        def precision(doctype, field):
            if (doctype, field) == ("Stock Ledger Entry", "actual_qty"):
                return 3
            return native_precision(doctype, field)

        posting = SimpleNamespace(
            is_internal_transfer=lambda: False, is_return=0, docstatus=1,
            get_sl_entries=lambda _row, fields: frappe._dict(fields),
        )
        with patch.object(frappe, "get_precision", side_effect=precision):
            remaining, first = allocate_split_balances(doc, {"stock-line": 0.3333}, 2, 3)
            doc.items[0].qty = 0.6667
            doc.items[0].stock_qty = remaining["items"]["stock-line"]["stock_qty"]
            remaining, second = allocate_split_balances(doc, {"stock-line": 0.3333}, 2, 3)

            entry_names = []
            for snapshot in (remaining, first, second):
                entry = SellingController.get_sle_for_source_warehouse(posting, frappe._dict(
                    qty=snapshot["items"]["stock-line"]["stock_qty"],
                ))
                name = frappe.generate_hash(length=12)
                frappe.get_doc({"doctype": "Stock Ledger Entry", "name": name, **entry}).db_insert()
                entry_names.append(name)

        posted = frappe.get_all(
            "Stock Ledger Entry", filters={"name": ["in", entry_names]}, pluck="actual_qty",
        )
        self.assertCountEqual([Decimal(str(qty)) for qty in posted], [
            Decimal("-0.166"), Decimal("-0.167"), Decimal("-0.167"),
        ])
        self.assertEqual(sum(Decimal(str(qty)) for qty in posted), Decimal("-0.500"))

    def test_add_check_reunites_shared_line_and_keeps_direct_discard_guard(self):
        for reverse in (False, True):
            with self.subTest(merge_into_child=reverse):
                root = _check("MERGE-ROOT", items=[_line("original", stock_qty=0.5)], grand_total=10)
                left, right = allocate_split_balances(root, {"original": 0.5}, 2, 3)
                child = _check("MERGE-CHILD", grand_total=5, items=[
                    _line("copied", qty=0.5, stock_qty=0.25, amount=5),
                ])
                for doc, part in ((root, left), (child, right)):
                    doc.items[0].qty = 0.5
                    doc.items[0].custom_origin_invoice_item = "original"
                    doc.items[0].custom_kitchen_anchor = root.name
                    doc.custom_split_group = "shared"
                    set_split_balance(doc, part, ["original"])
                    doc.calculate_taxes_and_totals = lambda doc=doc: restore_split_balance(
                        doc, get_saved_split_balance(doc),
                    )
                    doc.save = lambda: None
                    doc.db_set = lambda field, value: doc.set(field, value)
                destination, source = (child, root) if reverse else (root, child)
                source.discard = lambda: order.assert_no_shared_kitchen_void(source, method="before_discard")
                ticket = {"invoice": root.name}

                def set_value(doctype, name, field, value=None, **kwargs):
                    if (doctype, name, field) == ("URY KOT", "KOT-1", "invoice"):
                        ticket["invoice"] = value

                with patch.multiple(
                    order, _prepare_consolidated_discount=lambda *args: None,
                    _validate_and_collect_source_tenders=lambda *args: ([], None),
                    get_branch_stock_policy=lambda **kwargs: SimpleNamespace(reservation_control_enabled=False),
                ), patch.object(frappe, "get_all", side_effect=lambda dt, filters, **kw: [
                    "KOT-1",
                ] if dt == "URY KOT" and filters["invoice"] == ticket["invoice"] else []), patch.object(
                    frappe.db, "set_value", side_effect=set_value,
                ):
                    with self.assertRaisesRegex(frappe.ValidationError, "SPLIT-KITCHEN-SHARED"):
                        source.discard()
                    order.consolidate_checks(destination, [source])
                self.assertEqual(ticket["invoice"], destination.name)
                self.assertEqual(len(destination.items), 1)
                self.assertEqual(destination.items[0].qty, 1)
                self.assertEqual(destination.grand_total, 10)
                self.assertEqual(destination.items[0].stock_qty, 0.5)
                self.assertFalse(destination.items[0].get("custom_kitchen_anchor"))

    def test_unkeyed_and_overlapping_kots_anchor_items_without_stock_reservations(self):
        check = _check("KITCHEN-ROOT", items=[
            _line("a", item_code="Soup", reservation_line_key="soup"),
            _line("b", item_code="Stew", reservation_line_key="stew"),
        ])
        a = {"item": "Soup", "reservation_line_key": "soup", "quantity": 1}
        b = {"item": "Stew", "reservation_line_key": "stew", "quantity": 1}
        for tickets in (
            {"legacy": [{"item": "Soup", "quantity": 1}]},
            {"first": [a], "second": [a, b]},
        ):
            with self.subTest(tickets=list(tickets)), patch.object(
                frappe, "get_all", side_effect=lambda dt, filters, **kwargs:
                list(tickets) if dt == "URY KOT" else tickets[filters["parent"]],
            ):
                anchored = set()
                self.assertEqual(order._split_kots_to_move(check, {"a": 1}, False, anchored=anchored), [])
                self.assertIn("a", anchored)
