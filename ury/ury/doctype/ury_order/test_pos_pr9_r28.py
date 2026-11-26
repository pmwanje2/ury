"""Recover paid kitchen work when the legacy floor roster needs rebuilding."""
import json
import unittest
from datetime import timedelta

import frappe
from frappe.utils import now_datetime

from ury.ury.api import table_lifecycle as floor
from ury.ury.doctype.ury_order import test_pos_pr9_r27 as r27, ury_order


class TestLegacyPaidMembership(unittest.TestCase):
    def setUp(self):
        self.fx = r27.TestReleasedAndRecoveredSeatings()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.floor = self.fx.case

    def test_overnight_arrival_and_younger_reservation(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                owner, _, source, table = self.floor.seating(currency, places)
                now = now_datetime()
                owner.db_set("creation", now - timedelta(days=1))
                departed = self.fx.draft(owner, "-last-party", now - timedelta(days=2))
                self.floor.move(departed, source, table)
                self.floor.pay(departed)
                floor.set_table_state(table.name, "Available")

                # The arrivals straddle the start of the reserved visit. The
                # second check's opening time is later than either arrival.
                visit = now_datetime()
                self.floor.move(owner, source, table)
                self.floor.pay(owner)
                younger = self.fx.draft(owner, "-younger", now_datetime())
                frappe.get_doc(dict(doctype="URY Table Reservation", reserved_table=table.name,
                                    status="Seated", reserved_at=visit, pos_invoice=younger.name)).db_insert()
                floor.set_table_state(table.name, "Seated", since=younger.creation)
                self.assertEqual(self.floor.stage(table), "fired")
                self.assertEqual(self.fx.recover(table), {younger.name, owner.name})
                self.assertEqual(self.floor.stage(table), "fired")

    def test_younger_walk_in_joins_transferred_owner(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                owner, _, source, table = self.floor.seating(currency, places)
                now = now_datetime()
                owner.db_set("creation", now - timedelta(days=1))
                self.floor.move(owner, source, table)
                self.floor.pay(owner)
                younger = self.fx.draft(owner, "-walk-in", now_datetime())
                floor.set_table_state(table.name, "Seated", since=younger.creation, check=younger.name)
                self.assertEqual(self.fx.recover(table), {owner.name, younger.name})
                self.assertEqual(self.floor.stage(table), "fired")

    def test_paid_split_family_through_a_different_table(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                root, _, other, table = self.floor.seating(currency, places)
                now = now_datetime()
                root.db_set(dict(restaurant_table=table.name, creation=now - timedelta(minutes=40),
                                 custom_split_from=None, custom_split_group=None))
                ury_order.change_table_in_kot(root.name, table.name, root.branch)
                middle = self.fx.draft(root, "-middle", now - timedelta(minutes=30), custom_split_from=root.name)
                youngest = self.fx.draft(root, "-youngest", now - timedelta(minutes=10), custom_split_from=middle.name)
                departed = self.fx.draft(root, "-last-party", now - timedelta(minutes=20))
                self.floor.move(middle, table, other)
                for check in (root, middle, youngest, departed):
                    self.floor.pay(check)
                expected = {root.name, youngest.name}
                table.db_set(dict(occupied=1, latest_invoice_time=youngest.creation.time(),
                                  seating_checks=json.dumps(sorted(expected))))
                self.assertEqual(self.floor.stage(table), "fired")
                self.assertEqual(self.fx.recover(table), expected)
                self.assertEqual(self.floor.stage(table), "fired")
                self.assertEqual(self.fx.recover(other), {middle.name})

    def test_paid_ticket_on_secondary_merged_table(self):
        for currency, places in self.fx.currencies:
            with self.subTest(currency=currency):
                owner, _, primary, secondary = self.floor.seating(currency, places)
                owner.db_set(dict(restaurant_table=primary.name, custom_merged_tables=f"extra, {secondary.name} "))
                lookalike = self.fx.draft(owner, "-other-cluster", now_datetime(),
                                          custom_merged_tables=secondary.name + "0")
                for check in (owner, lookalike):
                    self.floor.pay(check)
                secondary.db_set(dict(occupied=1, latest_invoice_time=owner.creation.time(),
                                      seating_checks=json.dumps([owner.name])))
                self.assertEqual(self.floor.stage(secondary), "fired")
                self.assertEqual(self.fx.recover(secondary), {owner.name})
                self.assertEqual(self.floor.stage(secondary), "fired")
