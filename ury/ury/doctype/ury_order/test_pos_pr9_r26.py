"""Current seating recovery and metadata saves against persisted MariaDB records."""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from ury.patches.v3_46 import initialize_table_seating_checks
from ury.ury.api import seating_membership, table_lifecycle
from ury.ury.doctype.ury_order import test_pos_pr9_r25 as r25


class TestSeatingRosterRecovery(unittest.TestCase):
    def setUp(self):
        self.floor = r25.TestSeatingMembership()
        self.floor.setUp()
        self.addCleanup(self.floor.doCleanups)
        self.currencies = self.floor.fx.kitchen.currencies

    def test_stale_table_form_preserves_dirty_state(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                owner, _, source, table = self.floor.seating(currency, places)
                self.floor.move(owner, source, table)
                form = table.reload()
                table_lifecycle.set_table_state(table.name, "Dirty")
                fields = ["occupied", "custom_table_state", "custom_dirty_since", "latest_invoice_time", "seating_checks"]
                released = frappe.db.get_value("URY Table", table.name, fields)
                self.assertEqual(released[:2], (0, "Dirty"))
                self.assertIsNotNone(released[2])
                form.no_of_seats = 8
                form.flags.ignore_links = form.flags.ignore_mandatory = True
                form.save(ignore_permissions=True)
                self.assertEqual(form.reload().no_of_seats, 8)
                self.assertEqual(frappe.db.get_value("URY Table", table.name, fields), released)
                with patch.object(table_lifecycle, "table_policy", return_value={"custom_block_dirty_table_seating": 1}):
                    with self.assertRaisesRegex(frappe.ValidationError, "TABLE-DIRTY-NEEDS-BUSSING"):
                        table_lifecycle.assert_table_seatable(table.name, table.branch)

    def test_legacy_migration_excludes_departed_paid_walk_in(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                departed, ticket, source, table = self.floor.seating(currency, places)
                start = now_datetime() - timedelta(minutes=2)
                departed.db_set("creation", start - timedelta(minutes=23))
                self.floor.move(departed, source, table)
                self.floor.pay(departed)
                self.assertEqual(self.floor.stage(table), "fired")
                table_lifecycle.set_table_state(table.name, "Available")
                walk_in = frappe.copy_doc(departed)
                walk_in.name = currency + "-r26-walk-in"
                walk_in.update(dict(creation=start, docstatus=0, status="Draft", ury_applied_rounds=None,
                                    custom_split_from=None, custom_split_group=None))
                walk_in.db_insert()
                table.db_set(dict(occupied=1, latest_invoice_time=start.time(), seating_checks=None))
                initialize_table_seating_checks.execute()
                self.assertEqual(json.loads(table.reload().seating_checks), [walk_in.name])
                self.assertEqual(self.floor.stage(table), "seated")
                self.assertNotEqual(ticket.reload().order_status, "Served")

    def test_invalid_roster_recovers_paid_work_and_repairs_on_update(self):
        for currency, places in self.currencies:
            owner, _, source, table = self.floor.seating(currency, places)
            self.floor.move(owner, source, table)
            self.floor.pay(owner)
            for raw in ("{", "null", '"ABC"', "[123, null]", '["  "]'):
                table.db_set("seating_checks", raw)
                with self.subTest(currency=currency, roster=raw, operation="dashboard"):
                    self.assertEqual(self.floor.stage(table), "fired")
                with self.subTest(currency=currency, roster=raw, operation="idempotent add"):
                    seating_membership.update_members(frappe, table.name, add=[owner.name])
                    self.assertEqual(json.loads(table.reload().seating_checks), [owner.name])

            with self.subTest(currency=currency, operation="normalize valid names"):
                table.db_set("seating_checks", json.dumps([" " + owner.name, owner.name + " "]))
                self.assertEqual(self.floor.stage(table), "fired")
                seating_membership.update_members(frappe, table.name, add=[owner.name])
                self.assertEqual(json.loads(table.reload().seating_checks), [owner.name])
