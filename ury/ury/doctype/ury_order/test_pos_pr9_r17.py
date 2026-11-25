"""Keep exact kitchen ownership across Add Check, outlet transfer and re-split."""

import unittest
from unittest.mock import Mock, patch

import frappe

from ury.ury.api import ury_kot_generate as kitchen, ury_pos_invoice_qty_reduction as reduction
from ury.ury.doctype.ury_order import test_pos_pr9_r16 as r16, ury_order as order
from ury.ury_pos import floor_actions


class TestKitchenOwnership(unittest.TestCase):
    def setUp(self):
        self.floor = r16.TestIssuedSoupTickets()
        self.floor.setUp()
        self.addCleanup(self.floor.doCleanups)
        self.checks = self.floor.fx.checks
        self.persist = self.floor.fx.floor.persist
        self.enterContext(patch("ury.ury.api.ury_availability.apply_count_delta"))
        self.enterContext(patch.object(reduction, "resolve_pos_reason", return_value=dict(
            code="Guest", display="Guest", note="", is_waste=False)))
        self.enterContext(patch.object(reduction, "require_pos_override", return_value="Manager"))

    def outlet(self, company, name):
        branch = name + "-branch"
        frappe.get_doc(dict(doctype="Branch", name=branch, branch=branch, company=company)).db_insert()
        frappe.get_doc(dict(doctype="POS Profile", name=name, company=company, branch=branch,
                            custom_qty_reduction_allowed_order_types="Dine In",
                            custom_kot_naming_series="KOT-.####")).db_insert()
        frappe.get_doc(dict(doctype="URY Production Unit", name=name + "-kitchen", branch=branch)).db_insert()
        return branch

    def prepare(self, currency, places, keys=("dish", "dish"), qty=1):
        fixture = self.checks.fixture.fixture
        first, second = fixture.checks(currency)
        profile = currency + "-R17-A"
        branch = self.outlet(first.company, profile)
        self.enterContext(patch.object(kitchen, "_resolve_units_for_item_code",
                                      side_effect=lambda *a, pos_profile_id, **kw: [pos_profile_id + "-kitchen"]))
        for doc, key in zip((first, second), keys, strict=True):
            doc.branch, doc.pos_profile, doc.order_type = branch, profile, "Dine In"
            row = doc.items[0]
            row.update(dict(custom_origin_invoice_item=None, reservation_line_key=key, ury_held=1,
                            qty=qty if doc is first else 1))
            doc.calculate_taxes_and_totals()
            fixture.snapshot(doc, places)
        return self.checks.reload(first), self.checks.reload(second)

    def tea(self, source):
        doc = frappe.copy_doc(source)
        doc.name, doc.docstatus = source.currency + "-tea", 0
        doc.custom_split_balance = doc.custom_split_group = None
        doc.set("items", [])
        doc.append("items", dict(name=doc.name + "-item", item_code="Tea", qty=1, rate=15,
                                 price_list_rate=15, conversion_factor=1))
        doc.set_paid_amount = doc.set_outstanding_amount = Mock()
        doc.calculate_taxes_and_totals()
        return self.checks.reload(doc)

    def fire(self, doc):
        doc.save = lambda **kw: self.persist(doc)
        with patch.object(order, "_staff_held_order", return_value=(doc, None)):
            order.fire_items(doc.name, line=doc.items[0].name)
        name, = frappe.get_all("URY KOT", filters={"invoice": doc.name, "docstatus": 1}, pluck="name")
        return frappe.get_doc("URY KOT", name)

    def merge(self, target, source):
        self.checks.merge(target, source)
        self.persist(target)
        return target.reload()

    def use_invoice(self, doc):
        get_doc = frappe.get_doc
        def lookup(dt, *args, **kw):
            return doc if dt == "POS Invoice" and args == (doc.name,) else get_doc(dt, *args, **kw)
        def save(**kw):
            doc.calculate_taxes_and_totals()
            self.persist(doc)
        doc.save = save
        return patch.object(frappe, "get_doc", lookup)

    def reduce(self, doc, row, qty=0):
        with self.use_invoice(doc):
            result = reduction.reduce_order_item_qty(doc.name, row.name, qty, reason="Guest", approval_token="token")
        name, = result["cancel_kot_names"]
        doc.reload()
        ticket = frappe.get_doc("URY KOT", name)
        self.assertEqual((ticket.docstatus, ticket.kot_items[0].cancelled_qty), (1, 1))
        return ticket

    def test_duplicate_local_key_preserves_each_checks_ticket(self):
        for currency, places in self.checks.fixture.currencies:
            with self.subTest(currency=currency):
                first, second = self.prepare(currency, places)
                tickets = [self.fire(doc).name for doc in (first, second)]
                doc = self.merge(first, second)
                doc = self.merge(doc, self.tea(doc))
                self.assertEqual(self.reduce(doc, doc.items[0]).original_kot, tickets[0])
                self.assertEqual(self.reduce(doc, doc.items[0]).original_kot, tickets[1])
                self.assertEqual(doc.grand_total, 15)

    def test_after_outlet_transfer_cancel_at_issuing_station(self):
        for currency, places in self.checks.fixture.currencies:
            with self.subTest(currency=currency):
                first, _ = self.prepare(currency, places)
                issued = self.fire(first)
                doc = self.merge(self.tea(first), first)
                target = currency + "-R17-B"
                self.outlet(doc.company, target)
                with self.use_invoice(doc), patch.object(floor_actions, "authorize_or_perform",
                                                         return_value={"approver": "Manager"}):
                    floor_actions.transfer_check_outlet(doc.name, target)
                self.assertEqual(doc.reload().pos_profile, target)
                cancelled = self.reduce(doc, doc.items[-1])
                self.assertEqual((cancelled.production, cancelled.original_kot), (issued.production, issued.name))
                self.assertEqual(doc.grand_total, 15)

    def test_legacy_unkeyed_release_survives_add_check(self):
        for currency, places in self.checks.fixture.currencies:
            with self.subTest(currency=currency):
                first, _ = self.prepare(currency, places, keys=(None, "other"))
                issued = self.fire(first)
                self.assertEqual(issued.kot_items[0].reservation_line_key, "pos:Soup:1")
                doc = self.merge(self.tea(first), first)
                self.assertEqual(self.reduce(doc, doc.items[-1]).original_kot, issued.name)
                self.assertEqual(doc.grand_total, 15)

    def test_net_release_and_cancellation_move_to_owning_check(self):
        for currency, places in self.checks.fixture.currencies:
            with self.subTest(currency=currency):
                first, other = self.prepare(currency, places, keys=("issued", "held"), qty=2)
                issued = self.fire(first)
                doc = self.merge(first, other)
                doc = self.merge(doc, self.tea(doc))
                cancelled = self.reduce(doc, doc.items[0], 1)
                self.assertEqual(cancelled.original_kot, issued.name)
                child = frappe.get_doc(dict(doctype="POS Invoice", name=currency + "-r17-child",
                                            disable_rounded_total=1, ignore_pricing_rule=1))
                for check in (doc, child):
                    check.set_missing_values = check.run_method = Mock()
                    check.set_paid_amount = check.set_outstanding_amount = Mock()
                child.insert = lambda: self.checks.reload(child)
                doc.save = lambda: self.persist(doc)
                with patch.object(frappe, "new_doc", return_value=child), patch.object(
                        order, "get_branch_stock_policy", return_value=frappe._dict(reservation_control_enabled=0)):
                    order._apply_check_split(doc, {doc.items[0].name: 1, doc.items[-1].name: 1}, doc.company)
                child = frappe.get_doc("POS Invoice", child.name)
                self.assertEqual([ticket.reload().invoice for ticket in (issued, cancelled)], [child.name] * 2)
                self.assertFalse(child.items[0].custom_kitchen_anchor)
                self.assertEqual(self.reduce(child, child.items[0]).original_kot, issued.name)
                self.assertEqual((doc.reload().grand_total, child.grand_total), (100, 15))
