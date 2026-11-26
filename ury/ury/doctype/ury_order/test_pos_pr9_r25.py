"""Seating membership is independent of invoice age and table edits."""
import unittest
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from ury.ury.api import table_lifecycle as floor, ury_kot_display, ury_service_line
from ury.ury.doctype.ury_order import test_pos_pr9_r21 as r21, ury_order


class TestSeatingMembership(unittest.TestCase):
    def setUp(self):
        self.fx = r21.TestReceivingCheck()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def seating(self, currency, places):
        _, owner, ticket = self.fx.owners(currency, places)
        tables = []
        for number in (1, 2):
            table = frappe.get_doc(dict(doctype="URY Table", name=f"{currency}-r25-{number}",
                                        branch=owner.branch, occupied=0))
            table.db_insert()
            tables.append(table)
        key = "ury_dashboard_service_line:" + owner.branch
        self.addCleanup(frappe.cache.delete_value, key)
        return owner, ticket, *tables

    def stage(self, table):
        frappe.cache.delete_value("ury_dashboard_service_line:" + table.branch)
        rows = ury_service_line.get_service_line(table.branch)
        return next(row["stage"] for row in rows if row["table"] == table.name)

    def move(self, check, source, target):
        check.db_set("restaurant_table", target.name)
        floor.update_tables_after_transfer(source.name, target.name, check)
        ury_order.change_table_in_kot(check.name, target.name, check.branch)

    def pay(self, check):
        check.db_set(dict(docstatus=1, status="Paid"))

    def test_younger_split_check_does_not_hide_the_paid_owner(self):
        for currency, places in self.fx.kitchen.currencies:
            with self.subTest(currency=currency):
                owner, ticket, source, table = self.seating(currency, places)
                owner.db_set("creation", now_datetime() - timedelta(minutes=40))
                self.move(owner, source, table)
                tea = next(row for row in owner.items if row.item_code == "Tea")
                child = self.fx.kitchen.split(owner, [tea])
                self.pay(owner)
                self.assertEqual(self.stage(table), "fired")
                # sync_order republishes the seating clock when the younger check is saved.
                floor.set_table_state(table.name, "Seated", since=child.creation)
                self.assertEqual(self.stage(table), "fired")
                self.assertEqual(ury_kot_display.expo_bump_order(owner.name, tickets=[ticket.name])["served_kots"],
                                 [ticket.name])
                self.assertEqual(self.stage(table), "seated")

    def test_reused_table_only_tracks_the_incoming_party(self):
        for currency, places in self.fx.kitchen.currencies:
            with self.subTest(currency=currency):
                incoming, ticket, source, target = self.seating(currency, places)
                now = now_datetime()
                incoming.db_set("creation", now - timedelta(minutes=40))
                departed = frappe.copy_doc(incoming)
                departed.name = currency + "-previous-party"
                departed.update(dict(docstatus=0, creation=now - timedelta(minutes=25), ury_applied_rounds=None))
                departed.db_insert()
                stale = frappe.copy_doc(ticket)
                stale.name = currency + "-previous-ticket"
                stale.update(dict(invoice=departed.name, validation_dedup_key=None, docstatus=1))
                stale.db_insert()
                self.move(departed, source, target)
                self.pay(departed)
                self.assertEqual(self.stage(target), "fired")
                floor.set_table_state(target.name, "Available")
                self.assertEqual(self.stage(target), "open")
                self.move(incoming, target, source)
                self.move(incoming, source, target)
                self.pay(incoming)
                self.assertEqual(self.stage(source), "open")
                self.assertEqual(self.stage(target), "fired")
                self.assertEqual(ury_kot_display.expo_bump_order(incoming.name, tickets=[ticket.name])["served_kots"],
                                 [ticket.name])
                self.assertEqual(self.stage(target), "seated")
                self.assertNotEqual(stale.reload().order_status, "Served")

    def test_legacy_table_edit_does_not_change_its_paid_owner(self):
        for currency, places in self.fx.kitchen.currencies:
            with self.subTest(currency=currency):
                owner, ticket, _, table = self.seating(currency, places)
                table.db_set(dict(occupied=1, latest_invoice_time=None))
                owner.db_set("restaurant_table", table.name)
                self.pay(owner)
                ticket.db_set("restaurant_table", table.name)
                self.assertEqual(self.stage(table), "fired")
                table.db_set("no_of_seats", 6)
                self.assertEqual(self.stage(table), "fired")
                ury_order.cancel_kot(owner.name, reason="Guest")
                self.assertEqual(self.stage(table), "seated")
