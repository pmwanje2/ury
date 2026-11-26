"""Native Add Check regression cases with a single shared kitchen origin."""

import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import frappe

from ury.ury.doctype.ury_order import test_pos_pr9_r12 as r12, ury_order as order
from ury.ury_pos import pricing_actions
from ury.ury_pos.split_balances import allocate_split_balances


class TestSameOriginChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = r12.TestAddCheckRoundTrip()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def merge(self, target, source):
        def save(**kwargs):
            for row in target.items:
                if not row.name:
                    row.set_new_name()
        target.save = save
        source.discard = lambda: None
        source.db_set = source.set
        with patch.multiple(order, _validate_and_collect_source_tenders=lambda *args: ([], None),
                            get_branch_stock_policy=lambda **kwargs: frappe._dict(reservation_control_enabled=0)):
            order.consolidate_checks(target, [source])
        target.save = target.calculate_taxes_and_totals

    def reload(self, doc):
        doc.db_insert()
        for row in [*doc.items, *doc.taxes]:
            row.db_insert()
        reloaded = frappe.get_doc("POS Invoice", doc.name)
        reloaded.set_paid_amount = reloaded.set_outstanding_amount = lambda: None
        return reloaded

    def test_signed_fees_follow_same_origin_units(self):
        for currency, places in self.fixture.currencies:
            with self.subTest(currency=currency):
                target, source = self.fixture.priced_checks(currency, places, fees=(20, -20), siblings=True)
                self.merge(target, source)
                for percent, total in ((10, 180), (0, 200)):
                    self.assertEqual(self.fixture.fixture.discount(target, percent), (total, 0))
                doc = self.reload(target)
                parts = allocate_split_balances(doc, {doc.items[-1].name: 1}, places, places)
                for prefix in ("", "base_"):
                    self.assertEqual([s["header"][prefix + "grand_total"] for s in parts], [120, 80])
                    self.assertEqual([s["header"][prefix + "total_taxes_and_charges"] for s in parts], [20, -20])

    def whole_comp(self, comp_target):
        for currency, places in self.fixture.currencies:
            with self.subTest(currency=currency):
                target, source = self.fixture.priced_checks(currency, places, siblings=True)
                comped = target if comp_target else source
                comped.save = comped.calculate_taxes_and_totals
                with patch.multiple(pricing_actions,
                                    _load_invoice_and_profile=lambda *a, **kw: (comped, frappe._dict(name="POS")),
                                    authorize_or_perform=lambda **kw: dict(approver="Manager", reason="Guest",
                                                                           reason_note="", via_pin=True),
                                    sync_service_charge=lambda *args: None):
                    pricing_actions.apply_complimentary(comped.name, scope="check", comp_type="House")
                self.assertEqual(comped.grand_total, 0)
                self.merge(target, source)
                doc = self.reload(target)
                self.assertEqual(doc.grand_total, 100)
                self.assertFalse(doc.custom_is_complimentary)
                # Move one unit from the guest who received the comp even if
                # a defective merge collapsed both guests into a single row.
                move = doc.items[0] if comp_target else doc.items[-1]
                parts = allocate_split_balances(doc, {move.name: 1}, places, places)
                self.assertEqual([s["header"]["grand_total"] for s in parts], [100, 0])
                self.assertEqual((move.custom_comp_type, move.custom_comp_reason, move.custom_comp_approver),
                                 ("House", "Guest", "Manager"))
                self.assertTrue(move.custom_is_complimentary)
                self.assertEqual(self.fixture.fixture.discount(doc, 10)[0], 90)
                self.assertEqual(self.fixture.fixture.discount(doc, 0)[0], 100)

    def test_comped_check_into_billable(self):
        self.whole_comp(False)

    def test_billable_check_into_comped(self):
        self.whole_comp(True)

    def timed_release(self, legacy):
        early = datetime(2026, 11, 25, 19)
        late = early.replace(minute=30)
        for currency, places in self.fixture.currencies:
            with self.subTest(currency=currency):
                target, source = self.fixture.priced_checks(currency, places, siblings=True)
                for doc, deadline in ((target, early), (source, late)):
                    doc.items[0].update(dict(reservation_line_key="soup-cart", ury_held=1, fire_at=deadline))
                self.merge(target, source)
                self.assertEqual(len(target.items), 2)
                if legacy:
                    target.items[1].reservation_line_key = target.items[0].reservation_line_key
                doc = self.reload(target)
                doc.save = Mock()
                profile = frappe._dict(branch=doc.branch, ury_enable_hold_fire=1)
                def get_doc(doctype, _name):
                    return doc if doctype == "POS Invoice" else profile
                with (patch.object(frappe, "get_doc", get_doc),
                      patch.object(frappe.db, "sql", return_value=[(doc.name,)]),
                      patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"),
                      patch.object(order, "_lock_hold_order"), patch.object(order, "_publish_held_updates"),
                      patch.object(order, "kot_execute") as kitchen,
                      patch.object(order, "now_datetime", side_effect=[early, early, late])):
                    order.fire_due_held_orders()
                    self.assertEqual([row["qty"] for row in kitchen.call_args.args[3]], [1])
                    self.assertFalse(doc.items[0].ury_held)
                    self.assertEqual((doc.items[1].ury_held, doc.items[1].fire_at), (1, late))
                    if not legacy:
                        self.assertNotEqual(doc.items[0].reservation_line_key, doc.items[1].reservation_line_key)
                    order.fire_due_held_orders()
                    self.assertEqual(kitchen.call_count, 1)
                    order.fire_due_held_orders()
                    self.assertEqual(kitchen.call_count, 2)
                    self.assertEqual([row["qty"] for row in kitchen.call_args.args[3]], [1])
                    self.assertFalse(doc.items[1].ury_held)

    def test_add_check_keeps_distinct_fire_keys_and_deadlines(self):
        self.timed_release(False)

    def test_scheduler_respects_deadlines_on_preexisting_duplicate_keys(self):
        self.timed_release(True)
