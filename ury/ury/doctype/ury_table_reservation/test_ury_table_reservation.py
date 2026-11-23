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
    get_active_reservations,
    get_available_tables_for_reservation,
    get_branch_reservation_settings,
)
from ury.ury.api.reservation_scheduler import process_reservation_no_shows


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

    def test_the_branch_minimum_notice_refuses_a_booking_inside_it(self):
        # 45, not the branch's 30-minute buffer, whose occupied-table refusal also says "30 minutes".
        frappe.db.set_value("Branch", self.branch, "custom_min_reservation_notice", 45)

        def restore_seeded_notice():
            # tearDown commits before cleanups run, so the restore commits too.
            frappe.db.set_value("Branch", self.branch, "custom_min_reservation_notice", -1)
            frappe.db.commit()

        self.addCleanup(restore_seeded_notice)
        soon = (now_datetime() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        with self.assertRaisesRegex(frappe.ValidationError, "at least 45 minutes' notice"):
            create_table_reservation(
                table="_Test Reservation Table 1",
                customer="_Test Reservation Customer",
                customer_name="Customer Short Notice",
                customer_phone="9876543210",
                no_of_pax=2,
                reserved_at=soon,
                branch=self.branch,
            )

    def test_an_edit_inside_the_notice_that_keeps_the_time_is_saved(self):
        # Simphony's Minimum Reservation Notice applies to the booking time, not to notes or covers.
        frappe.db.set_value("URY Table", "_Test Reservation Table 1", {"occupied": 0, "latest_invoice_time": None})
        booked_for = (now_datetime() + timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:00")
        res_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer Calls Ahead",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=booked_for,
            branch=self.branch,
        )
        frappe.db.set_value("Branch", self.branch, "custom_min_reservation_notice", 90)

        def restore_seeded_notice():
            frappe.db.set_value("Branch", self.branch, "custom_min_reservation_notice", -1)
            frappe.db.commit()

        self.addCleanup(restore_seeded_notice)
        update_table_reservation(reservation_name=res_id, notes="Needs a high chair", no_of_pax=3)
        doc = frappe.get_doc("URY Table Reservation", res_id)
        self.assertEqual((doc.comments, doc.no_of_pax), ("Needs a high chair", 3))
        moved = (now_datetime() + timedelta(minutes=70)).strftime("%Y-%m-%d %H:%M:00")
        with self.assertRaisesRegex(frappe.ValidationError, "at least 90 minutes' notice"):
            update_table_reservation(reservation_name=res_id, reserved_at=moved)

    def test_walk_in_in_buffer_does_not_block_non_time_edit_on_same_table(self):
        # P1-3 (probe 6): a walk-in whose expected finish runs into the booking buffer
        # must not block edits that keep both table and time unchanged.
        frappe.db.set_value("URY Table", "_Test Reservation Table 1", {"occupied": 0, "latest_invoice_time": None})
        booked_for = (now_datetime() + timedelta(minutes=35)).strftime("%Y-%m-%d %H:%M:00")
        res_id = create_table_reservation(
            table="_Test Reservation Table 1",
            customer="_Test Reservation Customer",
            customer_name="Customer Walkin Buffer",
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=booked_for,
            branch=self.branch,
        )
        frappe.db.set_value("URY Table", "_Test Reservation Table 1", {
            "occupied": 1,
            "latest_invoice_time": now_datetime() - timedelta(minutes=45),
        })
        self.addCleanup(
            lambda: (
                frappe.db.set_value("URY Table", "_Test Reservation Table 1", {"occupied": 0, "latest_invoice_time": None}),
                frappe.db.commit(),
            )
        )
        avail = [t["name"] for t in get_available_tables_for_reservation(
            self.branch, booked_for, exclude_reservation=res_id
        )]
        self.assertIn("_Test Reservation Table 1", avail)
        update_table_reservation(reservation_name=res_id, notes="Needs high chair", no_of_pax=3)
        doc = frappe.get_doc("URY Table Reservation", res_id)
        self.assertEqual((doc.comments, doc.no_of_pax), ("Needs high chair", 3))
        moved = (now_datetime() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:00")
        with self.assertRaisesRegex(frappe.ValidationError, "currently occupied"):
            update_table_reservation(reservation_name=res_id, reserved_at=moved)

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


# R3/R4: each reservation timing is a Branch setting seeded by the Custom Field fixture.
TIMING_SEEDS = {
    "custom_grace_period": "15",
    "custom_unset_grace_policy": "Use Default Grace Period",
    "custom_min_reservation_notice": "-1",
    "custom_target_turn_time": "60",
}


class TestBranchReservationTimingOnTheSite(FrappeTestCase):
    """On a migrated site each timing is a seeded Branch column, and the grace policy runs on Branch save."""

    def test_each_timing_is_a_branch_column_with_its_seed(self):
        meta = frappe.get_meta("Branch")
        for fieldname, seed in TIMING_SEEDS.items():
            with self.subTest(field=fieldname):
                field = meta.get_field(fieldname)
                self.assertIsNotNone(field, f"Branch has no {fieldname} setting")
                self.assertEqual(str(field.default), seed)
                # The column default is what an existing Branch row gets when migrate adds the column.
                column = frappe.db.sql(
                    """SELECT COLUMN_DEFAULT FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabBranch' AND COLUMN_NAME = %s""",
                    fieldname,
                )
                self.assertEqual(str(column[0][0]).strip("'") if column else None, seed)
        new_branch = frappe.new_doc("Branch")
        self.assertEqual({fieldname: str(new_branch.get(fieldname)) for fieldname in TIMING_SEEDS}, TIMING_SEEDS)

    def test_refuse_to_enable_stops_a_branch_without_a_grace_on_save(self):
        name = "_Test R3 Grace Policy"

        def branch(grace):
            return frappe.get_doc({
                "doctype": "Branch",
                "branch": name,
                "user": [{"doctype": "URY User", "user": "Administrator"}],
                "custom_enable_reservation": 1,
                "custom_unset_grace_policy": "Refuse to Enable",
                "custom_grace_period": grace,
            })

        with self.assertRaisesRegex(frappe.ValidationError, "grace period"):
            branch(0).insert(ignore_permissions=True)
        self.assertFalse(frappe.db.exists("Branch", name))
        branch(10).insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "Branch", name, force=1, ignore_permissions=True)
        for bad_grace in (0, None, "", -5):
            with self.subTest(grace=bad_grace):
                saved = frappe.get_doc("Branch", name)
                saved.custom_grace_period = bad_grace
                with self.assertRaisesRegex(frappe.ValidationError, "grace period"):
                    saved.save(ignore_permissions=True)

        # Under Use Default Grace Period, clearing/zero/negative restores the seeded default on save.
        for unset_grace in (None, "", 0, -5):
            with self.subTest(default_policy_grace=unset_grace):
                saved = frappe.get_doc("Branch", name)
                saved.custom_unset_grace_policy = "Use Default Grace Period"
                saved.custom_grace_period = unset_grace
                saved.save(ignore_permissions=True)
                self.assertEqual(frappe.db.get_value("Branch", name, "custom_grace_period"), 15)

    def test_branch_save_refuses_a_cleared_turn_time_on_a_branch_taking_bookings(self):
        # Clearing the Int field in Desk sends None, which Frappe otherwise stores as 0.
        name = "_Test R4 Turn Time"

        def branch(turn):
            return frappe.get_doc({
                "doctype": "Branch",
                "branch": name,
                "user": [{"doctype": "URY User", "user": "Administrator"}],
                "custom_enable_reservation": 1,
                "custom_target_turn_time": turn,
            })

        with self.assertRaisesRegex(frappe.ValidationError, "target turn time"):
            branch(0).insert(ignore_permissions=True)
        self.assertFalse(frappe.db.exists("Branch", name))
        branch(45).insert(ignore_permissions=True)
        self.addCleanup(frappe.delete_doc, "Branch", name, force=1, ignore_permissions=True)
        # Saving an existing enabled Branch with None (cleared in Desk), blank, 0 or negative must be refused.
        for cleared in (None, "", 0, -5):
            with self.subTest(turn=cleared):
                saved = frappe.get_doc("Branch", name)
                saved.custom_target_turn_time = cleared
                with self.assertRaisesRegex(frappe.ValidationError, "target turn time"):
                    saved.save(ignore_permissions=True)
                self.assertEqual(frappe.db.get_value("Branch", name, "custom_target_turn_time"), 45)


class TestSeatedReservationCheckIntegration(FrappeTestCase):
    """Real bench integration for transfer (5A), split_bill (5B), discard (5C) and merged tables (5D)."""

    def setUp(self):
        from frappe.utils import nowdate

        TestURYTableReservation.setUp(self)
        self.company = frappe.db.get_value("Company", {}, "name")
        self.currency = frappe.db.get_value("Company", self.company, "default_currency") or "INR"
        year = nowdate()[:4]
        if not frappe.db.exists("Fiscal Year", year):
            frappe.get_doc({
                "doctype": "Fiscal Year",
                "year": year,
                "year_start_date": f"{year}-01-01",
                "year_end_date": f"{year}-12-31",
            }).insert(ignore_permissions=True)

        self.tables = ("_Test Reservation Table 1", "_Test Reservation Table 2", "_Test Reservation Table 3")
        for tname in self.tables[1:]:
            if not frappe.db.exists("URY Table", tname):
                frappe.get_doc({
                    "doctype": "URY Table",
                    "name": tname,
                    "table_name": tname,
                    "restaurant": "KFC",
                    "restaurant_room": "_Test Reservation Room",
                    "branch": self.branch,
                    "no_of_seats": 4,
                    "occupied": 0,
                }).insert(ignore_permissions=True)

        if not frappe.db.exists("Item Group", "All Item Groups"):
            frappe.get_doc({"doctype": "Item Group", "item_group_name": "All Item Groups", "is_group": 1}).insert(ignore_permissions=True)
        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups"
        uom = frappe.db.get_value("UOM", {}, "name") or "Nos"
        if not frappe.db.exists("Item", "_Test Res Dish"):
            frappe.get_doc({
                "doctype": "Item",
                "item_code": "_Test Res Dish",
                "item_name": "_Test Res Dish",
                "item_group": item_group,
                "stock_uom": uom,
                "is_stock_item": 0,
                "is_sales_item": 1,
                "standard_rate": 20,
            }).insert(ignore_permissions=True)

        if not frappe.db.exists("URY Menu", "_Test Res Menu"):
            frappe.get_doc({
                "doctype": "URY Menu",
                "name": "_Test Res Menu",
                "menu_name": "_Test Res Menu",
                "branch": self.branch,
                "enabled": 1,
                "items": [{"item": "_Test Res Dish", "item_name": "_Test Res Dish", "rate": 20}],
            }).insert(ignore_permissions=True)

        self.price_list = "_Test Res Selling"
        if not frappe.db.exists("Price List", self.price_list):
            frappe.get_doc({
                "doctype": "Price List",
                "price_list_name": self.price_list,
                "enabled": 1,
                "selling": 1,
                "currency": self.currency,
                "restaurant_menu": "_Test Res Menu",
            }).insert(ignore_permissions=True)
        else:
            frappe.db.set_value("Price List", self.price_list, {"restaurant_menu": "_Test Res Menu", "enabled": 1})

        if not frappe.db.exists("Item Price", {"item_code": "_Test Res Dish", "price_list": self.price_list}):
            frappe.get_doc({
                "doctype": "Item Price",
                "item_code": "_Test Res Dish",
                "price_list": self.price_list,
                "price_list_rate": 20,
                "selling": 1,
            }).insert(ignore_permissions=True)

        frappe.db.set_value("URY Restaurant", "KFC", "active_menu", "_Test Res Menu")

        warehouse = frappe.db.get_value("Warehouse", {"company": self.company, "is_group": 0}, "name")
        cost_center = frappe.db.get_value("Company", self.company, "cost_center") or frappe.db.get_value("Cost Center", {"company": self.company, "is_group": 0}, "name")
        income_account = frappe.db.get_value("Company", self.company, "default_income_account") or frappe.db.get_value("Account", {"company": self.company, "account_type": "Income Account", "is_group": 0}, "name")
        write_off_account = frappe.db.get_value("Company", self.company, "write_off_account") or frappe.db.get_value("Account", {"company": self.company, "root_type": "Expense", "is_group": 0}, "name")
        cash_account = frappe.db.get_value("Company", self.company, "default_cash_account") or frappe.db.get_value("Account", {"company": self.company, "account_type": "Cash", "is_group": 0}, "name")

        if not frappe.db.exists("Mode of Payment", "Cash"):
            frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "Cash", "type": "Cash", "enabled": 1}).insert(ignore_permissions=True)
        mop = frappe.get_doc("Mode of Payment", "Cash")
        if not any(a.company == self.company for a in mop.accounts):
            mop.append("accounts", {"company": self.company, "default_account": cash_account})
            mop.save(ignore_permissions=True)

        self.pos_profile = "_Test Res POS Profile"
        if not frappe.db.exists("POS Profile", self.pos_profile):
            frappe.get_doc({
                "doctype": "POS Profile",
                "name": self.pos_profile,
                "company": self.company,
                "branch": self.branch,
                "restaurant": "KFC",
                "currency": self.currency,
                "warehouse": warehouse,
                "selling_price_list": self.price_list,
                "write_off_account": write_off_account,
                "write_off_cost_center": cost_center,
                "income_account": income_account,
                "cost_center": cost_center,
                "customer": "_Test Reservation Customer",
                "custom_enable_payment_without_printing": 1,
                "payments": [{"mode_of_payment": "Cash", "default": 1}],
                "applicable_for_users": [{"user": "Administrator", "default": 1}],
                "role_allowed_for_billing": [{"role": "System Manager"}],
                "transfer_role_permissions": [{"role": "System Manager"}],
                "view_all_status": 1,
            }).insert(ignore_permissions=True)

        if not frappe.db.exists("POS Opening Entry", {"pos_profile": self.pos_profile, "status": "Open", "docstatus": 1}):
            poe = frappe.get_doc({
                "doctype": "POS Opening Entry",
                "period_start_date": now_datetime(),
                "posting_date": nowdate(),
                "company": self.company,
                "pos_profile": self.pos_profile,
                "branch": self.branch,
                "restaurant": "KFC",
                "user": "Administrator",
                "balance_details": [{"mode_of_payment": "Cash", "opening_amount": 0}],
            })
            poe.insert(ignore_permissions=True)
            poe.submit()

        self._clean()

    def _clean(self):
        for t in self.tables:
            frappe.db.set_value("URY Table", t, {"occupied": 0, "latest_invoice_time": None, "merged_with": None})
        for name in frappe.db.get_all("URY Table Reservation", filters={"reserved_table": ["in", list(self.tables)]}, pluck="name"):
            frappe.delete_doc("URY Table Reservation", name, force=1, ignore_permissions=True)
        for inv in frappe.db.get_all("POS Invoice", filters={"pos_profile": self.pos_profile}, fields=["name", "docstatus"]):
            if inv.docstatus == 1:
                frappe.get_doc("POS Invoice", inv.name).cancel()
            frappe.delete_doc("POS Invoice", inv.name, force=1, ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        self._clean()
        TestURYTableReservation.tearDown(self)

    def seat_party(self, table="_Test Reservation Table 1", customer="_Test Reservation Customer"):
        slot = (now_datetime() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:00")
        res_id = create_table_reservation(
            table=table,
            customer=customer,
            customer_name=customer,
            customer_phone="9876543210",
            no_of_pax=2,
            reserved_at=slot,
            branch=self.branch,
        )
        update_reservation_status(res_id, "Seated")
        return res_id

    def open_check(self, table="_Test Reservation Table 1", qty=1, extra=None):
        doc = frappe.get_doc({
            "doctype": "POS Invoice",
            "customer": "_Test Reservation Customer",
            "company": self.company,
            "branch": self.branch,
            "restaurant": "KFC",
            "restaurant_table": table,
            "order_type": "Dine In",
            "pos_profile": self.pos_profile,
            "selling_price_list": self.price_list,
            "currency": self.currency,
            "no_of_pax": 2,
            "is_pos": 1,
            "payments": [{"mode_of_payment": "Cash", "amount": 0}],
            "items": [{"item_code": "_Test Res Dish", "qty": qty, "rate": 20}],
            **(extra or {}),
        })
        doc.insert(ignore_permissions=True)
        frappe.db.set_value("URY Table", table, {"occupied": 1, "latest_invoice_time": doc.creation})
        frappe.db.commit()
        return doc

    def settle_check(self, inv_name):
        doc = frappe.get_doc("POS Invoice", inv_name)
        doc.is_pos = 1
        doc.payments = []
        amount = doc.rounded_total or doc.grand_total or doc.total
        doc.append("payments", {"mode_of_payment": "Cash", "amount": amount})
        doc.paid_amount = amount
        doc.save(ignore_permissions=True)
        doc.submit()
        frappe.db.commit()
        return doc

    def test_table_transfer_moves_bound_seated_reservation_and_survives_sweep(self):
        from ury.ury.doctype.ury_order.ury_order import table_transfer

        res_id = self.seat_party("_Test Reservation Table 1")
        inv = self.open_check("_Test Reservation Table 1")
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "pos_invoice"), inv.name)

        table_transfer("_Test Reservation Table 1", "_Test Reservation Table 2", inv.name)
        frappe.db.commit()

        after_xfer = frappe.db.get_value(
            "URY Table Reservation", res_id, ["status", "reserved_table", "pos_invoice"], as_dict=True
        )
        self.assertEqual(
            (after_xfer.status, after_xfer.reserved_table, after_xfer.pos_invoice),
            ("Seated", "_Test Reservation Table 2", inv.name),
        )
        # Floor poll / scheduler sweep must NOT mark the live transferred check Completed.
        get_active_reservations(self.branch)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Seated")
        # Settling the transferred check on Table 2 completes the reservation.
        self.settle_check(inv.name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Completed")

    def test_split_bill_completes_seated_visit_when_split_child_settles_last(self):
        from ury.ury.doctype.ury_order.ury_order import split_bill

        res_id = self.seat_party("_Test Reservation Table 1")
        inv1 = self.open_check("_Test Reservation Table 1", qty=2)
        frappe.db.set_value("POS Invoice", inv1.name, "invoice_printed", 1)
        frappe.db.commit()

        split_res = split_bill(inv1.name, [{"name": inv1.items[0].name, "qty": 1}])
        inv2_name = split_res["new_invoice"]
        frappe.db.commit()

        # Primary settles first: visit stays Seated while split child is still open.
        self.settle_check(inv1.name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Seated")
        get_active_reservations(self.branch)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Seated")

        # Split child settles last: visit completes immediately on submit.
        self.settle_check(inv2_name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Completed")

    def test_discarded_check_releases_binding_for_replacement_check(self):
        res_id = self.seat_party("_Test Reservation Table 1")
        inv1 = self.open_check("_Test Reservation Table 1")
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "pos_invoice"), inv1.name)

        inv1.discard()
        frappe.db.set_value("URY Table", "_Test Reservation Table 1", "occupied", 0)
        frappe.db.commit()
        self.assertIsNone(frappe.db.get_value("URY Table Reservation", res_id, "pos_invoice"))

        inv2 = self.open_check("_Test Reservation Table 1")
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "pos_invoice"), inv2.name)
        self.settle_check(inv2.name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Completed")

    def test_merged_table_check_on_partner_binds_seated_reservation(self):
        from ury.ury.doctype.ury_order.ury_order import merge_free_tables

        merge_free_tables("_Test Reservation Table 1", "_Test Reservation Table 2")
        frappe.db.commit()
        res_id = self.seat_party("_Test Reservation Table 1")
        inv = self.open_check("_Test Reservation Table 2", extra={"custom_merged_tables": "_Test Reservation Table 1"})
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "pos_invoice"), inv.name)
        self.settle_check(inv.name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Completed")

    def test_table_transfer_refuses_target_table_with_another_seated_party(self):
        from ury.ury.doctype.ury_order.ury_order import table_transfer

        res1 = self.seat_party("_Test Reservation Table 1")
        inv1 = self.open_check("_Test Reservation Table 1")
        self.seat_party("_Test Reservation Table 2", customer="_Test Customer B")
        # Even if Table 2's occupied flag were cleared, a second Seated party must still be refused.
        frappe.db.set_value("URY Table", "_Test Reservation Table 2", "occupied", 0)
        frappe.db.commit()
        with self.assertRaisesRegex(frappe.ValidationError, "already seated"):
            table_transfer("_Test Reservation Table 1", "_Test Reservation Table 2", inv1.name)
        self.assertEqual(
            frappe.db.get_value("URY Table Reservation", res1, "reserved_table"),
            "_Test Reservation Table 1",
        )

    def test_split_bill_with_transferred_child_completes_when_last_check_settles(self):
        from ury.ury.doctype.ury_order.ury_order import split_bill, table_transfer

        res_id = self.seat_party("_Test Reservation Table 1")
        inv1 = self.open_check("_Test Reservation Table 1", qty=2)
        frappe.db.set_value("POS Invoice", inv1.name, "invoice_printed", 1)
        frappe.db.commit()

        split_res = split_bill(inv1.name, [{"name": inv1.items[0].name, "qty": 1}])
        inv2_name = split_res["new_invoice"]
        frappe.db.commit()

        # Transfer split child to Table 2 while bound primary stays on Table 1.
        table_transfer("_Test Reservation Table 1", "_Test Reservation Table 2", inv2_name)
        frappe.db.commit()
        self.assertEqual(
            frappe.db.get_value("URY Table Reservation", res_id, "reserved_table"),
            "_Test Reservation Table 1",
        )
        # Settling primary on Table 1 first leaves visit Seated while split child is open on Table 2.
        self.settle_check(inv1.name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Seated")
        get_active_reservations(self.branch)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Seated")
        # Settling split child on Table 2 last completes the visit.
        self.settle_check(inv2_name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Completed")

    def test_merged_partner_check_unmerged_and_transferred_moves_seated_reservation(self):
        from ury.ury.doctype.ury_order.ury_order import merge_free_tables, table_transfer, unmerge_tables

        merge_free_tables("_Test Reservation Table 1", "_Test Reservation Table 2")
        frappe.db.commit()
        res_id = self.seat_party("_Test Reservation Table 1")
        inv = self.open_check("_Test Reservation Table 2", extra={"custom_merged_tables": "_Test Reservation Table 1"})
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "pos_invoice"), inv.name)
        # Print check, unmerge, then transfer check from Table 2 to Table 3: Seated reservation moves to Table 3.
        frappe.db.set_value("POS Invoice", inv.name, "invoice_printed", 1)
        frappe.db.commit()
        unmerge_tables("_Test Reservation Table 1")
        table_transfer("_Test Reservation Table 2", "_Test Reservation Table 3", inv.name)
        frappe.db.commit()
        self.assertEqual(
            frappe.db.get_value("URY Table Reservation", res_id, "reserved_table"),
            "_Test Reservation Table 3",
        )
        get_active_reservations(self.branch)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Seated")
        self.settle_check(inv.name)
        self.assertEqual(frappe.db.get_value("URY Table Reservation", res_id, "status"), "Completed")
