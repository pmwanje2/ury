"""Regression coverage for the first timed-fire review."""

import csv
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ury.ury.api import test_ury_hold_fire as fixture


class ReviewTests(fixture.HoldFireTests):
    def setup_course_tickets(self, tickets, cancelled=()):
        self.pos_profile.auto_fire_next_course_on_bump = 1
        self.framework.db.sql.side_effect = lambda query, *args, **kw: (
            tickets if "tabURY KOT Items" in query else [(self.invoice.name,)]
        )
        self.framework.db.get_values.return_value = [{"original_kot": name} for name in cancelled]
        self.framework.get_all.side_effect = lambda dt, **kw: (
            [dict(name=course, custom_serving_priority=idx)
             for idx, course in enumerate(("Starter", "Main", "Dessert"), 1)]
            if dt == "URY Menu Course" else []
        )

    def test_finishing_starter_after_main_advances_dessert_once(self):
        self.row("starter", held=0, course="Starter")
        self.row("main", held=0, course="Main")
        dessert = self.row("dessert", course="Dessert")
        tickets = [dict(name="starter-kot", course="Starter", order_status="Ready For Prepare"),
                   dict(name="main-kot", course="Main", order_status="Served")]
        self.setup_course_tickets(tickets)
        self.order._fire_next_held_course(self.invoice, ["main-kot"])
        self.assertEqual(self.tickets, [])
        tickets[0]["order_status"] = "Served"
        self.order._fire_next_held_course(self.invoice, ["starter-kot"])
        self.order._fire_next_held_course(self.invoice, ["starter-kot"])
        self.assertEqual(dessert.ury_held, 0)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])

    def test_verified_cancellation_does_not_block_remaining_station(self):
        self.row("starter", held=0, course="Starter")
        main = self.row("main")
        self.setup_course_tickets(
            [dict(name="hot", course="Starter", order_status="Served"),
             dict(name="cold", course="Starter", order_status="Ready For Prepare")],
            cancelled=["obsolete, cold"],
        )
        self.order._fire_next_held_course(self.invoice, ["hot"])
        self.assertEqual(main.ury_held, 0)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])

    def test_unversioned_relative_retry_cannot_postpone_existing_schedule(self):
        held = self.row()
        self.order.schedule_held_items(self.invoice.name, line=held.name, delay_minutes=10)
        deadline = held.fire_at
        with patch.object(self.order, "now_datetime", return_value=datetime(2026, 10, 16, 12, 2)):
            self.order.schedule_held_items(self.invoice.name, line=held.name, delay_minutes=10)
        self.assertEqual(held.fire_at, deadline)
        self.invoice.save.assert_called_once()

    def test_version_allows_explicit_reschedule(self):
        held = self.row()
        self.order.schedule_held_items(self.invoice.name, line=held.name, delay_minutes=10)
        deadline = held.fire_at
        self.order.schedule_held_items(self.invoice.name, line=held.name, delay_minutes=20,
                                       last_modified_time=self.invoice.modified)
        self.assertEqual(held.fire_at, deadline + timedelta(minutes=10))

    def test_expo_requires_displayed_ticket_selection(self):
        display = self.load_module("ury.ury.api.ury_kot_display", "ury_kot_display.py")
        self.framework.get_all.return_value = []
        with self.assertRaisesRegex(ValueError, "Select the displayed kitchen tickets"):
            display.expo_bump_order(self.invoice.name)
        self.framework.get_all.assert_not_called()

    def test_split_copies_timed_and_confirmation_state(self):
        scheduled = self.row("scheduled", qty=4)
        awaiting = self.row("awaiting", qty=2)
        scheduled.fire_at = datetime(2026, 10, 16, 12, 10)
        awaiting.fire_confirmation_pending = 1
        for row in (scheduled, awaiting):
            row.price_list_rate = row.base_price_list_rate = row.rate
            row.cost_center, row.uom, row.conversion_factor, row.warehouse = "CC", "Nos", 1, "Stores"
        timed = self.order._copy_invoice_item_fields(scheduled, 2)
        pending = self.order._copy_invoice_item_fields(awaiting, 1)
        self.assertEqual((timed["qty"], timed["ury_held"], timed["fire_at"]), (2, 1, scheduled.fire_at))
        self.assertEqual((pending["qty"], pending["ury_held"], pending["fire_confirmation_pending"]), (1, 1, 1))

    def test_direct_retail_releases_without_kitchen_station_or_ticket(self):
        held = self.row("retail")
        retail = SimpleNamespace(production_policy="DIRECT_RETAIL")
        self.framework.db.get_all.return_value = []
        with patch.object(self.kot, "resolve_production_context", return_value=retail), patch.object(
            self.kot, "resolve_production_units", return_value=[]
        ):
            self.fire(line=held.name)
        self.assertEqual(held.ury_held, 0)
        self.assertEqual(self.tickets, [])

    def test_disabled_hold_message_has_no_vendor_prefix(self):
        held = self.row()
        self.pos_profile.ury_enable_hold_fire = 0
        with self.assertRaises(ValueError) as caught:
            self.fire(line=held.name)
        self.assertIn("Hold and fire is disabled for this outlet.", str(caught.exception))
        self.assertNotRegex(str(caught.exception), r"(?i)\b(?:ury|huf|kamra|simphony)\b|URY_")


class TranslationTests(TestCase):
    def test_timed_fire_server_errors_are_in_each_catalog(self):
        messages = {
            "Hold and fire is disabled for this outlet.",
            "Select exactly one line or course to fire.",
            "The selected line or course is not on this order.",
            "Order has been modified. Reload the order and try again.",
            "Specify either a delay or a fire time.",
            "Choose a valid fire time or a positive delay in whole minutes.",
            "No active kitchen station is configured for {0}.",
            "Select the displayed kitchen tickets to bump.",
            "Invalid kitchen ticket selection.",
            "You cannot bump orders from another outlet.",
            "Select an expo station.",
            "You do not have access to held items at this outlet.",
            "You do not have access to held items on this check.",
            "Select a held kitchen line.",
            "This course is not awaiting confirmation.",
        }
        directory = Path(__file__).resolve().parents[1] / "translations"
        for language in ("en", "fr", "es", "ar"):
            with self.subTest(language=language), (directory / f"{language}.csv").open() as catalog:
                translations = {row[0]: row[1] for row in csv.reader(catalog) if len(row) > 1}
                self.assertEqual(messages - translations.keys(), set())
                for message in messages:
                    self.assertTrue(translations[message].strip())
                    self.assertNotRegex(translations[message], r"(?i)\b(?:ury|huf|kamra|simphony)\b|URY_")
