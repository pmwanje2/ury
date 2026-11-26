"""Voids on persisted Add Check allocations, including tender and kitchen effects."""

import unittest
from datetime import datetime
from unittest.mock import patch

import frappe

from ury.ury.api import ury_pos_invoice_qty_reduction as reduction
from ury.ury.doctype.ury_order import test_pos_pr9_r14 as r14


class TestAllocatedVoids(unittest.TestCase):
    def setUp(self):
        self.floor = r14.TestSplitOwnership()
        self.floor.setUp()
        self.addCleanup(self.floor.doCleanups)
        self.checks = self.floor.fx

    def merged_soups(self, currency, places, fees=None):
        first, later = self.checks.fixture.priced_checks(currency, places, siblings=True, fees=fees)
        for doc, minute in ((first, 0), (later, 30)):
            doc.items[0].update(dict(ury_held=1, reservation_line_key="soup",
                                     fire_at=datetime(2026, 11, 25, 19, minute)))
        self.checks.merge(first, later)
        first.branch = "Test Branch"
        first.order_type = "Dine In"
        return self.checks.reload(first)

    def void(self, doc, row_name):
        original = frappe.get_doc
        profile = frappe._dict(name="POS", custom_qty_reduction_allowed_order_types="Dine In",
                               custom_kot_naming_series="KOT-.####")

        def get_doc(dt, *args, **kwargs):
            if dt == "POS Invoice":
                return doc
            if dt == "POS Profile":
                return profile
            return original(dt, *args, **kwargs)

        def save(**kwargs):
            doc.calculate_taxes_and_totals()
            self.floor.persist(doc)

        doc.save = save
        with (patch.object(frappe, "get_doc", get_doc),
              patch.object(frappe, "has_permission", return_value=True),
              patch("ury.ury.api.ury_availability.apply_count_delta"),
              patch("ury.ury.api.ury_stock_policy.get_branch_stock_policy",
                    return_value=frappe._dict(reservation_control_enabled=0))):
            return reduction.reduce_order_item_qty(doc.name, row_name, 0, reason="Guest", approval_token="token")

    def test_void_fired_soup_after_remerge(self):
        for currency, places in self.checks.fixture.currencies:
            with self.subTest(currency=currency):
                doc = self.floor.remerge(self.merged_soups(currency, places))
                first, later = doc.items[:2]
                self.assertEqual([r["qty"] for r in self.floor.fire(doc, first)], [1])
                doc.reload()
                self.assertEqual((doc.grand_total, doc.items[0].ury_held, doc.items[1].ury_held), (215, 0, 1))
                reason = dict(code="Guest", display="Guest", note="", is_waste=False)
                with (patch.object(reduction, "resolve_pos_reason", return_value=reason),
                      patch.object(reduction, "require_pos_override", return_value="Manager") as approval,
                      patch.object(reduction, "process_items_for_cancel_kot", return_value=["cancel"]) as kitchen):
                    result = self.void(doc, first.name)
                self.assertEqual(approval.call_args.args[0], "void_sent_item")
                self.assertEqual(approval.call_args.args[4]["item_row_name"], first.name)
                self.assertEqual(approval.call_args.kwargs["approval_token"], "token")
                self.assertEqual((result["approver"], result["cancel_kot_names"]), ("Manager", ["cancel"]))
                self.assertEqual([(r["item_code"], r["qty"]) for r in kitchen.call_args.args[3]], [("Soup", -1)])
                doc.reload()
                self.assertEqual((doc.grand_total, [r.item_code for r in doc.items]), (115, ["Soup", "Tea"]))
                self.assertEqual([(r.name, r.fire_at.minute) for r in doc.items if r.ury_held], [(later.name, 30)])

    def test_tender_compared_to_surviving_fee_allocation(self):
        for currency, places in self.checks.fixture.currencies:
            with self.subTest(currency=currency):
                doc = self.merged_soups(currency, places, fees=(-20, 20))
                first, later = doc.items
                self.assertEqual(doc.grand_total, 200)
                before = doc.custom_split_balance
                payment = doc.append("payments", dict(mode_of_payment="Cash", amount=125, custom_tender_received=1))
                payment.db_insert()
                with self.assertRaises(reduction.QtyReductionError) as refused:
                    self.void(doc, first.name)
                self.assertEqual(refused.exception.reason_code, reduction.CODE_VOID_PARTIAL_PAYMENT)
                doc.reload()
                self.assertEqual((doc.custom_split_balance, len(doc.items)), (before, 2))
                doc.payments[0].db_set("amount", 110)
                with patch.object(reduction, "process_items_for_cancel_kot") as kitchen:
                    result = self.void(doc, first.name)
                kitchen.assert_not_called()
                self.assertEqual(result["cancel_kot_names"], [])
                doc.reload()
                self.assertEqual((doc.grand_total, doc.total_taxes_and_charges, doc.payments[0].amount), (120, 20, 110))
                self.assertEqual([(r.name, r.ury_held) for r in doc.items], [(later.name, 1)])
