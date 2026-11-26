"""Legacy print-release, occupancy audit, and MariaDB TIME precision."""
import json
import unittest

import frappe
from frappe.utils import get_time, now_datetime

from ury.ury.api import table_lifecycle as floor
from ury.ury.doctype.ury_order import test_pos_pr9_r27 as r27, ury_order


class TestPrintRelease(unittest.TestCase):
    def setUp(self):
        self.fx = r27.TestReleasedAndRecoveredSeatings()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def printed_draft(self, currency, places):
        check, ticket, _, table = self.fx.case.seating(currency, places)
        frappe.db.set_value("Branch", check.branch, {
            "custom_table_release_trigger": "On Bill Print (Legacy)", "custom_enable_bussing_state": 0,
        })
        check.db_set("restaurant_table", table.name)
        ury_order.change_table_in_kot(check.name, table.name, check.branch)
        floor.set_table_state(table.name, "Seated", check=check.name)
        self.assertEqual(self.fx.case.stage(table), "fired")
        check.db_set(dict(invoice_printed=1, custom_printing_time=now_datetime()))
        self.assertTrue(ury_order.release_tables_after_print(check.name))
        self.assertEqual(table.reload().occupied, 0)
        self.assertEqual(check.reload().docstatus, 0)
        return check, ticket, table

    def test_printed_unsettled_guest_is_excluded_after_legacy_reseating(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                old, ticket, table = self.printed_draft(currency, places)
                # Restore a pre-upgrade snapshot without occupancy or roster history.
                frappe.db.delete("Version", {"ref_doctype": "URY Table", "docname": table.name})
                new = self.fx.draft(old, "-walk-in", now_datetime(), invoice_printed=0, custom_printing_time=None)
                table.db_set(dict(occupied=1, custom_table_state="Seated", latest_invoice_time=new.creation.time()))
                self.assertEqual(frappe.db.count("Version", {"ref_doctype": "URY Table", "docname": table.name}), 0)
                members = self.fx.recover(table)
                self.assertNotEqual(ticket.reload().order_status, "Served")
                self.assertEqual((members, self.fx.case.stage(table)), ({new.name}, "seated"))

    def test_last_print_records_one_occupancy_release_even_on_retry(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                check, _, table = self.printed_draft(currency, places)
                self.assertTrue(ury_order.release_tables_after_print(check.name))
                history = frappe.get_all("Version", filters={"ref_doctype": "URY Table", "docname": table.name},
                                         order_by="creation asc", pluck="data")
                self.assertEqual([json.loads(row)["changed"] for row in history],
                                 [[["occupied", 0, 1]], [["occupied", 1, 0]]])

    def test_seating_clock_update_has_no_mariadb_notes(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                _, _, _, table = self.fx.case.seating(currency, places)
                floor.set_table_state(table.name, "Seated")
                instant = now_datetime().replace(microsecond=123456)
                # Already seated: the UPDATE is the last SQL statement before SHOW WARNINGS.
                floor.set_table_state(table.name, "Seated", since=instant)
                self.assertEqual(frappe.db.sql("SHOW WARNINGS"), ())
                self.assertEqual(get_time(table.reload().latest_invoice_time), instant.time())
