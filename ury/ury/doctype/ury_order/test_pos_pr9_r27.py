"""Release, transfer and all-paid split recovery on persisted floor records."""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import get_time, now_datetime

from ury.patches.v3_46 import initialize_table_seating_checks
from ury.ury.api import table_lifecycle as floor, ury_kot_display
from ury.ury.doctype.ury_order import test_pos_pr9_r25 as r25, ury_order


class TestReleasedAndRecoveredSeatings(unittest.TestCase):
    def setUp(self):
        self.case = r25.TestSeatingMembership()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.currencies = self.case.fx.kitchen.currencies

    def recover(self, table):
        table.db_set("seating_checks", None)
        initialize_table_seating_checks.execute()
        return set(json.loads(table.reload().seating_checks))

    def draft(self, original, suffix, created, **fields):
        check = frappe.copy_doc(original)
        check.name = original.currency + suffix
        check.update(dict(creation=created, docstatus=0, status="Draft", ury_applied_rounds=None,
                          custom_split_group=None, custom_split_from=None))
        check.update(fields)
        check.db_insert()
        return check

    def test_form_after_merged_release(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                _, _, a, b = self.case.seating(currency, places)
                for table in (a, b):
                    table.db_set("restaurant_room", currency + "-floor")
                with patch.object(frappe.db, "commit"):
                    self.assertTrue(ury_order.merge_tables_batch(a.name, [b.name]))
                    self.assertTrue(ury_order.unmerge_tables(a.name))
                    self.assertEqual(ury_order._get_merge_cluster(b.name)[0], [b.name])
                    self.assertTrue(ury_order.merge_tables_batch(a.name, [b.name]))
                for table in (a, b):
                    floor.set_table_state(table.name, "Seated")
                form = a.reload()
                self.assertEqual(form.merged_with, b.name)
                with patch.object(floor, "table_policy", return_value={"custom_enable_bussing_state": 1}):
                    floor.update_table_after_check([a.name, b.name], a.branch)
                self.assertIsNone(frappe.db.get_value("URY Table", a.name, "merged_with"))
                form.no_of_seats = 7
                form.flags.ignore_links = form.flags.ignore_mandatory = True
                form.save(ignore_permissions=True)
                self.assertEqual((form.reload().no_of_seats, form.custom_table_state), (7, "Dirty"))
                self.assertEqual(ury_order._get_merge_cluster(a.name)[0], [a.name])
                floor.mark_table_clean(a.name)
                self.assertEqual((a.reload().occupied, a.custom_table_state, a.merged_with), (0, "Available", None))

    def test_older_transferred_walk_in_excludes_departed_owner_before_and_after_payment(self):
        for currency, places in self.currencies:
            previous, issued, source, target = self.case.seating(currency, places)
            now = now_datetime()
            previous.db_set("creation", now - timedelta(minutes=25))
            self.case.move(previous, source, target)
            self.case.pay(previous)
            floor.set_table_state(target.name, "Available")
            incoming = self.draft(previous, "-incoming", now - timedelta(minutes=40), restaurant_table=source.name)
            self.case.move(incoming, source, target)
            # The elapsed-check display continues to show when the transferred check opened.
            self.assertEqual(get_time(target.reload().latest_invoice_time), incoming.creation.time())
            self.assertEqual(self.case.stage(target), "seated")
            for paid in (False, True):
                with self.subTest(currency=currency, paid=paid):
                    if paid:
                        self.case.pay(incoming)
                    self.assertEqual(self.recover(target), {incoming.name})
                    self.assertEqual(self.case.stage(target), "seated")
                    self.assertNotEqual(issued.reload().order_status, "Served")

    def test_all_paid_split_family_including_ticketless_intermediate(self):
        for currency, places in self.currencies:
            with self.subTest(currency=currency):
                root, ticket, _, table = self.case.seating(currency, places)
                now = now_datetime()
                root.db_set(dict(creation=now - timedelta(minutes=30), restaurant_table=table.name))
                ury_order.change_table_in_kot(root.name, table.name, root.branch)
                tea = next(row for row in root.items if row.item_code == "Tea")
                middle = self.case.fx.kitchen.split(root, [tea])
                middle.db_set(dict(creation=now - timedelta(minutes=20), custom_split_from=root.name))
                # Older split generations can lack a shared group or have a new one after re-splitting.
                youngest = self.draft(middle, "-grandchild", now - timedelta(minutes=5),
                                      restaurant_table=table.name, custom_split_from=middle.name)
                unrelated = self.draft(root, "-departed", now - timedelta(minutes=10))
                for check in (root, middle, youngest, unrelated):
                    self.case.pay(check)
                family = {root.name, middle.name, youngest.name}
                table.db_set(dict(occupied=1, latest_invoice_time=youngest.creation.time(),
                                  seating_checks=json.dumps(sorted(family))))
                self.assertEqual(self.case.stage(table), "fired")
                self.assertEqual(self.recover(table), family)
                self.assertEqual(self.case.stage(table), "fired")
                self.assertEqual(ury_kot_display.expo_bump_order(root.name, tickets=[ticket.name])["served_kots"],
                                 [ticket.name])
                self.assertEqual(self.case.stage(table), "seated")
