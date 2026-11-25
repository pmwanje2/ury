"""Native offline replay contracts, without replacing invoice/KOT persistence."""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, get_system_timezone, get_time, now_datetime

from ury.tests.offline_pos_utils import make_made_to_order_item, make_offline_pos, send_order


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

    def test_offline_fields_are_refused_on_a_till_without_offline_capture(self):
        frappe.db.set_value("POS Profile", self.pos.profile.name, "ury_offline_capture", 0)
        for kwargs in ({"offline_printed": 1}, {"captured_at": str(now_datetime())}, {"append": 1}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(
                frappe.ValidationError, "URY_OFFLINE_CAPTURE_DISABLED"
            ):
                self.send(**kwargs)
        self.assert_no_order()

    def test_append_on_a_till_without_offline_capture_leaves_the_order_unchanged(self):
        first = self.send()
        frappe.db.set_value("POS Profile", self.pos.profile.name, "ury_offline_capture", 0)
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_CAPTURE_DISABLED"):
            self.send(append=1, round_id="2", invoice=first.name,
                      last_invoice=first.name, last_modified_time=str(first.modified))
        self.assertEqual([row.qty for row in frappe.get_doc("POS Invoice", first.name).items], [1])
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": first.name}), 1)
        self.assertEqual(frappe.db.get_value("POS Invoice", first.name, "ury_last_round_id"), "1")

    def test_direct_kot_execute_cannot_mark_offline_printed_without_offline_capture(self):
        from ury.ury.api.ury_kot_generate import kot_execute

        first = self.send()
        frappe.db.set_value("POS Profile", self.pos.profile.name, "ury_offline_capture", 0)
        item = {"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 2}
        with self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_CAPTURE_DISABLED"):
            frappe.call(kot_execute, invoice_id=first.name, customer=self.pos.customer.name,
                        current_items=[item], previous_items=[dict(item, qty=1)], offline_printed=1)
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": first.name}), 1)

    def test_till_without_offline_capture_keeps_idempotent_online_orders(self):
        frappe.db.set_value("POS Profile", self.pos.profile.name, "ury_offline_capture", 0)
        first = self.send(offline_printed=0, append=0)
        retry = self.send(offline_printed="0", append="0")
        self.assertEqual(retry.name, first.name)
        kots = frappe.get_all("URY KOT", filters={"invoice": first.name}, fields=["ury_offline_printed"])
        self.assertEqual([row.ury_offline_printed for row in kots], [0])

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

    def test_append_without_snapshot_fields_keeps_occupied_gate_for_non_billing_user(self):
        first = self.send()
        frappe.set_user("Administrator")
        profile = frappe.get_doc("POS Profile", self.pos.profile.name)
        profile.set("role_allowed_for_billing", [])
        profile.save()
        frappe.set_user(self.pos.user.name)
        refused = self.send(append=1, round_id="2", invoice=first.name)
        self.assertEqual(refused.get("status"), "Failure")
        self.assertEqual(frappe.db.get_value("POS Invoice", first.name, "ury_last_round_id"), "1")

    def test_append_refuses_negative_and_zero_quantities_without_mutation(self):
        first = self.send()
        for qty in (-1, 0):
            with self.subTest(qty=qty), self.assertRaisesRegex(frappe.ValidationError, "URY_OFFLINE_APPEND_QTY"):
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

    def test_expected_total_validation_does_not_reset_saved_invoice_defaults(self):
        first = self.send()
        frappe.set_user("Administrator")
        original_cost_center = frappe.db.get_value(
            "Cost Center", {"company": "URY", "is_group": 0}
        )
        saved = frappe.get_doc("POS Invoice", first.name)
        saved.cost_center = original_cost_center
        saved.items[0].cost_center = original_cost_center
        saved.save()
        cost_center = frappe.get_doc({
            "doctype": "Cost Center", "cost_center_name": f"Offline Other {self.client_id}",
            "company": "URY", "parent_cost_center": frappe.db.get_value(
                "Cost Center", {"company": "URY", "is_group": 1}
            ),
        }).insert()
        profile = frappe.get_doc("POS Profile", self.pos.profile.name)
        profile.cost_center = cost_center.name
        profile.save()
        frappe.set_user(self.pos.user.name)
        added = self.send(append=1, invoice=first.name, round_id="2", expected_total=200,
                          last_invoice=first.name, last_modified_time=str(first.modified))
        self.assertEqual(added.cost_center, original_cost_center,
                         "Total comparison must not reapply profile defaults")
        self.assertEqual(frappe.get_doc("POS Invoice", added.name).items[0].cost_center, original_cost_center)

    def test_offline_printed_kot_is_served_and_emits_no_printer_or_mosaic_output(self):
        # Only external output seams are intercepted; invoice/KOT writes are real.
        frappe.set_user("Administrator")
        printer = frappe.get_doc({
            "doctype": "Network Printer Settings", "__newname": f"Demo {self.client_id}",
            "printer_name": "Demo Printer",
            "server_ip": "127.0.0.1", "port": 9100,
        }).insert()
        production = frappe.get_doc("URY Production Unit", self.pos.production.name)
        production.append("printer_settings", {"printer": printer.name, "custom_kot_print": 1})
        production.save()
        frappe.set_user(self.pos.user.name)
        # v16 KOT and waiter-slip output both go through Network Printer Settings.print_doc.
        with patch(
            "ury.ury.printing.custom_network_printer_settings.CustomNetworkPrinterSettings.print_doc"
        ) as print_output, patch(
            "ury.ury.api.ury_waiter_print.print_combined_waiter_order_slip"
        ) as slip_output, patch("frappe.publish_realtime") as realtime_output:
            first = self.send(offline_printed="1")
            second = self.send(offline_printed="1")
        kots = frappe.get_all("URY KOT", filters={"invoice": first.name}, pluck="name")
        self.assertEqual(len(kots), 1)
        kot = frappe.get_doc("URY KOT", kots[0])
        self.assertEqual(kot.get("ury_offline_printed"), 1)
        self.assertEqual((kot.docstatus, kot.order_status), (1, "Served"))
        self.assertEqual(first.name, second.name)
        print_output.assert_not_called()
        slip_output.assert_not_called()
        self.assertFalse(any(str(call.args[0]).startswith("kot_update_")
                             for call in realtime_output.call_args_list))

    def test_offline_printed_made_to_order_round_goes_through_ready_before_served(self):
        # A station-printed ticket is still a fired ticket: READY must attach the
        # production posting intent, exactly as the kitchen serve path does.
        dish = make_made_to_order_item(self.pos)
        with patch(
            "ury.ury.printing.custom_network_printer_settings.CustomNetworkPrinterSettings.print_doc"
        ) as print_output, patch("frappe.publish_realtime") as realtime_output:
            order = self.send(offline_printed=1,
                              items=[{"item": dish.name, "item_name": dish.item_name, "qty": 1}])
        kot = frappe.get_doc("URY KOT", frappe.get_all("URY KOT", filters={"invoice": order.name}, pluck="name")[0])
        self.assertEqual((kot.docstatus, kot.order_status, kot.ury_offline_printed), (1, "Served", 1))
        states = frappe.get_all("URY KOT Item Execution", filters={"kot": kot.name}, pluck="state")
        self.assertTrue(states)
        self.assertTrue(set(states) <= {"READY", "SERVED"}, f"Item execution stayed {states}")
        intents = frappe.get_all("URY Fulfilment Posting Intent", filters={"kot": kot.name},
                                 fields=["production_policy"])
        self.assertEqual([row.production_policy for row in intents], ["MADE_TO_ORDER"])
        self.assertTrue(kot.start_time_serv, "Served must carry the serve time like any other serve")
        self.assertNotIn(kot.production_time, (None, ""))
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

    def test_cross_waiter_employee_user_permission_merge_rounds_creates_kot(self):
        """Hydrogen R3 P1 regression: two Employee-linked Captains (each restricted by
        ERPNext Employee User Permission) colliding under Merge Rounds must merge the
        round AND create the KOT without failing has_permission on Captain #1's invoice."""
        from ury.ury.api.ury_kot_generate import kot_execute

        frappe.set_user("Administrator")
        suffix = frappe.generate_hash(length=6)
        frappe.db.set_value(
            "POS Profile",
            self.pos.profile.name,
            "custom_offline_collision_policy",
            "Merge Rounds",
        )
        # Create second floor Captain in same Branch & POS Profile (non-billing waiter role)
        captain2 = frappe.get_doc({
            "doctype": "User",
            "email": f"offline-cap2-{suffix}@example.com",
            "first_name": "Offline Captain Two",
            "send_welcome_email": 0,
        }).insert()
        captain2.add_roles("URY Captain")
        self.pos.user.add_roles("URY Captain")

        branch_doc = frappe.get_doc("Branch", self.pos.branch.name)
        branch_doc.append("user", {"user": captain2.name})
        branch_doc.save(ignore_permissions=True)

        profile_doc = frappe.get_doc("POS Profile", self.pos.profile.name)
        profile_doc.append("applicable_for_users", {"user": captain2.name})
        profile_doc.save(ignore_permissions=True)

        # Link distinct ERPNext Employee records (auto-creates Employee User Permissions)
        for gender in ("Female", "Male"):
            if not frappe.db.exists("Gender", gender):
                frappe.get_doc({"doctype": "Gender", "gender": gender}).insert(ignore_permissions=True)
        emp1 = frappe.get_doc({
            "doctype": "Employee",
            "first_name": "CapOne",
            "gender": "Female",
            "date_of_birth": "1990-01-01",
            "date_of_joining": "2020-01-01",
            "company": self.pos.profile.company,
            "user_id": self.pos.user.name,
            "create_user_permission": 1,
        }).insert(ignore_permissions=True)
        emp2 = frappe.get_doc({
            "doctype": "Employee",
            "first_name": "CapTwo",
            "gender": "Male",
            "date_of_birth": "1991-01-01",
            "date_of_joining": "2020-01-01",
            "company": self.pos.profile.company,
            "user_id": captain2.name,
            "create_user_permission": 1,
        }).insert(ignore_permissions=True)

        # Create a Dine In table on the branch/restaurant
        table = frappe.get_doc({
            "doctype": "URY Table",
            "__newname": f"Offline-T-{suffix}",
            "branch": self.pos.branch.name,
            "restaurant": self.pos.restaurant.name,
            "restaurant_room": self.pos.room.name,
            "is_take_away": 0,
        }).insert(ignore_permissions=True)

        # Captain #1 opens table check (stamps custom_waiter_employee = emp1.name)
        frappe.set_user(self.pos.user.name)
        first = send_order(
            self.pos,
            client_order_id=str(uuid4()),
            round_id="r1-cap1",
            table=table.name,
            order_type="Dine In",
            no_of_pax=2,
        )
        if hasattr(first, "custom_waiter_employee"):
            frappe.db.set_value("POS Invoice", first.name, "custom_waiter_employee", emp1.name)

        # Captain #2 (with Employee User Permission for emp2 only) sends an offline order on same table
        frappe.set_user(captain2.name)
        self.assertFalse(
            frappe.has_permission("POS Invoice", "write", doc=frappe.get_doc("POS Invoice", first.name)),
            "Setup check: Captain #2 must lack direct write permission on Captain #1's Employee-scoped invoice",
        )
        # Direct whitelisted kot_execute by Captain #2 must still be rejected with PermissionError
        item_row = {"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 2}
        with self.assertRaises(frappe.PermissionError):
            frappe.call(
                kot_execute,
                invoice_id=first.name,
                customer=self.pos.customer.name,
                restaurant_table=table.name,
                current_items=[item_row],
                previous_items=[dict(item_row, qty=1)],
            )
        round2_id = f"r2-{suffix}"
        merged = send_order(
            self.pos,
            items=[{"item": self.pos.item.name, "item_name": "Offline Tea", "qty": 1}],
            cashier=self.pos.user.name,
            owner=captain2.name,
            waiter=captain2.name,
            last_invoice=None,
            table=table.name,
            order_type="Dine In",
            no_of_pax=2,
            client_order_id=str(uuid4()),
            round_id=round2_id,
            captured_at=str(now_datetime() - timedelta(seconds=1)),
            offline_printed=0,
        )
        frappe.set_user("Administrator")
        self.assertEqual(merged.name, first.name)
        reloaded = frappe.get_doc("POS Invoice", first.name)
        self.assertEqual(sum(row.qty for row in reloaded.items), 2)
        self.assertIn(round2_id, str(reloaded.get("ury_last_round_id") or ""))
        self.assertEqual(frappe.db.count("URY KOT", {"invoice": first.name, "docstatus": 1}), 2)
        self.assertNotEqual(emp1.name, emp2.name)
