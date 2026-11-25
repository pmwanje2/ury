"""Regressions for split settlement, kitchen placement and floor concurrency."""

import unittest
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from ury import hooks
from ury.ury.api import table_lifecycle as floor, table_reservation as booking
from ury.ury.api import ury_kot_generate as kitchen, ury_waiter_print as slip
from ury.ury.doctype.ury_order import ury_order as order
from ury.ury.doctype.ury_table_reservation.ury_table_reservation import URYTableReservation
from ury.ury_pos import floor_actions, outlet_context


class Row(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


def throw(message, exc=frappe.ValidationError, **kw):
    raise exc(message)


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(frappe, "throw", throw))
        self.db = self.enterContext(patch.object(frappe, "db", Mock()))
        self.enterContext(patch.object(frappe, "session", Row(user="cashier@example.com")))
        for module in (order, booking, kitchen, floor, outlet_context):
            self.enterContext(patch.object(module, "_", lambda text: text, create=True))

    def test_split_money_tax_and_converted_stock_are_conserved(self):
        from ury.ury_pos.split_balances import allocate_split_balances

        for decimals, amount, tax in ((0, 100, 11), (2, 1, 0.11), (3, 1, 0.111)):
            with self.subTest(decimals=decimals):
                row = Row(name="A", qty=1, stock_qty=0.5, amount=amount,
                          net_amount=amount, base_net_amount=1)
                taxes = [Row(account_head="Tax", tax_amount_after_discount_amount=tax)]
                check = Row(items=[row], taxes=taxes, net_total=amount,
                            grand_total=amount + tax, rounded_total=amount + tax)
                shares = allocate_split_balances(check, {"A": 0.3333}, decimals, 2)
                retained, moved = allocate_split_balances(
                    Row(items=[Row(name="A", qty=0.6667, **shares[0]["items"]["A"])],
                        taxes=[Row(**shares[0]["taxes"][0])], **shares[0]["header"]),
                    {"A": 0.3333}, decimals, 2,
                )
                checks = (retained, shares[1], moved)
                self.assertEqual([x["items"]["A"]["amount"] for x in checks],
                                 [round(amount - 2 * round(amount / 3, decimals), decimals),
                                  round(amount / 3, decimals), round(amount / 3, decimals)])
                self.assertEqual(sum(round(x["items"]["A"]["stock_qty"] * 10000) for x in checks), 5000)
                self.assertEqual(sum(round(x["taxes"][0]["tax_amount_after_discount_amount"] * 10**decimals)
                                     for x in checks), round(tax * 10**decimals))
                self.assertEqual(sum(round(x["header"]["grand_total"] * 10**decimals) for x in checks),
                                 round((amount + tax) * 10**decimals))

    def test_shared_line_keeps_both_overlapping_tickets_on_anchor(self):
        source = Row(name="ROOT", items=[
            Row(name=key, qty=1, reservation_line_key=key, item_code=key) for key in ("A", "B")
        ])
        a = Row(reservation_line_key="A", item="A", quantity=1)
        b = Row(reservation_line_key="B", item="B", quantity=1)
        with patch.object(frappe, "get_all", side_effect=[["ONE", "TWO"], [a], [a, b]]):
            anchored = set()
            self.assertEqual(order._split_kots_to_move(source, {"A": 1}, True, anchored=anchored), [])
            self.assertEqual(anchored, {"A", "B"})

    def test_unidentified_reserved_release_refuses_split(self):
        source = Row(name="ROOT", items=[Row(name="A", qty=2, reservation_line_key="A", item_code="Soup")])
        with patch.object(frappe, "get_all", side_effect=[["KOT"], [
            Row(reservation_line_key=None, item="Soup", quantity=1)
        ]]), self.assertRaisesRegex(frappe.ValidationError, "SPLIT-RESERVED-KOT"):
            order._split_kots_to_move(source, {"A": 1}, True)

    def test_native_discard_invokes_shared_production_guard(self):
        path = hooks.doc_events["POS Invoice"].get("before_discard")
        self.assertIsInstance(path, str)
        doc = Row(name="ROOT", custom_split_group="GROUP", items=[Row(custom_kitchen_anchor="ROOT")])
        with self.assertRaisesRegex(frappe.ValidationError, "SPLIT-KITCHEN-SHARED"):
            frappe.get_attr(path)(doc, "before_discard")

    def test_profile_membership_precedes_split_planning(self):
        check = Row(name="ROOT", pos_profile="Profile B", branch="Main", docstatus=0)
        with (
            patch.object(outlet_context, "get_authorized_outlets", return_value=[
                {"pos_profile": "Profile A", "branch": "Main"}
            ]),
            patch.object(frappe, "get_doc", return_value=check),
            patch.object(frappe, "has_permission", return_value=True),
            patch.object(order, "getBranch", return_value="Main"),
            patch.object(order, "_pa_assert_not_offline_replay"),
            patch.object(order, "_pa_assert_not_efris_fiscalised"),
            patch.object(order, "_plan_split_checks") as plan,
            self.assertRaisesRegex(frappe.PermissionError, "OUTLET-FORBIDDEN"),
        ):
            try:
                order.split_check_multi("ROOT", splits=[], mode="manual")
            finally:
                plan.assert_not_called()

    def test_slip_preserves_identical_item_placements(self):
        kot = Row(type="New Order", kot_items=[
            Row(item="Soup", item_name="Soup", quantity=1, comments="", seat_no=seat, course=course)
            for seat, course in ((1, "Starter"), (2, "Main"))
        ])
        with patch.object(slip, "_normalize_qty", side_effect=int):
            self.assertEqual([(row.get("seat_no"), row["course"], row["quantity"])
                              for row in slip._aggregate_kot_items([kot])],
                             [(1, "Starter", 1), (2, "Main", 1)])

    def test_issued_line_rejects_placement_change_at_kitchen_boundary(self):
        previous = dict(item="Soup", qty=1, reservation_line_key="a", custom_course="Starter", custom_seat_no=1)
        invoice = Row(name="ROOT", pos_profile="Till")
        profile = Row(name="Till", custom_kot_naming_series="KOT")
        for change in ({"custom_course": "Main"}, {"custom_seat_no": 2}):
            with (
                self.subTest(change=change),
                patch.object(frappe, "get_doc", side_effect=[invoice, profile]),
                patch.object(frappe, "get_precision", return_value=4),
                patch.object(frappe, "_", lambda text: text),
                self.assertRaisesRegex(frappe.ValidationError, "KOT-PLACEMENT-LOCKED"),
            ):
                kitchen._execute_kot("ROOT", "Guest", "T1", [dict(previous, **change)], [previous],
                                     ignore_permissions=True)

    def transfer(self, destination_state, *, block):
        invoice = Row(name="ROOT", pos_profile="Till", branch="Main", docstatus=0,
                      restaurant_table="Old", creation="now", invoice_printed=0, save=Mock())
        origin = Row(name="Old", branch="Main", custom_table_state="Seated")
        destination = Row(name="New", branch="Main", restaurant_room="Room",
                          custom_table_state=destination_state)
        profile = Row(transfer_role_permissions=[])
        self.db.exists.return_value = None
        self.db.get_all.return_value = []
        self.db.get_value.return_value = destination_state
        with ExitStack() as stack:
            for obj, name, value in (
                (frappe, "get_doc", [invoice, destination, origin, profile]),
                (frappe, "has_permission", True), (frappe, "get_roles", []),
                (order, "_pa_assert_not_offline_replay", None), (order, "_pa_assert_not_efris_fiscalised", None),
                (order, "_pa_authorize_or_perform", None), (order, "_pa_is_company_policy_active", False),
                (order, "_get_merge_cluster", (["Old"], {})), (order, "_sync_service_charge", None),
                (order, "change_table_in_kot", None), (floor_actions, "assert_table_free_for_transfer", None),
                (booking, "check_table_reservation", None), (booking, "validate_seated_reservation_transfer", None),
                (booking, "transfer_seated_reservation", None),
                (floor, "table_policy", {"custom_block_dirty_table_seating": block}),
            ):
                kwargs = {"side_effect": value} if name == "get_doc" else {"return_value": value}
                stack.enter_context(patch.object(obj, name, **kwargs))
            order.table_transfer("Old", "New", "ROOT")

    def test_transfer_does_not_seat_dirty_table(self):
        with self.assertRaisesRegex(frappe.ValidationError, "TABLE-DIRTY-NEEDS-BUSSING"):
            self.transfer("Dirty", block=1)
        self.db.set_value.assert_not_called()

    def test_transfer_vacates_source_and_seats_destination(self):
        self.transfer("Available", block=0)
        writes = {call.args[1]: call.args[2] for call in self.db.set_value.call_args_list
                  if call.args[0] == "URY Table"}
        self.assertEqual(writes["Old"].get("custom_table_state"), "Available")
        self.assertEqual(writes["New"].get("custom_table_state"), "Seated")

    def test_waitlist_rejects_phone_that_cannot_be_used_in_tel_link(self):
        doc = Row(name="WAIT", status="Waiting", flags=Row(), insert=Mock())
        with (
            patch.object(booking, "_waitlist_access"),
            patch.object(booking, "now_datetime", return_value=datetime(2026, 11, 25)),
            patch.object(frappe, "get_doc", return_value=doc),
            self.assertRaises(frappe.ValidationError),
        ):
            booking.add_walkin_waitlist("Main", "Pat", "123;foo=bar", 2, quoted_wait_minutes=5)
        doc.insert.assert_not_called()

    def test_waitlist_uses_table_first_and_a_current_booking_read(self):
        party = Row(name="WAIT", is_waitlist=1, status="Waiting", branch="Main", no_of_pax=2,
                    requested_room=None, flags=Row(), save=Mock(), check_permission=Mock())
        table = Row(name="T1", branch="Main", custom_table_state="Available", occupied=0,
                    is_take_away=0, restaurant_room="Room", no_of_seats=4, check_permission=Mock())
        docs = {"URY Table Reservation": party, "URY Table": table}

        def active_booking(table, **options):
            return {"is_lock_window_active": True} if options.get("for_update") else None

        with (
            patch.object(booking, "assert_outlet_access"),
            patch.object(booking, "ensure_reservations_enabled"),
            patch.object(booking, "open_checks_on", return_value=[]) as checks,
            patch.object(booking, "check_table_reservation", side_effect=active_booking),
            patch.object(booking, "now_datetime", return_value=datetime(2026, 11, 25)),
            patch.object(frappe, "get_doc", side_effect=lambda dt, *args, **kw: docs[dt]) as load,
            self.assertRaisesRegex(frappe.ValidationError, "held for a reservation"),
        ):
            booking.seat_waitlist_party("WAIT", "T1")
        self.assertEqual([call.args[0] for call in load.call_args_list], ["URY Table", "URY Table Reservation"])
        checks.assert_called_once_with(["T1"], for_update=True)
        party.save.assert_not_called()

    def test_regular_seating_locks_table_before_reading_open_checks(self):
        row = Row(name="RES", reserved_table="T1")
        self.db.exists.return_value = None
        with patch.object(booking, "open_checks_on", return_value=[]) as checks:
            URYTableReservation.validate_table_is_free_to_seat(row)
        self.db.get_value.assert_any_call("URY Table", "T1", "name", for_update=True)
        checks.assert_called_once_with(["T1"], for_update=True)
