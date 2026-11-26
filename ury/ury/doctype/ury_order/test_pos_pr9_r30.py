"""Occupancy auditing and recovery from the original POS print/transfer writes."""
import json
import unittest
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from ury.ury.api import table_lifecycle as floor, ury_kot_display
from ury.ury.doctype.ury_order import test_pos_pr9_r27 as r27, ury_order


class TestTableReleaseEvidence(unittest.TestCase):
    def setUp(self):
        self.fx = r27.TestReleasedAndRecoveredSeatings()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def table_history(self, table):
        return frappe.get_all("Version", filters={"ref_doctype": "URY Table", "docname": table.name},
                              order_by="creation asc, name asc", pluck="data")

    def test_real_state_changes_write_one_version_per_flip(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                _, _, _, table = self.fx.case.seating(currency, places)
                table.db_set("occupied", 1)
                modified = table.reload().modified
                for state in ("Dirty", "Available", "Seated", "Check Printed"):
                    floor.set_table_state(table.name, state)
                self.assertEqual(table.reload().modified, modified)
                self.assertEqual([json.loads(data)["changed"] for data in self.table_history(table)],
                                 [[["occupied", 1, 0]], [["occupied", 0, 1]]])

    def legacy_transfer(self, invoice, source, target):
        # The legacy transfer writes the table directly. Only the invoice save
        # has a Frappe Version. Use its native versioning without PR-9 save hooks.
        invoice.db_set("restaurant_table", source.name)
        before = frappe.get_doc("POS Invoice", invoice.name)
        frappe.db.set_value("URY Table", target.name, {"occupied": 1, "latest_invoice_time": invoice.creation})
        frappe.db.set_value("URY Table", source.name, {"occupied": 0, "latest_invoice_time": None})
        invoice.db_set("restaurant_table", target.name)
        invoice._doc_before_save = before
        invoice.flags.ignore_version = False
        invoice.save_version()
        ury_order.change_table_in_kot(invoice.name, target.name, invoice.branch)

    def legacy_visit(self, currency, places, *, reservation=False, pay_after_join=False):
        case = self.fx.case
        owner, ticket, source, table = case.seating(currency, places)
        scheduled = now_datetime() - timedelta(minutes=15)
        owner.db_set("creation", scheduled - timedelta(days=1))
        previous = self.fx.draft(owner, "-previous-party", scheduled - timedelta(hours=2))
        stale = frappe.copy_doc(ticket)
        stale.name = currency + "-previous-kot"
        stale.update(dict(invoice=previous.name, docstatus=1, validation_dedup_key=None))
        stale.db_insert()
        self.legacy_transfer(previous, source, table)
        previous.db_set("invoice_printed", 1)
        case.pay(previous)
        frappe.db.set_value("URY Table", table.name, {"occupied": 0, "latest_invoice_time": None})

        self.legacy_transfer(owner, source, table)
        if not pay_after_join:
            case.pay(owner)
        younger = self.fx.draft(owner, "-new-party", now_datetime())
        frappe.db.set_value("URY Table", table.name, "latest_invoice_time", younger.creation)
        if reservation:
            frappe.get_doc(dict(doctype="URY Table Reservation", reserved_table=table.name,
                                status="Seated", reserved_at=scheduled, pos_invoice=younger.name)).db_insert()
        if pay_after_join:
            case.pay(owner)

        self.assertFalse(self.table_history(table))
        self.assertTrue(frappe.db.exists("Version", {"ref_doctype": "POS Invoice", "docname": owner.name,
                                                    "data": ("like", '%"restaurant_table"%')}))
        members = self.fx.recover(table)
        before_bump = case.stage(table)
        self.assertEqual(ury_kot_display.expo_bump_order(owner.name, tickets=[ticket.name])["served_kots"],
                         [ticket.name])
        self.assertNotEqual(stale.reload().order_status, "Served")
        self.assertEqual((members, before_bump, case.stage(table)),
                         ({younger.name, owner.name}, "fired", "seated"))
        self.assertFalse(self.table_history(table))

    def test_late_reservation_after_print_release(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                self.legacy_visit(currency, places, reservation=True)

    def test_walk_in_paid_before_younger_check(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                self.legacy_visit(currency, places)

    def test_walk_in_paid_while_younger_check_is_open(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                self.legacy_visit(currency, places, pay_after_join=True)
