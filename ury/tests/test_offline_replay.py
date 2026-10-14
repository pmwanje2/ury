"""Native offline replay contracts, without replacing invoice/KOT persistence."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, get_system_timezone, get_time, now_datetime

from ury.tests.offline_pos_utils import make_offline_pos, send_order


class TestOfflineReplay(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.pos = make_offline_pos()
        self.client_id = str(uuid4())

    def tearDown(self):
        frappe.set_user("Administrator")
        super().tearDown()

    def send(self, **kwargs):
        payload = dict(client_order_id=self.client_id, round_id="1")
        payload.update(kwargs)
        return send_order(self.pos, **payload)

    def assert_no_order(self):
        filters = {"pos_profile": self.pos.profile.name}
        self.assertEqual(frappe.db.count("POS Invoice", filters), 0)
        self.assertEqual(frappe.db.count("URY KOT", filters), 0)

    def test_append_preserves_existing_lines_and_creates_only_round_kot(self):
        first = self.send(items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 2}])
        previous = frappe.get_doc("POS Invoice", first.name)
        added = self.send(
            append="1", round_id="2", invoice=first.name, last_invoice=first.name,
            last_modified_time=str(first.modified),
        )
        saved = frappe.get_doc("POS Invoice", first.name)
        self.assertEqual([row.qty for row in saved.items], [2, 1])
        self.assertEqual(saved.items[0].name, previous.items[0].name)
        self.assertEqual(added.grand_total, 300)
        kots = frappe.get_all("URY KOT", filters={"invoice": first.name}, order_by="creation", pluck="name")
        self.assertEqual(len(kots), 2)
        self.assertEqual([float(row.quantity) for row in frappe.get_doc("URY KOT", kots[1]).kot_items], [1])
        retry = self.send(append=1, round_id="2", invoice=first.name)
        self.assertEqual(retry.name, first.name)
        self.assertEqual([row.qty for row in frappe.get_doc("POS Invoice", first.name).items], [2, 1])
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": first.name}), 2)

    def test_append_ignores_stale_timestamp_but_replace_still_refuses_it(self):
        first = self.send()
        stale = "2000-01-01 00:00:00"
        added = self.send(append=1, round_id="2", invoice=first.name,
                          last_invoice=first.name, last_modified_time=stale)
        self.assertEqual(added.get("name"), first.name, "Append must not enter the stale replace gate")
        refused = self.send(append="0", round_id="3", invoice=first.name,
                            last_invoice=first.name, last_modified_time=stale)
        self.assertEqual(refused.get("status"), "Failure")
        self.assertEqual(sum(row.qty for row in frappe.get_doc("POS Invoice", first.name).items), 2)

    def test_append_does_not_require_remove_items_permission(self):
        first = self.send(items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 2}])
        frappe.db.set_value("POS Profile", self.pos.profile.name, "remove_items", 0)
        try:
            added = self.send(append=1, round_id="2", invoice=first.name,
                              last_invoice=first.name, last_modified_time=str(first.modified))
        except frappe.PermissionError as error:
            self.fail(f"Adding a round is not a removal: {error}")
        self.assertEqual(added.grand_total, 300)

    def test_append_refuses_negative_and_zero_quantities_without_mutation(self):
        first = self.send()
        for qty in (-1, 0):
            with self.subTest(qty=qty), self.assertRaises(frappe.ValidationError):
                self.send(append=1, round_id="2", invoice=first.name,
                          last_invoice=first.name, last_modified_time=str(first.modified),
                          items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": qty}])
        self.assertEqual(frappe.db.get_value("POS Invoice", first.name, "grand_total"), 100)
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": first.name}), 1)

    def test_capture_inside_shift_sets_posting_datetime_and_capture_field(self):
        captured = get_datetime(self.pos.opening.period_start_date)
        result = self.send(captured_at=str(captured))
        saved = frappe.get_doc("POS Invoice", result.name)
        self.assertEqual(get_datetime(saved.ury_offline_captured_at), captured)
        self.assertEqual(str(saved.posting_date), str(captured.date()))
        self.assertEqual(get_time(saved.posting_time), captured.time())

    def test_capture_before_shift_is_refused_with_stable_id(self):
        captured = get_datetime(self.pos.opening.period_start_date) - timedelta(seconds=1)
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_OUTSIDE_SHIFT"):
            self.send(captured_at=str(captured))
        self.assert_no_order()

    def test_utc_capture_is_normalized_to_sites_posting_timezone(self):
        captured = get_datetime(self.pos.opening.period_start_date)
        utc_capture = captured.replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(ZoneInfo("UTC"))
        result = self.send(captured_at=utc_capture.isoformat())
        saved = frappe.get_doc("POS Invoice", result.name)
        self.assertEqual(get_datetime(saved.ury_offline_captured_at), captured)
        self.assertEqual(get_time(saved.posting_time), captured.time())

    def test_future_capture_is_refused_with_stable_id(self):
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_OUTSIDE_SHIFT"):
            self.send(captured_at=str(now_datetime() + timedelta(hours=1)))
        self.assert_no_order()

    def test_closed_or_draft_opening_cannot_authorize_capture(self):
        for changes in ({"status": "Closed"}, {"status": "Open", "docstatus": 0}):
            frappe.db.set_value("POS Opening Entry", self.pos.opening.name, changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(
                frappe.ValidationError, "URY_OFFLINE_OUTSIDE_SHIFT"
            ):
                self.send(captured_at=str(now_datetime()))
        self.assert_no_order()

    def test_another_users_opening_cannot_authorize_capture(self):
        frappe.db.set_value("POS Opening Entry", self.pos.opening.name, "user", "Administrator")
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_OUTSIDE_SHIFT"):
            self.send(captured_at=str(now_datetime()))
        self.assert_no_order()

    def test_malformed_capture_is_refused_with_stable_id(self):
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_OUTSIDE_SHIFT"):
            self.send(captured_at="not-a-date")
        self.assert_no_order()

    def test_expected_total_mismatch_is_refused_before_invoice_or_kot(self):
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_TOTAL_MISMATCH"):
            self.send(expected_total=1)
        self.assert_no_order()

    def test_append_total_mismatch_preserves_saved_order_and_round(self):
        first = self.send()
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_TOTAL_MISMATCH"):
            self.send(append=1, expected_total=1, invoice=first.name, round_id="2",
                      last_invoice=first.name, last_modified_time=str(first.modified))
        saved = frappe.get_doc("POS Invoice", first.name)
        self.assertEqual(saved.grand_total, 100)
        self.assertEqual(saved.ury_last_round_id, "1")
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": first.name}), 1)

    def test_matching_expected_total_uses_server_price_not_caller_rate(self):
        result = self.send(expected_total="100")
        self.assertEqual(result.grand_total, 100)
        self.assertEqual(result["items"][0]["rate"], 100)

    def test_expected_total_compares_native_tax_inclusive_invoice_total(self):
        first = self.send()
        frappe.set_user("Administrator")
        account = frappe.get_doc({
            "doctype": "Account", "account_name": f"Offline VAT {self.client_id}",
            "company": "URY", "parent_account": "Duties and Taxes - URY", "account_type": "Tax",
        }).insert()
        invoice = frappe.get_doc("POS Invoice", first.name)
        invoice.append("taxes", {"charge_type": "On Net Total", "account_head": account.name,
                                 "rate": 18, "description": "Demo VAT"})
        invoice.save()
        frappe.set_user(self.pos.user.name)
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_TOTAL_MISMATCH"):
            self.send(append=1, invoice=first.name, round_id="2", expected_total=200,
                      last_invoice=first.name, last_modified_time=str(invoice.modified))
        added = self.send(append=1, invoice=first.name, round_id="2", expected_total=236,
                          last_invoice=first.name, last_modified_time=str(invoice.modified))
        self.assertEqual(added.grand_total, 236)

    def test_offline_printed_kot_is_served_and_emits_no_printer_or_mosaic_output(self):
        # Only external output seams are intercepted; invoice/KOT writes are real.
        frappe.set_user("Administrator")
        printer = frappe.get_doc({
            "doctype": "Network Printer Settings", "name": f"Demo {self.client_id}",
            "printer_name": "Demo Printer",
            "server_ip": "127.0.0.1", "port": 9100,
        }).insert()
        production = frappe.get_doc("URY Production Unit", self.pos.production.name)
        production.append("printer_settings", {"printer": printer.name, "custom_kot_print": 1})
        production.save()
        frappe.set_user(self.pos.user.name)
        with patch("ury.ury.doctype.ury_kot.ury_kot.print_by_server") as print_output, \
             patch("frappe.publish_realtime") as realtime_output:
            first = self.send(offline_printed="1")
            second = self.send(offline_printed="1")
        kots = frappe.get_all("URY KOT", filters={"invoice": first.name}, pluck="name")
        self.assertEqual(len(kots), 1)
        kot = frappe.get_doc("URY KOT", kots[0])
        self.assertEqual(kot.get("ury_offline_printed"), 1)
        self.assertEqual((kot.docstatus, kot.order_status), (1, "Served"))
        self.assertEqual(first.name, second.name)
        print_output.assert_not_called()
        self.assertFalse(any(str(call.args[0]).startswith("kot_update_")
                             for call in realtime_output.call_args_list))

    def test_online_round_after_offline_round_still_publishes_to_mosaic(self):
        first = self.send(offline_printed=1)
        with patch("frappe.publish_realtime") as realtime_output:
            self.send(append=1, round_id="2", invoice=first.name, offline_printed="0",
                      last_invoice=first.name, last_modified_time=str(first.modified))
        kots = frappe.get_all("URY KOT", filters={"invoice": first.name}, order_by="creation", pluck="name")
        self.assertEqual(len(kots), 2)
        kot = frappe.get_doc("URY KOT", kots[1])
        self.assertEqual(kot.get("ury_offline_printed"), 0)
        self.assertEqual(kot.order_status, "Ready For Prepare")
        self.assertTrue(any(str(call.args[0]).startswith("kot_update_")
                            for call in realtime_output.call_args_list))
