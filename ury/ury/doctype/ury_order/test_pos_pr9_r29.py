"""Recover a pre-upgrade seating from standard Frappe occupancy and transfer history."""
import json
import unittest
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from ury.ury.api import table_lifecycle, ury_kot_display
from ury.ury.doctype.ury_order import test_pos_pr9_r27 as r27


class TestOccupancyBoundary(unittest.TestCase):
    def setUp(self):
        self.fx = r27.TestReleasedAndRecoveredSeatings()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def transition(self, table, occupied):
        # Persist the same diff Frappe writes for a tracked legacy URY Table.
        before = frappe.get_doc("URY Table", table.name)
        self.assertEqual(before.occupied, 1 - occupied)
        table.reload().db_set("occupied", occupied)
        version = frappe.new_doc("Version")
        self.assertTrue(version.update_version_info(before, table))
        version.insert(ignore_permissions=True)
        table_lifecycle.set_table_state(table.name, "Seated" if occupied else "Available")

    def visit(self, currency, places, *, reserved):
        floor = self.fx.case
        owner, ticket, source, table = floor.seating(currency, places)
        scheduled = now_datetime() - timedelta(minutes=15)
        owner.db_set("creation", scheduled - timedelta(days=1))
        previous = self.fx.draft(owner, "-departed", scheduled - timedelta(hours=1))
        stale = frappe.copy_doc(ticket)
        stale.name = currency + "-departed-ticket"
        stale.update(dict(invoice=previous.name, docstatus=1, validation_dedup_key=None))
        stale.db_insert()
        floor.move(previous, source, table)
        floor.pay(previous)
        self.assertEqual(floor.stage(table), "fired")

        self.transition(table, 0)
        self.transition(table, 1)
        floor.move(owner, source, table)
        floor.pay(owner)
        younger = self.fx.draft(owner, "-new-party", now_datetime())
        if reserved:
            frappe.get_doc(dict(doctype="URY Table Reservation", status="Seated",
                                reserved_table=table.name, reserved_at=scheduled,
                                pos_invoice=younger.name)).db_insert()
        table_lifecycle.set_table_state(table.name, "Seated", since=younger.creation)
        versions = frappe.get_all("Version", filters={"ref_doctype": "URY Table", "docname": table.name},
                                  pluck="data")
        self.assertEqual(len(versions), 2)
        for data in versions:
            self.assertEqual([edit[0] for edit in json.loads(data)["changed"]], ["occupied"])
        self.assertEqual(frappe.db.count("Version", {"ref_doctype": "POS Invoice",
                         "docname": ("in", [previous.name, owner.name]),
                         "data": ("like", '%"restaurant_table"%')}), 2)

        members = self.fx.recover(table)
        before_bump = floor.stage(table)
        self.assertEqual(ury_kot_display.expo_bump_order(owner.name, tickets=[ticket.name])["served_kots"],
                         [ticket.name])
        self.assertNotEqual(stale.reload().order_status, "Served")
        self.assertEqual((members, before_bump, floor.stage(table)),
                         ({owner.name, younger.name}, "fired", "seated"))

    def test_late_reservation_after_previous_guest_left(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                self.visit(currency, places, reserved=True)

    def test_overnight_walk_in_arrived_before_younger_check(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                self.visit(currency, places, reserved=False)
