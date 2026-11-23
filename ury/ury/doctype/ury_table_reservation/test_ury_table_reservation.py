# Copyright (c) 2026, Tridz Technologies Pvt. Ltd and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime, add_to_date, get_datetime
from datetime import timedelta
from ury.ury.api.table_reservation import (
    create_table_reservation,
    update_table_reservation,
    update_reservation_status,
    check_table_reservation,
    get_branch_reservation_settings,
)
from ury.ury.api.reservation_scheduler import process_reservation_no_shows
from ury.ury.report.average_table_time.average_table_time import (
    get_branch_reservation_duration,
)


class TestURYTableReservation(FrappeTestCase):
    def setUp(self):
        cust_group = frappe.db.get_value("Customer Group", {"is_group": 0}, "name") or "Individual"
        territory = frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories"
        restaurant = "KFC"
        self.branch = "KFC"

        # Create test customer if not exists
        if not frappe.db.exists("Customer", "_Test Reservation Customer"):
            cust = frappe.get_doc({
                "doctype": "Customer",
                "customer_name": "_Test Reservation Customer",
                "customer_group": cust_group,
                "territory": territory,
                "mobile_number": "9876543210",
            })
            cust.insert(ignore_permissions=True)

        if not frappe.db.exists("Customer", "_Test Customer B"):
            cust_b = frappe.get_doc({
                "doctype": "Customer",
                "customer_name": "_Test Customer B",
                "customer_group": cust_group,
                "territory": territory,
                "mobile_number": "9876543211",
            })
            cust_b.insert(ignore_permissions=True)

        # Ensure branch exists and has reservation settings
        if not frappe.db.exists("Branch", self.branch):
            br = frappe.get_doc({
                "doctype": "Branch",
                "branch": self.branch,
                "user": [{"doctype": "URY User", "user": "Administrator"}],
                "custom_enable_reservation": 1,
                "custom_buffer_time": 30,
                "custom_grace_period": 15,
            })
            br.insert(ignore_permissions=True)
        else:
            frappe.db.set_value("Branch", self.branch, {
                "custom_enable_reservation": 1,
                "custom_buffer_time": 30,
                "custom_grace_period": 15,
            })

        # Create test room if not exists
        if not frappe.db.exists("URY Room", "_Test Reservation Room"):
            room = frappe.get_doc({
                "doctype": "URY Room",
                "name": "_Test Reservation Room",
                "room_name": "_Test Reservation Room",
                "branch": self.branch,
            })
            room.insert(ignore_permissions=True)

        # Create test restaurant if not exists (URY Table links to it, and it
        # requires a company and a default room on version-16)
        if not frappe.db.exists("URY Restaurant", restaurant):
            restaurant_doc = frappe.get_doc({
                "doctype": "URY Restaurant",
                "name": restaurant,
                "restaurant_name": restaurant,
                "company": frappe.db.get_value("Company", {}, "name"),
                "invoice_series_prefix": "KFC-",
                "branch": self.branch,
                "default_room": "_Test Reservation Room",
            })
            restaurant_doc.insert(ignore_permissions=True)

        # Create test table if not exists
        if not frappe.db.exists("URY Table", "_Test Reservation Table 1"):
            table = frappe.get_doc({
                "doctype": "URY Table",
                "name": "_Test Reservation Table 1",
                "table_name": "_Test Reservation Table 1",
                "restaurant": restaurant,
                "restaurant_room": "_Test Reservation Room",
                "branch": self.branch,
                "no_of_seats": 4,
                "occupied": 0,
            })
            table.insert(ignore_permissions=True)

        # Clean up existing test reservations
        frappe.db.sql(
            "DELETE FROM `tabURY Table Reservation` WHERE reserved_table = '_Test Reservation Table 1'"
        )
        frappe.db.commit()

    def tearDown(self):
        frappe.db.sql(
            "DELETE FROM `tabURY Table Reservation` WHERE reserved_table = '_Test Reservation Table 1'"
        )
        frappe.db.commit()

    def test_create_reservation_with_mandatory_fields(self):
        res_time = (now_datetime() + timedelta(days=1)).strftime("%Y-%m-%d 19:00:00")
        res_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Test Res Customer",
            customer_phone="9876543210",
            no_of_pax=4,
            reserved_at=res_time,
            notes="Birthday dinner",
            branch=self.branch,
        )
        self.assertTrue(res_id)
        doc = frappe.get_doc("URY Table Reservation", res_id)
        self.assertEqual(doc.status, "Confirmed")
        self.assertEqual(doc.customer_phone, "9876543210")
        self.assertEqual(doc.no_of_pax, 4)

    def test_same_table_same_time_conflict(self):
        res_time = (now_datetime() + timedelta(days=2)).strftime("%Y-%m-%d 20:00:00")
        create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer A",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=res_time,
            branch=self.branch,
        )

        # Attempting to book exact same time should throw exception
        with self.assertRaises(frappe.ValidationError):
            create_table_reservation(
                table="_Test Reservation Table 1",
                customer="_Test Customer B",
                customer_name="Customer B",
                customer_phone="9876543211",
                no_of_pax=2,
                reserved_at=res_time,
                branch=self.branch,
            )

    def test_overlapping_time_window_conflict_with_buffer(self):
        # Reservation 1: 10:00 PM (22:00). With 90 min duration -> ends at 23:30.
        # Customer B attempts 11:00 PM (23:00) -> overlaps dining window!
        base_date = (now_datetime() + timedelta(days=3)).strftime("%Y-%m-%d")
        res1_time = f"{base_date} 22:00:00"
        res2_time = f"{base_date} 23:00:00"

        create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer A",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=res1_time,
            branch=self.branch,
        )

        with self.assertRaises(frappe.ValidationError):
            create_table_reservation(
                table="_Test Reservation Table 1",
                customer="_Test Customer B",
                customer_name="Customer B",
                customer_phone="9876543211",
                no_of_pax=2,
                reserved_at=res2_time,
                branch=self.branch,
            )

    def test_non_conflicting_reservations_with_buffer_spacing(self):
        # Reservation 1: 18:00 (6:00 PM). Duration 90 mins -> ends at 19:30. Buffer is 30 mins.
        # Reservation 2: 20:00 (8:00 PM). Buffer starts at 19:30 -> Exact fit without conflict!
        base_date = (now_datetime() + timedelta(days=4)).strftime("%Y-%m-%d")
        res1_time = f"{base_date} 18:00:00"
        res2_time = f"{base_date} 20:00:00"

        res1_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer A",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=res1_time,
            branch=self.branch,
        )
        res2_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Customer B",
            customer_name="Customer B",
            customer_phone="9876543211",
            no_of_pax=2,
            reserved_at=res2_time,
            branch=self.branch,
        )
        self.assertTrue(res1_id)
        self.assertTrue(res2_id)

    def test_edit_reservation_and_cancel_flow(self):
        base_date = (now_datetime() + timedelta(days=5)).strftime("%Y-%m-%d")
        res_time = f"{base_date} 19:00:00"

        res_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer A",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=res_time,
            branch=self.branch,
        )

        # Update notes and pax (pax 4 <= seats 4)
        update_table_reservation(
            reservation_name=res_id,
            no_of_pax=4,
            notes="Changed to 4 persons",
        )

        doc = frappe.get_doc("URY Table Reservation", res_id)
        self.assertEqual(doc.no_of_pax, 4)
        self.assertEqual(doc.comments, "Changed to 4 persons")

    def test_capacity_validation(self):
        # Table capacity is 4. Booking 5 persons should fail.
        res_time = (now_datetime() + timedelta(days=6)).strftime("%Y-%m-%d 19:00:00")
        with self.assertRaises(frappe.ValidationError):
            create_table_reservation(
                table="_Test Reservation Table 1",
                customer="_Test Reservation Customer",
                customer_name="Customer Capacity Fail",
                customer_phone="9876543210",
                no_of_pax=5,
                reserved_at=res_time,
                branch=self.branch,
            )

    def test_past_datetime_validation(self):
        # Booking in the past should fail.
        past_time = (now_datetime() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        with self.assertRaises(frappe.ValidationError):
            create_table_reservation(
                table="_Test Reservation Table 1",
                customer="_Test Reservation Customer",
                customer_name="Customer Past Fail",
                customer_phone="9876543210",
                no_of_pax=2,
                reserved_at=past_time,
                branch=self.branch,
            )

    def test_buffer_time_free_table(self):
        # Booking 15 mins in future (inside 30 min buffer) when table is free -> should succeed.
        frappe.db.set_value("URY Table", "_Test Reservation Table 1", "occupied", 0)
        future_time = (now_datetime() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        res_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer Free Buffer",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=future_time,
            branch=self.branch,
        )
        self.assertTrue(res_id)

    def test_buffer_time_occupied_table_conflict(self):
        # Table is occupied starting now. Booking in 15 mins (within 90 min duration) -> should fail.
        frappe.db.set_value("URY Table", "_Test Reservation Table 1", {
            "occupied": 1,
            "latest_invoice_time": now_datetime(),
        })
        future_time = (now_datetime() + timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
        with self.assertRaises(frappe.ValidationError):
            create_table_reservation(
                table="_Test Reservation Table 1",
                customer="_Test Reservation Customer",
                customer_name="Customer Occupied Fail",
                customer_phone="9876543210",
                no_of_pax=2,
                reserved_at=future_time,
                branch=self.branch,
            )
        # Reset table state
        frappe.db.set_value("URY Table", "_Test Reservation Table 1", {"occupied": 0, "latest_invoice_time": None})

    def test_automatic_no_show_handling(self):
        # Create a past reservation whose grace period has expired (e.g. 2 hours ago)
        past_time = (now_datetime() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

        doc = frappe.get_doc({
            "doctype": "URY Table Reservation",
            "branch": self.branch,
            "reserved_table": "_Test Reservation Table 1",
            "customer": "_Test Reservation Customer",
            "customer_name": "Late Customer",
            "customer_phone": "9876543210",
            "no_of_pax": 2,
            "reserved_at": past_time,
            "status": "Confirmed",
        })
        doc.flags.ignore_validate = True
        doc.insert(ignore_permissions=True, ignore_links=True)
        frappe.db.commit()

        # Run the background no-show task
        process_reservation_no_shows()

        doc.reload()
        self.assertEqual(doc.status, "No Show")

    def test_completed_status_transition(self):
        res_time = (now_datetime() + timedelta(days=1)).strftime("%Y-%m-%d 18:00:00")
        res_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer Complete",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=res_time,
            branch=self.branch,
        )
        update_reservation_status(res_id, "Seated")
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Seated")
        update_reservation_status(res_id, "Completed")
        doc = frappe.get_doc("URY Table Reservation", res_id)
        self.assertEqual(doc.status, "Completed")

    def test_cancelled_status_persistence_and_table_release(self):
        res_time = (now_datetime() + timedelta(days=2)).strftime("%Y-%m-%d 18:00:00")
        res1_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer Cancel",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=res_time,
            branch=self.branch,
        )
        # Cancel reservation
        update_reservation_status(res1_id, "Cancelled", cancel_reason="Guest called")
        doc1 = frappe.get_doc("URY Table Reservation", res1_id)
        self.assertEqual(doc1.status, "Cancelled")

        # Booking same table & time after cancellation should succeed because table is released
        res2_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Customer B",
            customer_name="Customer New Booking",
            customer_phone="9876543211",
            no_of_pax=2,
            reserved_at=res_time,
            branch=self.branch,
        )
        self.assertTrue(res2_id)

    def test_invalid_status_transition(self):
        res_time = (now_datetime() + timedelta(days=3)).strftime("%Y-%m-%d 18:00:00")
        res_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer Invalid Transition",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=res_time,
            branch=self.branch,
        )
        update_reservation_status(res_id, "Cancelled", cancel_reason="Guest called")
        with self.assertRaises(frappe.ValidationError):
            update_reservation_status(res_id, "Completed")



class TestConcurrentBookingsOnOneTable(FrappeTestCase):
    """Two hosts book one table and slot at once, each in its own InnoDB transaction.

    The second host's request has already read the site, so its REPEATABLE READ
    snapshot predates the first booking's commit. Once it holds the table it
    must still see that booking and refuse, cleanly. MariaDB 11.8 (the estate)
    runs innodb_snapshot_isolation=ON, where a locking read of a row committed
    after the snapshot raises error 1020; earlier MariaDB and
    innodb_snapshot_isolation=OFF return the current row instead. Both are run.
    """

    setUp = TestURYTableReservation.setUp
    tearDown = TestURYTableReservation.tearDown
    table = "_Test Reservation Table 1"

    def setUp(self):
        TestURYTableReservation.setUp(self)
        self.slot = (now_datetime() + timedelta(days=8)).strftime("%Y-%m-%d 19:00:00")
        if not frappe.db.exists("URY Table", "_Test Reservation Table 2"):
            frappe.get_doc({
                "doctype": "URY Table", "name": "_Test Reservation Table 2",
                "table_name": "_Test Reservation Table 2", "restaurant": "KFC",
                "restaurant_room": "_Test Reservation Room", "branch": self.branch,
                "no_of_seats": 4, "occupied": 0,
            }).insert(ignore_permissions=True)
        frappe.db.sql("DELETE FROM `tabURY Table Reservation` WHERE reserved_table = '_Test Reservation Table 2'")
        frappe.db.commit()

    def tearDown(self):
        frappe.db.sql("DELETE FROM `tabURY Table Reservation` WHERE reserved_table = '_Test Reservation Table 2'")
        TestURYTableReservation.tearDown(self)

    def book(self, customer, reserved_at, table=None):
        return create_table_reservation(
            table=table or self.table, customer=customer, customer_name=customer,
            customer_phone="9876543211", no_of_pax=2, reserved_at=reserved_at, branch=self.branch,
        )

    def race(self, second_host, snapshot_isolation):
        """The first host holds the table with an uncommitted booking while the second one arrives."""
        import threading
        import time

        site, sites_path = frappe.local.site, frappe.local.sites_path
        snapshot_open = threading.Event()
        outcome = {}

        def run():
            frappe.init(site=site, sites_path=sites_path)
            frappe.connect()
            try:
                frappe.db.sql("SET SESSION innodb_snapshot_isolation = %s" % ("ON" if snapshot_isolation else "OFF"))
                frappe.db.commit()
                # The request reads the site first (as every reservation write does),
                # which opens its snapshot before the first booking commits.
                frappe.db.get_value("Branch", self.branch, "custom_enable_reservation")
                snapshot_open.set()
                try:
                    outcome["saved"] = second_host()
                except frappe.ValidationError as refused:
                    outcome["refused"] = str(refused)
                except Exception as error:  # a raw lock or snapshot error is a failure, not a refusal
                    outcome["error"] = repr(error)
                finally:
                    frappe.db.rollback()
            finally:
                frappe.destroy()

        frappe.db.get_value("URY Table", self.table, "name", for_update=True)
        frappe.get_doc({
            "doctype": "URY Table Reservation", "branch": self.branch, "reserved_table": self.table,
            "customer": "_Test Reservation Customer", "customer_name": "Customer A",
            "customer_phone": "9876543210", "no_of_pax": 2, "reserved_at": self.slot, "status": "Confirmed",
        }).insert()
        second = threading.Thread(target=run)
        try:
            second.start()
            self.assertTrue(snapshot_open.wait(30), "the second host never started")
            time.sleep(1.5)  # it reaches the table lock and waits there
        finally:
            frappe.db.commit()
            second.join(90)
        self.assertFalse(second.is_alive(), "the second host is still waiting")
        return outcome

    def assert_refused_once(self, outcome):
        self.assertNotIn("error", outcome, outcome)
        self.assertNotIn("saved", outcome, "the second host double-booked the table")
        self.assertTrue(outcome.get("refused"), outcome)
        self.assertEqual(frappe.db.count("URY Table Reservation", {
            "reserved_table": self.table, "reserved_at": self.slot, "status": "Confirmed"}), 1)

    def test_a_second_booking_is_refused_with_current_reads(self):
        # MariaDB before 11.6.2, or innodb_snapshot_isolation=OFF.
        outcome = self.race(lambda: self.book("_Test Customer B", self.slot), snapshot_isolation=False)
        self.assert_refused_once(outcome)
        self.assertIn("already has a reservation", outcome["refused"])

    def test_a_second_booking_is_refused_cleanly_on_the_estate_s_mariadb(self):
        outcome = self.race(lambda: self.book("_Test Customer B", self.slot), snapshot_isolation=True)
        self.assert_refused_once(outcome)

    def test_moving_a_booking_onto_the_slot_is_refused_on_the_estate_s_mariadb(self):
        # An edit takes no naming-series lock, so only the overlap read can catch it.
        later = self.slot.replace("19:00:00", "22:30:00")
        moved = self.book("_Test Customer B", later)
        frappe.db.commit()
        outcome = self.race(lambda: update_table_reservation(reservation_name=moved, reserved_at=self.slot),
            snapshot_isolation=True)
        self.assert_refused_once(outcome)
        self.assertEqual(str(frappe.db.get_value("URY Table Reservation", moved, "reserved_at")), later)

    def test_moving_a_booking_on_another_table_at_the_same_moment_is_not_refused(self):
        # Guard: the locking read touches only its own table's bookings, so the
        # first host's new booking on table 1 never refuses an edit on table 2.
        # (An insert would share the naming-series row, which is not this check.)
        other = "_Test Reservation Table 2"
        later = self.slot.replace("19:00:00", "22:30:00")
        moved = self.book("_Test Customer B", later, table=other)
        frappe.db.commit()
        outcome = self.race(lambda: update_table_reservation(reservation_name=moved, reserved_at=self.slot),
            snapshot_isolation=True)
        self.assertEqual(outcome, {"saved": True})
        self.assertEqual(str(frappe.db.get_value("URY Table Reservation", moved, "reserved_at")), self.slot)


class TestNativeReservationBranchGuard(FrappeTestCase):
    def test_disabled_branch_refuses_native_save_before_conflict_checks(self):
        from unittest.mock import MagicMock, patch

        from ury.ury.doctype.ury_table_reservation.ury_table_reservation import URYTableReservation

        doc = MagicMock(branch='Branch A')
        with patch('frappe.db.get_value', return_value=0), self.assertRaisesRegex(
            frappe.ValidationError, 'switched off'
        ):
            URYTableReservation.validate(doc)
        doc.validate_conflicts.assert_not_called()
