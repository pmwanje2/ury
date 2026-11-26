"""Kitchen ownership survives check transfers without editing the submitted ticket."""
import unittest

import frappe

from ury.ury.doctype.ury_order import test_pos_pr9_r18 as r18, ury_order as order


class TestKitchenAudit(unittest.TestCase):
    def setUp(self):
        self.kitchen = r18.TestIssuedOwnership()
        self.kitchen.setUp()
        self.addCleanup(self.kitchen.doCleanups)
        self.fx = self.kitchen.fx

    def snapshot(self, tickets):
        return (frappe.db.sql("SELECT * FROM `tabURY KOT` WHERE name IN %s ORDER BY name", (tuple(tickets),)),
                frappe.db.sql("SELECT * FROM `tabURY KOT Items` WHERE parent IN %s ORDER BY name", (tuple(tickets),)))

    def test_raw_key_competing_with_historical_position_refuses_merge(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                key = "pos:Soup:1"
                left, right = self.fx.prepare(currency, places, keys=(key, None))
                right = self.kitchen.configure(right, places, [
                    dict(item_code="Soup"), dict(item_code="Soup", reservation_line_key=key),
                ])
                issued = [self.fx.fire(left).name]
                issued.extend(self.kitchen.issue(right, [row])[0] for row in right.items)
                before = self.snapshot(issued)
                with self.assertRaisesRegex(frappe.ValidationError, "POS-KOT-OWNERSHIP"):
                    self.fx.merge(left, right)
                self.assertEqual(self.snapshot(issued), before)
                self.assertEqual((left.reload().grand_total, right.reload().grand_total), (100, 200))

    def nested_dishes(self, currency, places):
        soup, salad = self.fx.prepare(currency, places)
        self.fx.fire(soup)
        rows = [dict(item_code="Salad", reservation_line_key="dish")]
        for code, key, parent in (("Dressing", "dressing", "dish"), ("Salt", "salt", "dish"),
                                  ("Oil", "oil", "dressing")):
            rows.append(dict(item_code=code, reservation_line_key=key, custom_parent_line_key=parent,
                             custom_is_modifier=1, custom_line_role="Modifier", rate=2, price_list_rate=2))
        salad = self.kitchen.configure(salad, places, rows)
        issued, = self.kitchen.issue(salad, salad.items)
        return self.fx.merge(soup, salad), issued

    def test_void_removes_and_cancels_every_modifier_descendant(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                doc, issued = self.nested_dishes(currency, places)
                cancellations = self.kitchen.void(doc, doc.items[1])
                self.assertEqual([row.item_code for row in doc.items], ["Soup"])
                self.assertEqual(doc.grand_total, 100)
                self.assertCountEqual([(c.original_kot, r.item, r.cancelled_qty)
                                       for c in cancellations for r in c.kot_items],
                                      [(issued, item, 1) for item in ("Salad", "Dressing", "Salt", "Oil")])

    def test_split_moves_every_modifier_descendant(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                doc, _ = self.nested_dishes(currency, places)
                selection = [[dict(name=doc.items[1].name, qty=1)]]
                plan, = order._plan_split_checks(doc, selection, "manual", None)
                child = self.kitchen.split(doc, [row for row in doc.items if row.name in plan])
                self.assertCountEqual([row.item_code for row in child.items], ["Salad", "Dressing", "Salt", "Oil"])
                self.assertEqual((doc.grand_total, child.grand_total), (100, 106))

    def test_ticket_headers_and_lines_survive_chained_merge_split_and_void(self):
        for currency, places in self.kitchen.currencies:
            with self.subTest(currency=currency):
                left, right = self.fx.prepare(currency, places)
                tickets = [self.fx.fire(check).name for check in (left, right)]
                before = self.snapshot(tickets)
                doc = self.fx.merge(left, right)
                self.assertEqual(self.snapshot(tickets), before)
                doc = self.fx.merge(self.fx.tea(doc), doc)
                self.assertEqual(self.snapshot(tickets), before)
                child = self.kitchen.split(doc, [doc.items[0], doc.items[-1]])
                self.assertEqual(self.snapshot(tickets), before)
                cancelled, = self.kitchen.void(child, child.items[-1])
                self.assertEqual((cancelled.original_kot, cancelled.kot_items[0].cancelled_qty), (tickets[1], 1))
                self.assertEqual((doc.grand_total, child.grand_total), (100, 15))
                self.assertEqual(self.snapshot(tickets), before)
