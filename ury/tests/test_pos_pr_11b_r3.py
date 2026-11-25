"""R3 regressions for course cancellation, bump atomicity and validation copy."""

import csv
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ury.ury.api import test_ury_hold_fire as fixture


class CourseFlowTests(fixture.HoldFireTests):
    def test_verified_partial_cancel_keeps_open_starter_until_remaining_quantity_is_gone(self):
        self.pos_profile.auto_fire_next_course_on_bump = 1
        self.row("starter", held=0, qty=3, course="Starter")
        dessert = self.row("dessert", course="Dessert")
        starter = dict(name="starter-kot", course="Starter", order_status="Ready For Prepare", has_remaining_qty=1)
        self.framework.db.sql.side_effect = lambda query, *args, **kwargs: [starter] if "tabURY KOT Items" in query else []
        cancellation = dict(name="void-1", type="Partially cancelled", original_kot="starter-kot")
        quantities = [
            dict(parent="starter-kot", item="Fish", quantity=3, cancelled_qty=0, comments="no onions"),
            dict(parent="void-1", item="Fish", quantity=3, cancelled_qty=1, comments="no onions"),
        ]

        def get_values(doctype, *args, **kwargs):
            return quantities if doctype == "URY KOT Items" else [cancellation]

        self.framework.db.get_values.side_effect = get_values
        self.framework.get_all.side_effect = lambda doctype, **kwargs: (
            [dict(name="Starter", custom_serving_priority=1), dict(name="Dessert", custom_serving_priority=2)]
            if doctype == "URY Menu Course" else [cancellation]
        )

        display = self.load_module("ury.ury.api.ury_kot_display", "ury_kot_display.py")
        self.order._fire_next_held_course(self.invoice, ["starter-kot"])
        self.assertEqual(dessert.ury_held, 1)
        self.assertEqual(self.tickets, [])
        self.assertNotIn("starter-kot", display._get_cancel_confirmed_original_kots("Branch A"))

        quantities[1]["cancelled_qty"] = 3
        self.assertIn("starter-kot", display._get_cancel_confirmed_original_kots("Branch A"))
        self.order._fire_next_held_course(self.invoice, ["starter-kot"])
        self.assertEqual(dessert.ury_held, 0)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])

    def run_bump_with_failed_release(self, *, expo):
        display = self.load_module("ury.ury.api.ury_kot_display", "ury_kot_display.py")
        display.getBranch.return_value = self.invoice.branch
        kot = fixture.Row(name="earlier", invoice=self.invoice.name, type="New Order",
                          branch=self.invoice.branch, pos_profile=self.pos_profile.name,
                          creation=datetime(2026, 10, 16, 12))
        self.historical_tickets[kot.name] = kot
        self.framework.get_all.return_value = [{"name": kot.name, "branch": kot.branch}]
        self.framework.get_all.side_effect = None
        self.framework.db.get_value.return_value = "Ready For Prepare"
        self.framework.db.get_value.side_effect = None
        database = {"earlier": "Ready For Prepare", "next": "Held"}
        effects = ["previous event"]
        savepoints = {}

        def rollback(*, save_point):
            database.update(savepoints[save_point])

        def record_served(*_args):
            database["earlier"] = "Served"
            effects.append("earlier served")

        def release_next(*_args):
            database["next"] = "Unheld"
            effects.append("invalid release")
            raise ValueError("No active kitchen station is configured for Fish.")

        side_effect_tools = SimpleNamespace(
            capture_pending_side_effects=lambda: len(effects),
            discard_side_effects_since=lambda size: effects.__delitem__(slice(size, None)),
        )
        self.framework.db.savepoint.side_effect = lambda name: savepoints.update({name: database.copy()})
        self.framework.db.rollback.side_effect = rollback
        with (
            patch.dict(sys.modules, {"frappe.desk.form.linked_with": side_effect_tools}),
            patch.object(display, "_lock_course_fire_order", return_value=self.invoice),
            patch.object(display, "record_kot_served", side_effect=record_served),
            patch.object(self.order, "_fire_next_held_course", side_effect=release_next),
        ):
            # Expo checks the invoice branch before looking up the ticket.
            self.framework.db.get_value.side_effect = lambda dt, name, field, **kw: (
                kot.branch if field == "branch" else
                kot.pos_profile if field == "pos_profile" else database["earlier"]
            )
            if expo:
                result = display.expo_bump_order(self.invoice.name, tickets=[kot.name])
                self.assertEqual(result["served_kots"], [kot.name])
            else:
                display.serve_kot(kot.name)
        self.assertEqual(database, {"earlier": "Served", "next": "Held"})
        self.assertEqual(effects, ["previous event", "earlier served"])
        self.framework.db.commit.assert_not_called()
        self.framework.log_error.assert_called_once()

    def test_failed_next_course_does_not_undo_station_bump(self):
        self.run_bump_with_failed_release(expo=False)

    def test_failed_next_course_does_not_undo_expo_bump(self):
        self.run_bump_with_failed_release(expo=True)

    def test_cancellation_manager_refusal_is_translated(self):
        display = self.load_module("ury.ury.api.ury_kot_display", "ury_kot_display.py")
        message = "Only a manager can confirm a cancelled KOT."
        with patch.object(display, "_", return_value="Localized manager refusal") as translate:
            with self.assertRaisesRegex(PermissionError, "Localized manager refusal"):
                display.confirm_cancel_kot("cancel-ticket")
        translate.assert_called_once_with(message)

    def test_offline_hold_refusal_has_neutral_code(self):
        with self.assertRaises(ValueError) as caught:
            self.sync([self.request()], offline_printed=1)
        self.assertIn("Hold and fire requires an online connection.", str(caught.exception))
        self.assertIn("HOLD-OFFLINE", str(caught.exception))
        self.assertNotIn("URY", str(caught.exception))


class R3CatalogTests(TestCase):
    def test_cancellation_and_offline_refusals_are_in_each_catalog(self):
        directory = Path(__file__).resolve().parents[1] / "translations"
        messages = {"Only a manager can confirm a cancelled KOT.", "Hold and fire requires an online connection."}
        for language in ("en", "fr", "es", "ar"):
            with self.subTest(language=language), (directory / f"{language}.csv").open() as catalog:
                translations = {row[0]: row[1] for row in csv.reader(catalog) if len(row) > 1}
                self.assertEqual(messages - translations.keys(), set())
                for message in messages:
                    self.assertTrue(translations[message].strip())
                    self.assertNotRegex(translations[message], r"(?i)\b(?:ury|huf|kamra|simphony)\b|URY_")
