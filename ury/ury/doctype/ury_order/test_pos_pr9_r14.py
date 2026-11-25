"""Comp and held-line ownership after a persisted split/merge round trip."""

import json
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import frappe

from ury.ury.api import ury_order_reservation_service as stock, ury_pos_invoice_qty_reduction as reduction
from ury.ury.doctype.ury_order import test_pos_pr9_r13 as r13, ury_order as order
from ury.ury_pos import pricing_actions


class TestSplitOwnership(unittest.TestCase):
    def setUp(self):
        self.fx = r13.TestSameOriginChecks()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    @staticmethod
    def persist(doc):
        """Use native rows and snapshots without requiring a cash session."""
        doc.db_update()
        doc.update_child_table("items")
        doc.update_child_table("taxes")

    def split_comp(self, move_comp):
        for currency, places in self.fx.fixture.currencies:
            with self.subTest(currency=currency):
                target, source = self.fx.fixture.priced_checks(currency, places, siblings=True)
                source.save = source.calculate_taxes_and_totals
                with patch.multiple(pricing_actions,
                                    _load_invoice_and_profile=lambda *a, **kw: (source, frappe._dict(name="POS")),
                                    authorize_or_perform=lambda **kw: dict(approver="Manager", reason="Guest",
                                                                           reason_note="", via_pin=True),
                                    sync_service_charge=lambda *args: None):
                    pricing_actions.apply_complimentary(source.name, scope="check", comp_type="House")
                self.fx.merge(target, source)
                merged = self.fx.reload(target)
                merged.custom_discount_approver = "Manager"
                merged.custom_discount_approved_percent = 100
                child = frappe.get_doc(dict(doctype="POS Invoice", name="r14-child-" + currency,
                                            disable_rounded_total=1, ignore_pricing_rule=1))
                for doc in (merged, child):
                    doc.set_missing_values = doc.run_method = Mock()
                    doc.set_paid_amount = doc.set_outstanding_amount = Mock()
                child.insert = lambda: self.fx.reload(child)
                merged.save = lambda: self.persist(merged)
                moved = next(row for row in merged.items if bool(row.custom_is_complimentary) == move_comp)
                with (patch.object(frappe, "new_doc", return_value=child),
                      patch.object(order, "user_discount_limit", return_value=100)):
                    result = order._apply_check_split(merged, {moved.name: 1}, merged.company,
                                                      kitchen_plan=(False, [], set()))
                parent, child = (frappe.get_doc("POS Invoice", result[key]) for key in ("source_invoice", "new_invoice"))
                free, paid = (child, parent) if move_comp else (parent, child)
                self.assertEqual((free.grand_total, paid.grand_total), (0, 100))
                for half in (free, paid):
                    half.room_number = "Room 12"
                    half.flags.ury_privileged_action_verified = True
                pricing_actions.validate_privileged_pricing_mutations(paid)
                with self.assertRaisesRegex(frappe.ValidationError, "COMP-ROOM-CHARGE-FORBIDDEN"):
                    pricing_actions.validate_privileged_pricing_mutations(free)
                self.assertEqual((free.custom_is_complimentary, paid.custom_is_complimentary), (1, 0))
                self.assertEqual((free.custom_comp_type, free.custom_comp_reason, free.custom_comp_approver),
                                 ("House", "Guest", "Manager"))
                self.assertFalse(paid.custom_comp_approver)

    def test_comp_remains_on_parent(self):
        self.split_comp(False)

    def test_comp_moves_to_child(self):
        self.split_comp(True)

    def legacy_check(self):
        a, b = self.fx.fixture.priced_checks("USD", 2, siblings=True)
        for doc, minute in ((a, 0), (b, 30)):
            doc.items[0].update(dict(ury_held=1, reservation_line_key="k", fire_at=datetime(2026, 11, 25, 19, minute)))
        self.fx.merge(a, b)
        for row in a.items:
            row.reservation_line_key = "k"
        a.branch = "Test Branch"
        a.order_type = "Dine In"
        doc = self.fx.reload(a)
        for idx, row in enumerate(doc.items):
            context = {"reservation_line_key": "ref:Soup:k", "source_line_ref": "k", "warehouse": "Kitchen"}
            frappe.get_doc(dict(doctype="URY Stock Reservation", reservation_group=f"r14-{idx}",
                                branch=doc.branch, company=doc.company, order_ref=doc.name, warehouse="Kitchen",
                                top_level_item="Soup", component_item="Soup", status="Reserved", qty=1,
                                audit_log=json.dumps([dict(event="create", frozen_context=context)]))).db_insert()
        return doc

    def remerge(self, doc):
        third = frappe.copy_doc(doc)
        third.name = "r14-third"
        third.custom_split_balance = third.custom_split_group = None
        third.set("items", [])
        third.append("items", dict(name="r14-Tea", item_code="Tea", rate=15, price_list_rate=15,
                                   qty=1, conversion_factor=1))
        third.set_paid_amount = third.set_outstanding_amount = Mock()
        third.calculate_taxes_and_totals()
        third.discard = third.db_set = Mock()
        def save():
            for row in doc.items:
                if not row.name:
                    row.set_new_name()
            self.persist(doc)
        doc.save = save
        with (patch.object(order, "_validate_and_collect_source_tenders", return_value=([], None)),
              patch.object(order, "get_branch_stock_policy", return_value=frappe._dict(reservation_control_enabled=1))):
            order.consolidate_checks(doc, [third])
        return doc.reload()

    def history(self, doc, row):
        groups = stock._active_groups(doc.name, row.item_code, stock._line_key(row.item_code, row, 1))
        self.assertEqual(len(groups), 1)
        raw = frappe.db.get_value("URY Stock Reservation", {"reservation_group": groups[0]}, "audit_log")
        log = json.loads(raw)
        self.assertEqual(log[0]["frozen_context"]["reservation_line_key"], "ref:Soup:k")
        self.assertEqual(log[-1]["frozen_context"]["reservation_line_key"], stock._line_key(row.item_code, row, 1))
        return groups[0], log

    def fire(self, doc, row):
        doc.save = lambda **kw: self.persist(doc)
        with (patch.object(order, "_staff_held_order", return_value=(doc, None)),
              patch.object(order, "_publish_held_updates"), patch.object(order, "kot_execute") as kitchen):
            order.fire_items(doc.name, line=row.name)
            return kitchen.call_args.args[3]

    def test_manual_fire_refuses_unresolved_duplicate_keys(self):
        doc = self.legacy_check()
        with self.assertRaises(frappe.ValidationError):
            self.fire(doc, doc.items[0])
        self.assertTrue(all(row.ury_held for row in doc.reload().items))

    def test_manual_fire_after_remerge(self):
        doc = self.remerge(self.legacy_check())
        first, later = doc.items[:2]
        self.assertEqual([line["qty"] for line in self.fire(doc, first)], [1])
        doc.reload()
        self.assertEqual((doc.items[0].ury_held, doc.items[1].ury_held), (0, 1))
        self.assertEqual(doc.items[1].fire_at.minute, 30)
        self.assertNotEqual(self.history(doc, first)[0], self.history(doc, later)[0])

    def test_cancel_one_held_sibling_after_remerge(self):
        doc = self.remerge(self.legacy_check())
        first, later = doc.items[:2]
        first_group, _ = self.history(doc, first)
        later_before = self.history(doc, later)
        profile = frappe._dict(name="POS", custom_qty_reduction_allowed_order_types="Dine In")
        original = frappe.get_doc
        def get_doc(dt, *args, **kwargs):
            return doc if dt == "POS Invoice" else profile if dt == "POS Profile" else original(dt, *args, **kwargs)
        def save(**kwargs):
            doc.calculate_taxes_and_totals()
            self.persist(doc)
        doc.save = save
        with (patch.object(frappe, "get_doc", get_doc), patch.object(frappe, "has_permission", return_value=True),
              patch("ury.ury.api.ury_availability.apply_count_delta"),
              patch("ury.ury.api.ury_stock_policy.get_branch_stock_policy",
                    return_value=frappe._dict(reservation_control_enabled=1)),
              patch.object(stock, "resolve_production_context", return_value=frappe._dict(warehouse="Kitchen")),
              patch.object(stock, "_warehouse_for_context", return_value="Kitchen"),
              patch("ury.ury.doctype.ury_stock_reservation.ury_stock_reservation.URYStockReservation.save",
                    lambda reservation, **kw: reservation.db_update())):
            result = reduction.reduce_order_item_qty(doc.name, first.name, 0)
        self.assertEqual(result["cancel_kot_names"], [])
        self.assertEqual(frappe.db.get_value("URY Stock Reservation", {"reservation_group": first_group}, "status"),
                         "Released")
        self.assertEqual(self.history(doc, later), later_before)
        doc.reload()
        self.assertEqual([(r.name, r.fire_at.minute) for r in doc.items if r.ury_held], [(later.name, 30)])
        self.assertEqual(doc.grand_total, 115)


    def test_unattributable_stock_groups_refuse_remerge(self):
        doc = self.legacy_check()
        row = frappe.get_doc("URY Stock Reservation", {"reservation_group": "r14-1"})
        row.db_set("warehouse", "Other kitchen")
        before = frappe.db.get_value("URY Stock Reservation", row.name, "audit_log")
        with self.assertRaises(frappe.ValidationError):
            self.remerge(doc)
        self.assertEqual(len(doc.reload().items), 2)
        self.assertEqual(frappe.db.get_value("URY Stock Reservation", row.name, "audit_log"), before)

    def test_modifier_links_follow_each_parent_deadline(self):
        doc = self.legacy_check()
        first, later = doc.items
        for row in (first, later):
            doc.append("items", dict(name=row.name + "-side", item_code="Side", qty=1,
                                     reservation_line_key="side", custom_parent_line_key="k",
                                     ury_held=1, fire_at=row.fire_at))
        changes = order._merged_kitchen_keys([doc], {row.name: row.name for row in doc.items})
        for row in doc.items:
            row.update(changes.get(row.name, {}))
        self.assertNotEqual(first.reservation_line_key, later.reservation_line_key)
        self.assertEqual([r.custom_parent_line_key for r in doc.items[2:]],
                         [first.reservation_line_key, later.reservation_line_key])
        self.assertEqual({r.name for r in order._selected_held_rows(doc, first.name, None)},
                         {first.name, first.name + "-side"})
