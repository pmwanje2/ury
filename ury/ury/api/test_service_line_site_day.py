import sqlite3
from datetime import datetime, time, timedelta, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import frappe

from ury.ury.api.ury_service_line import get_running_low, get_service_line


class TestServiceLineInvoiceAge(TestCase):
    def setUp(self):
        self.enterContext(patch("ury.ury.api.ury_service_line.require_dashboard_user"))

    def check_age(self, creation, latest_time, expected_minutes, expected_stage):
        now = datetime(2026, 10, 2, 0, 7)

        def site_datetime(value=None):
            if value is None:
                return now
            if isinstance(value, str) and len(value) == 8:
                return datetime.combine(now.date(), time.fromisoformat(value))
            return frappe.utils.get_datetime(value)

        cache = MagicMock()
        cache.get_value.return_value = None
        invoices = [frappe._dict(name="INV-001", creation=creation)] if creation else []
        with patch("ury.ury.api.ury_service_line.frappe.cache", return_value=cache), patch(
            "ury.ury.api.ury_service_line.get_datetime", side_effect=site_datetime,
        ), patch("ury.ury.api.ury_service_line.frappe.get_all", return_value=[
            frappe._dict(name="Table 1", occupied=1, latest_invoice_time=latest_time, is_take_away=0),
        ]), patch("ury.ury.api.ury_service_line.frappe.db.sql", side_effect=[
            invoices, [frappe._dict(order_status="Served")],
        ]) as sql:
            result = get_service_line("Branch A")
        self.assertEqual(result, [{"table": "Table 1", "stage": expected_stage, "minutes": expected_minutes}])
        query, params = sql.call_args_list[0].args
        self.assertIn("creation", query.split("FROM")[0])
        self.assertIn("docstatus = 0", query)
        self.assertIn("ORDER BY creation DESC LIMIT 1", query)
        self.assertEqual(params, {"table": "Table 1"})
        cache.set_value.assert_called_once_with("ury_dashboard_service_line:Branch A", result, expires_in_sec=15)

    def test_overnight_age_uses_open_invoice_datetime_not_time_only_field(self):
        self.check_age("2026-10-01 23:50:00", timedelta(hours=23, minutes=50), 17, "served")

    def test_future_invoice_age_is_never_negative(self):
        self.check_age("2026-10-02 00:12:00", "00:12:00", 0, "served")

    def test_no_open_invoice_has_no_age_even_when_table_time_exists(self):
        self.check_age(None, "00:00:00", None, "seated")

    def test_open_invoice_has_age_when_table_time_is_missing(self):
        self.check_age("2026-10-01 23:50:00", None, 17, "served")

    def test_over_stage_uses_invoice_age_not_table_time(self):
        self.check_age("2026-10-01 22:22:00", "00:00:00", 105, "over")


class TestRunningLowSiteTime(TestCase):
    def setUp(self):
        self.enterContext(patch("ury.ury.api.ury_service_line.require_dashboard_user"))
        self.utc_now = datetime(2026, 10, 1, 22, 59, tzinfo=timezone.utc)
        self.site_now = self.utc_now.astimezone(ZoneInfo("Africa/Kampala")).replace(tzinfo=None)
        self.cache = MagicMock()
        self.cache.get_value.return_value = None
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        # Evaluate the emitted sales SQL, with MariaDB's UTC CURDATE and
        # date/time TIMESTAMP functions, rather than returning canned rows.
        self.db.create_function("CURDATE", 0, lambda: self.utc_now.date().isoformat())
        self.db.create_function("TIMESTAMP", 2, lambda date, clock: f"{date} {clock}")
        self.db.executescript("""
            CREATE TABLE `tabPOS Invoice` (
                name TEXT, docstatus INTEGER, posting_date TEXT, posting_time TEXT, branch TEXT
            );
            CREATE TABLE `tabPOS Invoice Item` (
                parent TEXT, item_code TEXT, item_name TEXT, qty REAL
            );
            CREATE TABLE `tabItem` (name TEXT, is_stock_item INTEGER);
            INSERT INTO `tabItem` VALUES ('ITEM1', 1);
        """)
        for target, value in (
            ("frappe.utils.data.now_datetime", lambda: self.site_now),
            ("ury.ury.api.ury_service_line.frappe.cache", MagicMock(return_value=self.cache)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_sale(self, name, posted_at, qty, branch="Branch A", docstatus=1):
        date, clock = posted_at.split(" ")
        self.db.execute("INSERT INTO `tabPOS Invoice` VALUES (?, ?, ?, ?, ?)", (
            name, docstatus, date, clock, branch,
        ))
        self.db.execute("INSERT INTO `tabPOS Invoice Item` VALUES (?, ?, ?, ?)", (
            name, "ITEM1", "Item One", qty,
        ))

    def query_sales(self, query, params, as_dict):
        self.assertTrue(as_dict)
        bindings = {}
        for name, value in params.items():
            query = query.replace(f"%({name})s", f":{name}")
            bindings[name] = value.isoformat(sep=" ") if isinstance(value, datetime) else value
        return [frappe._dict(dict(row)) for row in self.db.execute(query, bindings)]

    def check_running_low(self, branch, hours, start, end, qty_sold, eta_minutes):
        def setting(doctype, filters, field):
            return hours if doctype == "URY Report Settings" else "Kitchen - U"

        with patch("frappe.db.get_value", side_effect=setting), patch(
            "ury.ury.api.ury_service_line._bin_actual_qty", return_value=12,
        ), patch("frappe.db.sql", side_effect=self.query_sales) as sql:
            result = get_running_low(branch)
        self.assertEqual(result, [{
            "item_code": "ITEM1", "item_name": "Item One", "remaining": 12,
            "qty_sold_today": qty_sold, "eta_minutes": eta_minutes, "data_quality_issue": False,
        }])
        params = sql.call_args.args[1]
        self.assertEqual(params, {"start": start, "end": end, **({"branch": branch} if branch else {})})
        self.cache.get_value.assert_called_once_with(f"ury_dashboard_running_low:{branch}")
        self.cache.set_value.assert_called_once_with(
            f"ury_dashboard_running_low:{branch}", result, expires_in_sec=60,
        )

    def test_site_0159_finds_sales_when_utc_date_is_previous_day(self):
        self.assertEqual(self.site_now, datetime(2026, 10, 2, 1, 59))
        self.assertEqual(self.utc_now.date().isoformat(), "2026-10-01")
        self.add_sale("INV-SITE", "2026-10-02 00:30:00", 12)
        self.add_sale("INV-OTHER", "2026-10-02 00:30:00", 100, branch="Other")
        self.add_sale("INV-DRAFT", "2026-10-02 00:30:00", 100, docstatus=0)
        self.check_running_low("Branch A", None, datetime(2026, 10, 2), datetime(2026, 10, 3), 12, 119)

    def test_before_report_cutoff_uses_previous_business_day_and_elapsed_hours(self):
        self.add_sale("INV-START", "2026-10-01 06:00:00", 6)
        self.add_sale("INV-SITE", "2026-10-02 00:30:00", 6)
        self.add_sale("INV-BEFORE", "2026-10-01 05:59:59", 100)
        self.add_sale("INV-END", "2026-10-02 06:00:00", 100)
        self.check_running_low("Branch A", 6, datetime(2026, 10, 1, 6), datetime(2026, 10, 2, 6), 12, 1199)

    def test_after_report_cutoff_uses_current_business_day_and_elapsed_hours(self):
        self.utc_now = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
        self.site_now = datetime(2026, 10, 2, 12)
        self.add_sale("INV-START", "2026-10-02 06:00:00", 12)
        self.add_sale("INV-BEFORE", "2026-10-02 05:59:59", 100)
        self.add_sale("INV-END", "2026-10-03 06:00:00", 100)
        self.check_running_low("Branch A", 6, datetime(2026, 10, 2, 6), datetime(2026, 10, 3, 6), 12, 360)

    def test_elapsed_hours_keeps_half_hour_floor_after_report_cutoff(self):
        self.utc_now = datetime(2026, 10, 2, 3, 10, tzinfo=timezone.utc)
        self.site_now = datetime(2026, 10, 2, 6, 10)
        self.add_sale("INV-START", "2026-10-02 06:00:00", 12)
        self.check_running_low("Branch A", 6, datetime(2026, 10, 2, 6), datetime(2026, 10, 3, 6), 12, 30)

    def test_without_branch_uses_site_day_and_all_warehouses(self):
        self.add_sale("INV-SITE", "2026-10-02 00:30:00", 6)
        self.add_sale("INV-OTHER", "2026-10-02 00:30:00", 6, branch="Other")
        self.check_running_low(None, None, datetime(2026, 10, 2), datetime(2026, 10, 3), 12, 119)
