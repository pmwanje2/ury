from datetime import date, datetime
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from ury.ury.api.ury_dashboard import (
    get_baseline,
    get_dashboard_stats,
    get_needs_attention,
    get_shift_metrics,
)


class TestDashboardSiteTime(TestCase):
    def setUp(self):
        self.site_now = datetime(2026, 10, 2, 0, 7)
        self.cache = MagicMock()
        self.cache.get_value.return_value = None
        for target, value in (
            ("ury.ury.api.ury_dashboard.frappe.cache", MagicMock(return_value=self.cache)),
            ("frappe.utils.data.now_datetime", lambda: self.site_now),
            ("ury.ury.api.ury_dashboard.frappe.db.get_value", MagicMock(return_value=None)),
            ("ury.ury.api.ury_dashboard.frappe.db.count", MagicMock(return_value=1)),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def check_stats_window(self, branch, hours, start, end):
        with patch("ury.ury.api.ury_dashboard.frappe.db.get_value", return_value=hours), patch(
            "ury.ury.api.ury_dashboard.frappe.db.sql",
            return_value=[frappe._dict(currency="USD", total_invoices=1, grand_total=84000)],
        ) as sql:
            result = get_dashboard_stats(branch)
        query, params = sql.call_args.args
        self.assertEqual(params.get("start"), start)
        self.assertEqual(params.get("end"), end)
        self.assertNotIn("curdate", query.lower())
        self.assertIn("< %(end)s", query)
        self.assertEqual(params.get("branch"), branch)
        self.assertEqual(result["todays_sales"], 84000)
        self.assertEqual(result["orders_today"], 1)
        self.cache.set_value.assert_called_once_with(
            f"ury_dashboard_stats:{branch}:None", result, expires_in_sec=30,
        )

    def test_stats_use_site_day_without_report_settings(self):
        self.check_stats_window("Branch A", None, datetime(2026, 10, 2), datetime(2026, 10, 3))

    def aggregate_stats(self, company=None):
        # The CTEs shadow the two real tables for this single MariaDB query.
        fixture = """
            WITH `tabURY Report Settings` (branch, hours) AS (
                SELECT 'A', 6 UNION ALL SELECT 'B', 0
            ), `tabPOS Invoice` AS (
                SELECT sales.*, 'USD' AS currency, 1 AS docstatus, 'Paid' AS status FROM (
                    SELECT 'prior' AS name, 'A' AS branch, 'Co' AS company,
                        DATE('2026-10-02') AS posting_date, TIME('01:00') AS posting_time, 100 AS grand_total
                    UNION ALL SELECT 'opening', 'A', 'Co', '2026-10-02', '06:00', 20
                    UNION ALL SELECT 'next', 'A', 'Co', '2026-10-03', '06:00', 200
                    UNION ALL SELECT 'midnight', 'B', 'Co', '2026-10-02', '01:00', 50
                    UNION ALL SELECT 'unset', 'C', 'Other', '2026-10-02', '00:00', 5
                ) sales
            )
        """
        sql = frappe.db.sql
        with patch.object(frappe.db, "sql", side_effect=lambda q, *a, **kw: sql(fixture + q, *a, **kw)), patch(
            "frappe.get_all", return_value=[],
        ):
            stats = get_dashboard_stats(company=company)
        return stats["orders_today"], stats["todays_sales"]

    def test_stats_without_branch_use_site_day(self):
        # Before A opens: its business day still includes the 01:00 sale.
        self.assertEqual(self.aggregate_stats(), (3, 155))

    def test_company_stats_use_each_outlets_cutoff(self):
        self.site_now = datetime(2026, 10, 2, 8)
        self.assertEqual(self.aggregate_stats(company="Co"), (2, 70))

    def test_all_branch_stats_use_each_outlets_cutoff(self):
        self.site_now = datetime(2026, 10, 2, 8)
        self.assertEqual(self.aggregate_stats(), (3, 75))

    def test_stats_before_report_cutoff_use_previous_business_day(self):
        self.check_stats_window("Branch A", 6, datetime(2026, 10, 1, 6), datetime(2026, 10, 2, 6))

    def test_stats_after_report_cutoff_use_current_business_day(self):
        self.site_now = datetime(2026, 10, 2, 12)
        self.check_stats_window("Branch A", 6, datetime(2026, 10, 2, 6), datetime(2026, 10, 3, 6))

    def test_baseline_uses_site_date_not_database_utc_date(self):
        with patch("ury.ury.api.ury_dashboard.frappe.db.sql", return_value=[]) as sql:
            result = get_baseline("Branch A", weeks=6)
        query, params = sql.call_args.args
        self.assertEqual(params.get("start_date"), date(2026, 8, 21))
        self.assertEqual(params.get("end_date"), date(2026, 10, 2))
        self.assertNotIn("curdate", query.lower())
        self.assertEqual(params["weekday"], 4)
        self.assertEqual(params["hour_low"], 0)
        self.assertEqual(params["hour_high"], 1)
        self.cache.set_value.assert_called_once_with(
            "ury_dashboard_baseline:Branch A:4:0", result, expires_in_sec=300,
        )

    def test_zero_duration_served_ticket_counts(self):
        rows = [frappe._dict(sales=100, covers=1), frappe._dict(avg_ticket_minutes=0)]
        with patch("ury.ury.api.ury_dashboard.frappe.db.sql", side_effect=[[r] for r in rows]) as sql:
            self.assertEqual(get_shift_metrics("Branch A")["avg_ticket_minutes"], 0)
        invoice, ticket = (call.args[0] for call in sql.call_args_list)
        self.assertIn("< %(end)s", invoice)
        self.assertIn("< %(end)s", ticket)
        self.assertIn("'Served'", ticket)

    def test_occupied_attention_uses_latest_open_invoice_creation(self):
        with patch("ury.ury.api.ury_dashboard.frappe.get_all", return_value=[]), patch(
            "ury.ury.api.ury_dashboard.frappe.db.sql", side_effect=[[], [{"name": "Table 1"}]],
        ) as sql:
            result = get_needs_attention("Branch A")
        self.assertEqual([item["type"] for item in result], ["table_occupied_long"])
        query, params = sql.call_args_list[1].args
        self.assertIn("`tabPOS Invoice`", query)
        self.assertIn("docstatus` = 0", query)
        self.assertIn("MAX(i.`creation`)", query)
        self.assertNotIn("latest_invoice_time", query)
        self.assertEqual(params, {"threshold": datetime(2026, 10, 1, 23, 7), "branch": "Branch A"})
