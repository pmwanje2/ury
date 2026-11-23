"""Site-less contracts for the waiter (Captain) table grid's reservation badges.

URY Table Reservation is the single table-booking record, so the grid reads
Confirmed and Seated reservations from it, never native Appointment. The real
endpoint and the real reservation modules run against the in-memory site of
the reservation lifecycle tests, whose get_list enforces native DocPerm.
"""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from ury.ury.api.test_table_reservation_lifecycle import DAY, Refused, _Site, at


class TestCaptainGridReadsTableReservations(_Site):
    def setUp(self):
        super().setUp()
        self.frappe.whitelist = MagicMock(side_effect=lambda *a, **k: (lambda function: function))
        # The native Appointment path still exists on a site; the grid must not use it.
        self.site.rows["Appointment"] = {}
        self.frappe.db.get_single_value = lambda doctype, field: 120
        self.bookings = self.load("ury.ury.api.ury_table_bookings", "ury/api/ury_table_bookings.py")
        self.clock("18:30")  # no till has closed, so the branch's stay is the 90 minute default

    def read(self, branch="Branch A"):
        result = self.bookings.get_upcoming_bookings(branch)
        for call in self.frappe.get_list.call_args_list + self.frappe.has_permission.call_args_list:
            doctype = call.args[0] if call.args else call.kwargs.get("doctype")
            self.assertNotEqual(doctype, "Appointment", "the grid still reads native Appointment")
        self.assertEqual(self.site.versions, [], "a grid read must not write")
        return result

    def reservation_reads(self):
        return [c for c in self.frappe.get_list.call_args_list if c.args[0] == "URY Table Reservation"]

    def test_confirmed_and_seated_reservations_annotate_their_tables(self):
        self.booking("TR-1", "19:00", guest="Grace Hopper", pax=4)
        self.booking("TR-2", "18:00", table="T2", status="Seated", guest="Ada Lovelace", pax=2)
        for name, status, clock in (("TR-3", "Requested", "18:45"), ("TR-4", "Completed", "18:10"),
                ("TR-5", "Cancelled", "18:40"), ("TR-6", "No Show", "18:20")):
            self.booking(name, clock, table="T3", status=status)
        self.assertEqual(self.read(), {
            "T1": {"scheduled_time": f"{DAY} 19:00:00", "party_size": 4, "customer_name": "Grace Hopper",
                "status": "Confirmed", "server_now": f"{DAY} 18:30:00"},
            "T2": {"scheduled_time": f"{DAY} 18:00:00", "party_size": 2, "customer_name": "Ada Lovelace",
                "status": "Seated", "server_now": f"{DAY} 18:30:00"},
        })
        self.frappe.whitelist.assert_called_once_with(methods=["GET"])

    def test_a_branch_with_reservations_off_returns_nothing_and_reads_nothing(self):
        self.booking("TR-9", "19:00", table="T9", branch="Branch Off")
        self.assertEqual(self.read("Branch Off"), {})
        self.assertEqual(self.reservation_reads(), [])

    def test_an_empty_branch_never_reads_across_branches(self):
        self.booking("TR-1", "19:00")
        self.assertEqual(self.read(""), {})
        self.assertEqual(self.reservation_reads(), [])

    def test_no_reservation_read_permission_returns_nothing(self):
        self.site.roles = ["Guest Viewer"]
        self.booking("TR-1", "19:00")
        self.assertEqual(self.read(), {})
        self.frappe.has_permission.assert_any_call("URY Table Reservation", "read")

    def test_a_permission_denial_at_the_native_list_boundary_returns_nothing(self):
        self.booking("TR-1", "19:00")
        self.frappe.get_list.side_effect = Refused("No read access")
        self.assertEqual(self.read(), {})

    def test_another_branchs_reservations_are_not_returned(self):
        self.booking("TR-9", "19:00", table="T9", branch="Branch Off")
        self.assertEqual(self.read(), {})

    def test_the_window_is_the_branch_stay_on_either_side_of_the_site_clock(self):
        # 18:30 with a 90 minute stay: 17:00 to 20:00 inclusive.
        self.booking("TR-1", "17:00")
        self.booking("TR-2", "20:00", table="T2")
        self.booking("TR-3", "16:59:59", table="T3")
        self.booking("TR-4", "20:00:01", table="T3")
        self.assertEqual(set(self.read()), {"T1", "T2"})

    def test_the_nearest_reservation_per_table_wins_even_if_rows_are_unsorted(self):
        self.booking("TR-A", "20:00")
        self.booking("TR-B", "18:20", guest="Nearest Guest")
        self.booking("TR-C", "19:00")
        self.booking("TR-D", "18:45", table="T2")
        result = self.read()
        self.assertEqual(result["T1"]["customer_name"], "Nearest Guest")
        self.assertEqual(result["T2"]["scheduled_time"], f"{DAY} 18:45:00")

    def test_one_bulk_read_serves_the_whole_branch(self):
        for number in range(25):
            self.table(f"T{number + 10}", 4)
            self.booking(f"TR-{number}", "19:00", table=f"T{number + 10}")
        per_table = []
        get_value = self.frappe.db.get_value
        self.frappe.db.get_value = lambda doctype, *a, **k: (per_table.append(doctype), get_value(doctype, *a, **k))[1]
        self.assertEqual(len(self.read()), 25)
        self.assertEqual(len(self.reservation_reads()), 1)
        self.assertEqual(self.reservation_reads()[0].kwargs.get("limit_page_length"), 0)
        self.assertNotIn("URY Table Reservation", per_table)
        self.assertNotIn("URY Table", per_table)

    def test_the_window_crosses_midnight_on_the_site_clock(self):
        self.booking("TR-1", "23:45")
        self.site.now = at("23:45") + timedelta(minutes=45)  # 00:30 the next day
        self.assertEqual(self.read()["T1"]["server_now"], "2026-11-24 00:30:00")


if __name__ == "__main__":
    unittest.main()
