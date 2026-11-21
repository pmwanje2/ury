"""Site-less unittest regressions: real endpoint, mocked Frappe boundaries."""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest import TestCase
from unittest.mock import MagicMock, patch


class TestUpcomingBookings(TestCase):
    def setUp(self):
        path = Path(__file__).with_name("ury_table_bookings.py")
        self.assertTrue(path.exists(), "The read-only upcoming-bookings endpoint is missing")
        self.now = datetime(2026, 10, 14, 18, 30)
        self.rows = []
        self.fields = {"custom_ury_table", "custom_party_size"}
        self.tables = ["Table 1", "Table 2", "Table 3"]
        self.framework = MagicMock()
        self.framework.PermissionError = PermissionError
        self.framework.whitelist.side_effect = lambda **kwargs: lambda function: function
        self.framework.has_permission.return_value = True
        self.framework.get_meta.return_value.has_field.side_effect = self.fields.__contains__
        self.framework.db.get_single_value.return_value = 120
        self.framework.get_list.side_effect = self.read_permitted_rows
        utils = ModuleType("frappe.utils")
        utils.now_datetime = MagicMock(side_effect=lambda: self.now)
        utils.get_datetime = lambda value: value if isinstance(value, datetime) else datetime.fromisoformat(value)
        self.clock = utils.now_datetime
        self.enterContext(patch.dict(sys.modules, {"frappe": self.framework, "frappe.utils": utils}))
        spec = importlib.util.spec_from_file_location("x55_bookings_under_test", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def read_permitted_rows(self, doctype, **kwargs):
        # These are native database boundaries: rejecting unrestricted reads
        # catches a real permission bypass, not a mock implementation detail.
        self.assertNotIn("ignore_permissions", kwargs)
        self.assertEqual(kwargs["limit_page_length"], 0)
        if doctype == "URY Table":
            self.assertEqual(kwargs["filters"], {"branch": "Branch A"})
            self.assertEqual(kwargs["pluck"], "name")
            return self.tables
        self.assertEqual(doctype, "Appointment")
        filters = kwargs["filters"]
        self.assertEqual(filters["status"], "Open")
        self.assertEqual(filters["custom_ury_table"], ["in", self.tables])
        operator, (start, end) = filters["scheduled_time"]
        self.assertEqual(operator, "between")
        self.window = (start, end)
        fields = kwargs["fields"]
        if "custom_party_size" not in self.fields:
            self.assertNotIn("custom_party_size", fields)
        rows = [row for row in self.rows if row["status"] == "Open"
                and row["custom_ury_table"] in self.tables
                and start <= datetime.fromisoformat(row["scheduled_time"]) <= end]
        return [{field: row.get(field) for field in fields} for row in rows]

    def booking(self, time, table="Table 1", status="Open", size=4, name="Grace Hopper"):
        return {"name": f"{table}-{time}", "scheduled_time": time,
                "custom_ury_table": table, "status": status,
                "custom_party_size": size, "customer_name": name}

    def read(self, branch="Branch A"):
        result = self.module.get_upcoming_bookings(branch)
        self.framework.get_all.assert_not_called()
        self.framework.db.sql.assert_not_called()
        self.framework.db.set_value.assert_not_called()
        return result

    def test_no_generic_table_field_returns_empty_without_reading_bookings(self):
        self.fields.remove("custom_ury_table")
        self.assertEqual(self.read(), {})
        self.framework.get_list.assert_not_called()

    def test_no_appointment_read_permission_returns_empty(self):
        self.framework.has_permission.return_value = False
        self.assertEqual(self.read(), {})
        self.framework.has_permission.assert_called_once_with("Appointment", "read")
        self.framework.get_list.assert_not_called()

    def test_permission_denial_at_native_list_boundary_returns_empty(self):
        self.framework.get_list.side_effect = PermissionError("No read access")
        self.assertEqual(self.read(), {})

    def test_empty_branch_never_reads_across_branches(self):
        self.assertEqual(self.read(""), {})
        self.framework.get_list.assert_not_called()

    def test_branch_without_visible_tables_returns_empty(self):
        self.tables = []
        self.assertEqual(self.read(), {})
        self.assertEqual(self.framework.get_list.call_count, 1)

    def test_open_booking_returns_only_badge_data_and_site_local_now(self):
        self.rows = [self.booking("2026-10-14 19:00:00")]
        self.assertEqual(self.read(), {"Table 1": {
            "scheduled_time": "2026-10-14 19:00:00", "party_size": 4,
            "customer_name": "Grace Hopper", "server_now": "2026-10-14 18:30:00",
        }})
        self.framework.whitelist.assert_called_once_with(methods=["GET"])

    def test_party_size_field_is_optional(self):
        self.fields.remove("custom_party_size")
        self.rows = [self.booking("2026-10-14 19:00:00")]
        self.assertIsNone(self.read()["Table 1"]["party_size"])

    def test_window_edges_are_inclusive_and_just_outside_is_excluded(self):
        self.rows = [self.booking("2026-10-14 16:30:00"),
                     self.booking("2026-10-14 20:30:00", "Table 2"),
                     self.booking("2026-10-14 16:29:59", "Table 3"),
                     self.booking("2026-10-14 20:30:01", "Table 3")]
        self.assertEqual(set(self.read()), {"Table 1", "Table 2"})
        self.assertEqual(self.window, (datetime(2026, 10, 14, 16, 30), datetime(2026, 10, 14, 20, 30)))

    def test_unset_duration_defaults_to_120_minutes(self):
        self.framework.db.get_single_value.return_value = None
        self.rows = [self.booking("2026-10-14 20:30:00")]
        self.assertIn("Table 1", self.read())
        self.framework.db.get_single_value.assert_called_once_with(
            "Appointment Booking Settings", "appointment_duration")
        self.assertEqual(self.window, (datetime(2026, 10, 14, 16, 30), datetime(2026, 10, 14, 20, 30)))

    def test_configured_duration_is_minutes_not_hours(self):
        self.framework.db.get_single_value.return_value = 90
        self.rows = [self.booking("2026-10-14 20:00:00"), self.booking("2026-10-14 20:00:01", "Table 2")]
        self.assertEqual(set(self.read()), {"Table 1"})
        self.assertEqual(self.window, (datetime(2026, 10, 14, 17), datetime(2026, 10, 14, 20)))

    def test_closed_bookings_are_ignored(self):
        self.rows = [self.booking("2026-10-14 18:30:00", status="Closed")]
        self.assertEqual(self.read(), {})

    def test_nearest_booking_per_table_wins_even_if_rows_are_unsorted(self):
        self.rows = [self.booking("2026-10-14 20:00:00"),
                     self.booking("2026-10-14 18:20:00", name="Nearest Guest"),
                     self.booking("2026-10-14 19:00:00"),
                     self.booking("2026-10-14 18:45:00", "Table 2")]
        result = self.read()
        self.assertEqual(result["Table 1"]["customer_name"], "Nearest Guest")
        self.assertEqual(result["Table 2"]["scheduled_time"], "2026-10-14 18:45:00")

    def test_booking_on_another_branch_table_is_not_returned(self):
        self.rows = [self.booking("2026-10-14 19:00:00", "Other Branch Table")]
        self.assertEqual(self.read(), {})

    def test_midnight_window_uses_server_site_time_not_utc_or_client_clock(self):
        self.now = datetime(2026, 10, 15, 0, 30)
        self.rows = [self.booking("2026-10-14 23:45:00")]
        self.assertEqual(self.read()["Table 1"]["server_now"], "2026-10-15 00:30:00")
        self.clock.assert_called_once_with()
        self.assertEqual(self.window, (datetime(2026, 10, 14, 22, 30), datetime(2026, 10, 15, 2, 30)))

    def test_bulk_read_does_not_truncate_to_frappe_default_twenty_rows(self):
        self.tables = [f"Table {number}" for number in range(25)]
        self.rows = [self.booking("2026-10-14 19:00:00", table) for table in self.tables]
        self.assertEqual(len(self.read()), 25)
