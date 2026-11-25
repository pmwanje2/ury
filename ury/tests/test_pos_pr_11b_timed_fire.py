"""Timed kitchen release through the real order and ticket generation paths."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Lock, local

from ury.ury.api import test_ury_hold_fire as fixture


class TimedFireTests(fixture.HoldFireTests):
    def schedule(self, **selection):
        return self.order.schedule_held_items(self.invoice.name, **selection)

    def test_duplicate_cart_identity_cannot_create_two_releases(self):
        held = self.row("held", qty=1.25, course="Main")
        held.reservation_line_key = "cart-held"
        request = self.request("held", qty=1.25)
        with self.assertRaises(ValueError):
            self.sync([request.copy(), request.copy()])
        self.order.fire_items(self.invoice.name, line="held")
        self.order.fire_items(self.invoice.name, line="held")
        self.assertEqual(self.ticket_lines(), [("Fish", 1.25, "no onions")])

    def test_schedule_course_and_preserve_schedule_through_edits(self):
        held = self.row("held", qty=1.25)
        self.row("dessert", code="Tea", course="Dessert")
        self.pos_profile.default_fire_delay_minutes = 15
        self.schedule(course="Main")
        self.assertEqual(held.fire_at, datetime(2026, 10, 16, 12, 15))
        self.assertFalse(self.invoice.items[1].get("fire_at"))
        self.sync([self.request("held", qty=1.25, note="extra chilli"),
                   self.request("dessert", code="Tea")])
        self.assertEqual(self.invoice.items[0].fire_at, datetime(2026, 10, 16, 12, 15))
        self.assertEqual(self.ticket_lines(), [])

    def test_worker_fires_only_due_rows_once_and_clears_deadline(self):
        due = self.row("due", qty=1.25)
        future = self.row("future", note="later")
        self.schedule(line="due", fire_at="2026-10-16T12:00:00")
        self.schedule(line="future", delay_minutes=20)
        self.framework.db.sql.side_effect = lambda query, *args, **kw: (
            [(self.invoice.name,)] if "GROUP BY" in query else [(self.invoice.name,)]
        )
        self.order.fire_due_held_orders()
        self.order.fire_due_held_orders()
        self.assertEqual(self.ticket_lines(), [("Fish", 1.25, "no onions")])
        self.assertIsNone(due.fire_at)
        self.assertEqual(future.ury_held, 1)
        self.assertEqual(future.fire_at, datetime(2026, 10, 16, 12, 20))

    def test_cross_outlet_staff_cannot_schedule(self):
        self.row()
        self.order.getBranch.return_value = "Other branch"
        with self.assertRaises(PermissionError):
            self.schedule(line="held-1", delay_minutes=10)
        self.invoice.save.assert_not_called()

    def test_worker_and_manual_fire_share_transaction_lock(self):
        self.row()
        self.schedule(line="held-1", fire_at="2026-10-16T12:00:00")
        lock, state, start = Lock(), local(), Barrier(2)

        def sql(query, *args, **kwargs):
            if "FOR UPDATE" in query and "tabPOS Invoice" in query:
                lock.acquire(timeout=5)
                state.locked = True
            return [(self.invoice.name,)]

        def release_lock():
            if getattr(state, "locked", False):
                state.locked = False
                lock.release()

        self.framework.db.sql.side_effect = sql
        self.framework.db.commit.side_effect = release_lock
        self.framework.db.rollback.side_effect = release_lock

        def run(action):
            start.wait(timeout=5)
            try:
                action()
            finally:
                release_lock()

        with ThreadPoolExecutor(2) as threads:
            results = [
                threads.submit(run, self.order.fire_due_held_orders),
                threads.submit(run, lambda: self.order.fire_items(self.invoice.name, line="held-1")),
            ]
            for result in results:
                result.result(timeout=10)

        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])
        self.assertEqual(sum("FOR UPDATE" in call.args[0]
                             and "tabPOS Invoice" in call.args[0]
                             for call in self.framework.db.sql.call_args_list), 2)

    def test_bump_waits_for_every_earlier_station_and_opens_only_next_course(self):
        mains = self.row("mains", course="Main")
        dessert = self.row("dessert", course="Dessert")
        self.pos_profile.auto_fire_next_course_on_bump = 1
        first = {"name": "hot", "course": "Starter", "order_status": "Served"}
        second = {"name": "cold", "course": "Starter", "order_status": "Ready For Prepare"}
        history = [first, second]
        self.framework.db.sql.side_effect = lambda query, *args, **kw: (
            history if "tabURY KOT Items" in query else [(self.invoice.name,)]
        )
        self.framework.get_all.side_effect = lambda dt, **kw: (
            [{"name": "Starter", "custom_serving_priority": 1},
             {"name": "Main", "custom_serving_priority": 2},
             {"name": "Dessert", "custom_serving_priority": 3}]
            if dt == "URY Menu Course" else []
        )
        self.order._fire_next_held_course(self.invoice, ["hot"])
        self.assertEqual(self.ticket_lines(), [])
        second["order_status"] = "Served"
        self.order._fire_next_held_course(self.invoice, ["cold"])
        self.order._fire_next_held_course(self.invoice, ["cold"])
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])
        self.assertEqual(mains.ury_held, 0)
        self.assertEqual(dessert.ury_held, 1)
