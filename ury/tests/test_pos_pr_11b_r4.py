"""R4 regressions for cancellation allocation and white-label confirmation errors."""

from unittest.mock import Mock

from ury.ury.api import test_ury_hold_fire as fixture


class R4CancellationTests(fixture.HoldFireTests):
    def test_later_confirmation_reserves_units_for_earlier_pending_cancel(self):
        self.pos_profile.auto_fire_next_course_on_bump = 1
        self.row("starter", held=0, qty=3, course="Starter")
        dessert = self.row("dessert", course="Dessert")
        starter_tickets = [
            dict(name="older", course="Starter", order_status="Served", has_remaining_qty=1),
            dict(name="newer", course="Starter", order_status="Ready For Prepare", has_remaining_qty=1),
        ]
        cancels = [
            dict(name="first", type="Partially cancelled", original_kot="newer", verified=0),
            dict(name="second", type="Partially cancelled", original_kot="newer,older", verified=1),
        ]
        history = [
            dict(parent="older", item="Fish", quantity=1, cancelled_qty=0),
            dict(parent="newer", item="Fish", quantity=2, cancelled_qty=0),
            dict(parent="first", item="Fish", quantity=2, cancelled_qty=1),
            dict(parent="second", item="Fish", quantity=3, cancelled_qty=2),
        ]

        def get_values(doctype, filters, _fields, **kwargs):
            if doctype == "URY KOT Items":
                return [row for row in history if row["parent"] in filters["parent"][1]]
            return [row for row in cancels if filters.get("verified", row["verified"]) == row["verified"]]

        self.framework.db.get_values.side_effect = get_values
        self.framework.db.sql.side_effect = lambda query, *args, **kw: starter_tickets if "tabURY KOT Items" in query else []
        self.framework.get_all.side_effect = lambda dt, **kw: (
            [dict(name="Starter", custom_serving_priority=1), dict(name="Dessert", custom_serving_priority=2)]
            if dt == "URY Menu Course" else get_values(dt, kw["filters"], kw["fields"])
        )
        display = self.load_module("ury.ury.api.ury_kot_display", "ury_kot_display.py")

        self.order._fire_next_held_course(self.invoice, ["older", "newer"])
        self.assertEqual(dessert.ury_held, 1)
        self.assertEqual(self.tickets, [])
        self.assertEqual(display._get_cancel_confirmed_original_kots("Branch A"), {"older"})

        # A serve cannot bypass a cancellation that is still awaiting acknowledgement.
        starter_tickets[1]["order_status"] = "Served"
        self.order._fire_next_held_course(self.invoice, ["newer"])
        self.assertEqual(dessert.ury_held, 1)

        cancels[0]["verified"] = 1
        self.order._fire_next_held_course(self.invoice, ["newer"])
        self.order._fire_next_held_course(self.invoice, ["newer"])
        self.assertEqual(dessert.ury_held, 0)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])

    def test_missing_cancel_ticket_never_queues_the_internal_doctype_name(self):
        display = self.load_module("ury.ury.api.ury_kot_display", "ury_kot_display.py")
        self.framework.get_roles.return_value = ["URY Manager"]
        self.framework.DoesNotExistError = LookupError
        self.framework.throw.side_effect = None

        def throw(message, exception):
            self.framework.local.message_log.append(message)
            raise exception(message)

        def missing_document(_doctype, name):
            throw(f"URY KOT {name} not found", LookupError)

        self.framework.throw.side_effect = throw
        for exists in (False, True):
            with self.subTest(exists=exists):
                # True simulates a ticket removed between exists() and get_doc().
                self.framework.db.exists = Mock(return_value=exists)
                self.framework.get_doc = Mock(side_effect=missing_document)
                self.framework.local.message_log = ["Previous message"]
                with self.assertRaisesRegex(LookupError, "Kitchen ticket KOT-000 was not found"):
                    display.confirm_cancel_kot("KOT-000")
                self.assertEqual(self.framework.local.message_log, [
                    "Previous message", "Kitchen ticket KOT-000 was not found.",
                ])
                if not exists:
                    self.framework.get_doc.assert_not_called()
