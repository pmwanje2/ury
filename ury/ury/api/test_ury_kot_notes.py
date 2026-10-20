"""Site-less U1 regressions: real ordering/pricing/KOT code, fake DB and printer."""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch


class OrderingCases:
    def setUp(self):
        self.framework = MagicMock()
        self.framework._ = lambda text: text
        self.framework.ValidationError = ValueError
        self.framework.PermissionError = PermissionError
        self.framework.whitelist.side_effect = lambda **kwargs: lambda function: function
        self.framework.session = SimpleNamespace(user="Guest")
        self.framework.set_user.side_effect = lambda user: setattr(self.framework.session, "user", user)
        self.framework.has_permission.return_value = True
        self.framework.get_roles.return_value = ["URY Cashier"]
        self.framework.throw.side_effect = lambda message, exc=ValueError: self.raise_error(message, exc)
        self.framework.db.exists.return_value = True
        self.framework.db.get_value.side_effect = self.get_value
        self.framework.db.get_list.return_value = [SimpleNamespace(price_list_rate=100)]
        self.framework.db.get_all.return_value = [SimpleNamespace(name="Kitchen")]
        self.framework.get_all.return_value = [SimpleNamespace(item_group="Food")]
        self.framework.get_doc.side_effect = self.get_doc

        utils = ModuleType("frappe.utils")
        utils.cint = lambda value: int(value or 0)
        utils.flt = lambda value: float(value or 0)
        utils.get_datetime = lambda value: datetime.fromisoformat(str(value))
        utils.now_datetime = lambda: datetime(2026, 10, 16, 12)
        utils.get_system_timezone = lambda: "Africa/Kampala"
        utils.add_to_date = MagicMock()
        document = ModuleType("frappe.model.document")
        document.Document = object
        rate_limiter = ModuleType("frappe.rate_limiter")
        # These cases exercise orders/KOTs, not feedback throttling. Keep the
        # native decorator importable without substituting the feedback suite.
        rate_limiter.rate_limit = lambda **kwargs: lambda function: function
        queries = ModuleType("erpnext.controllers.queries")
        queries.item_query = MagicMock()
        pos_api = ModuleType("ury.ury_pos.api")
        for name in ("getBranch", "getBranchRoom", "getRoom", "posOpening"):
            setattr(pos_api, name, MagicMock(return_value="Branch A"))
        pos_api.resolve_restaurant_menu = MagicMock(return_value={"items": [{"item": "Fish"}, {"item": "Tea"}]})
        printer = ModuleType("ury.ury.api.ury_waiter_print")
        printer.print_combined_waiter_order_slip = MagicMock()
        self.enterContext(patch.dict(sys.modules, {
            "frappe": self.framework, "frappe.utils": utils,
            "frappe.rate_limiter": rate_limiter,
            "frappe.model.document": document, "erpnext.controllers.queries": queries,
            "ury.ury_pos.api": pos_api, "ury.ury.api.ury_waiter_print": printer,
        }))
        self.kot = self.load_module("ury.ury.api.ury_kot_generate", "ury_kot_generate.py")
        self.order = self.load_module("ury.ury.doctype.ury_order.ury_order", "../doctype/ury_order/ury_order.py")
        self.guest = self.load_module("ury.ury.api.self_ordering", "self_ordering.py")

        self.profile = SimpleNamespace(
            name="Guest Profile", enabled=1, allow_add_to_running_table=1,
            branch="Branch A", pos_profile="POS A", default_customer="Walk-in",
        )
        self.pos_profile = SimpleNamespace(
            name="POS A", branch="Branch A", custom_kot_naming_series="KOT-.#####",
            role_allowed_for_billing=[SimpleNamespace(role="URY Cashier")],
            transfer_role_permissions=[], role_restricted_for_table_order=[],
            custom_enable_multiple_cashier=0, applicable_for_users=[], remove_items=1,
        )
        self.session = MagicMock(table=None, invoice="POS-TEST", source="QR Pickup", device=None)
        self.session.name = "SESSION-TEST"
        self.session.ordering_profile = self.profile.name
        self.invoice = MagicMock()
        for name, value in {
            "name": "POS-TEST", "customer": "Walk-in", "restaurant_table": None,
            "order_type": "Take Away", "branch": "Branch A", "pos_profile": "POS A",
            "selling_price_list": "Standard Selling", "invoice_created": 1, "invoice_printed": 0,
            "grand_total": 100, "items": [], "custom_merged_tables": "", "waiter": "waiter@example.com",
        }.items():
            setattr(self.invoice, name, value)
        self.invoice.append.side_effect = self.append_invoice
        self.tickets = []
        self.historical_tickets = {}
        self.enterContext(patch.object(self.guest, "_resolve_session", return_value=self.session))
        self.enterContext(patch.object(self.guest, "_resolve_or_create_pos_invoice", return_value=(self.invoice, self.invoice.name)))
        self.enterContext(patch.object(self.order, "get_order_invoice", return_value=self.invoice))

    def load_module(self, name, relative_path):
        path = Path(__file__).parent / relative_path
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {name: module}))
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def raise_error(message, exception):
        raise exception(message)

    @staticmethod
    def get_value(doctype, filters, field):
        return {"item_group": "Food", "course": "Main", "cost_center": "CC", "name": "Menu A",
                "active_menu": "Menu A"}.get(field)

    def get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            self.assertEqual(doctype["doctype"], "URY KOT")
            ticket = SimpleNamespace(**doctype, name=f"KOT-{len(self.tickets) + 1}", kot_items=[])
            ticket.append = lambda field, row: ticket.kot_items.append(SimpleNamespace(**row))
            ticket.insert = MagicMock()
            ticket.submit = MagicMock()
            self.tickets.append(ticket)
            return ticket
        if doctype == "URY KOT":
            return self.historical_tickets[name]
        return {
            "URY Self Ordering Profile": self.profile, "POS Profile": self.pos_profile,
            "POS Invoice": self.invoice, "Customer": SimpleNamespace(mobile_number=""),
            "URY Production Unit": SimpleNamespace(item_groups=[SimpleNamespace(item_group="Food")]),
            "Item": SimpleNamespace(item_group="Food"),
        }[doctype]

    def append_invoice(self, field, row):
        self.assertEqual(field, "items")
        self.invoice.items.append(SimpleNamespace(**row, amount=row["qty"] * row["rate"]))

    @staticmethod
    def line(note="", qty=1, code="Fish"):
        return {"item_code": code, "item_name": code, "qty": qty, "comments": note}

    def seed(self, note="", qty=1, code="Fish"):
        self.invoice.items.append(SimpleNamespace(
            item_code=code, item_name=code, qty=qty, comment=note, rate=100, amount=100 * qty,
        ))

    def add(self, note="", qty=1, code="Fish"):
        result = self.guest.add_customer_items("test-session", [{"item": code, "qty": qty, "comment": note}])
        self.framework.log_error.assert_not_called()
        self.assertEqual(self.framework.session.user, "Guest")
        return result

    def ticket_lines(self):
        return [(row.item, row.quantity, row.comments) for ticket in self.tickets for row in ticket.kot_items]


class TestKOTNoteDiff(OrderingCases, TestCase):
    def test_repeat_dish_with_different_note_is_not_dropped(self):
        previous = [self.line("no onions")]
        current = [self.line("no onions"), self.line("extra chilli")]
        self.assertEqual(self.kot.compare_two_array(current, previous), [self.line("extra chilli")])

    def test_quantity_delta_only_subtracts_the_matching_note(self):
        previous = [self.line("no onions", 2), self.line("extra chilli", 1)]
        current = [self.line("no onions", 3), self.line("extra chilli", 2)]
        self.assertEqual(self.kot.compare_two_array(current, previous),
                         [self.line("no onions", 1), self.line("extra chilli", 1)])

    def test_unchanged_noted_lines_do_not_generate_a_ticket(self):
        self.assertEqual(self.kot.compare_two_array([self.line("no onions")], [self.line("no onions")]), [])

    def test_legacy_unnoted_order_still_sends_only_quantity_delta(self):
        previous = [{"item_code": "Fish", "item_name": "Fish", "qty": 2}]
        current = [{"item_code": "Fish", "item_name": "Fish", "qty": 3}]
        self.assertEqual(self.kot.compare_two_array(current, previous),
                         [{"item_code": "Fish", "item_name": "Fish", "qty": 1}])

    def test_missing_null_and_empty_notes_match(self):
        for previous in ({"item_code": "Fish", "qty": 1}, self.line(None), self.line("")):
            with self.subTest(previous=previous):
                self.assertEqual(self.kot.compare_two_array([self.line("")], [previous]), [])
                self.assertEqual(self.kot.get_removed_items([previous], [self.line("")]), [])

    def test_removed_note_variant_is_cancelled_without_removing_the_dish(self):
        previous = [self.line("no onions"), self.line("extra chilli")]
        self.assertEqual(self.kot.get_removed_items(previous, [self.line("no onions")]),
                         [self.line("extra chilli")])

    def test_legacy_removed_dish_is_still_cancelled(self):
        self.assertEqual(self.kot.get_removed_items([self.line()], []), [self.line()])

    def test_cancellation_line_uses_the_quantity_of_its_note_variant(self):
        self.framework.db.get_list.return_value = []
        previous = [self.line("no onions", 2), self.line("extra chilli", 3)]
        self.kot.kot_execute(self.invoice.name, self.invoice.customer, None,
                             [self.line("no onions", 1), self.line("extra chilli", 3)], previous)
        self.assertEqual([(row.quantity, row.cancelled_qty, row.comments)
                          for ticket in self.tickets for row in ticket.kot_items], [(2, 1, "no onions")])

    def test_cancellation_links_the_original_ticket_with_the_matching_note(self):
        self.historical_tickets = {
            "KOT-PLAIN": SimpleNamespace(name="KOT-PLAIN", kot_items=[SimpleNamespace(item="Fish", comments="no onions")]),
            "KOT-HOT": SimpleNamespace(name="KOT-HOT", kot_items=[SimpleNamespace(item="Fish", comments="extra chilli")]),
        }
        self.framework.db.get_list.return_value = [SimpleNamespace(name=name) for name in self.historical_tickets]
        self.kot.create_cancel_kot_doc(
            self.invoice.name, None, [self.line("extra chilli")], "Partially cancelled", "Walk-in",
            None, "POS A", "CNCL-.#####", [self.line("no onions"), self.line("extra chilli")], "Kitchen",
        )
        self.assertEqual(self.tickets[0].original_kot, "KOT-HOT")

    def test_changing_a_note_cancels_the_old_variant_and_sends_the_new_one(self):
        self.framework.db.get_list.return_value = []
        self.kot.kot_execute(self.invoice.name, "Walk-in", None,
                             [self.line("extra chilli")], [self.line("no onions")])
        self.assertEqual([(ticket.type, row.comments) for ticket in self.tickets for row in ticket.kot_items],
                         [("Order Modified", "extra chilli"), ("Partially cancelled", "no onions")])


class TestGuestKOTNotes(OrderingCases, TestCase):
    def test_guest_note_reaches_the_real_kot_line(self):
        self.add("no onions")
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])

    def test_second_round_keeps_both_notes_and_sends_only_the_new_dish(self):
        self.seed("no onions")
        result = self.add("extra chilli")
        self.assertEqual([row.comment for row in self.invoice.items], ["no onions", "extra chilli"])
        self.assertEqual([row["comment"] for row in result["items"]], [None, None])
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "extra chilli")])

    def test_repeated_rows_with_the_same_note_produce_only_the_added_quantity(self):
        self.seed("no onions")
        self.seed("no onions")
        self.add("no onions")
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])

    def test_old_noted_dish_is_not_resent_or_cancelled_when_another_dish_is_added(self):
        self.seed("no onions")
        self.add(code="Tea")
        self.assertEqual(self.ticket_lines(), [("Tea", 1, "")])

    def test_two_notes_on_one_dish_in_the_same_request_stay_separate(self):
        self.guest.add_customer_items("test-session", [
            {"item": "Fish", "qty": 1, "comment": "no onions"},
            {"item": "Fish", "qty": 1, "comment": "extra chilli"},
        ])
        self.framework.log_error.assert_not_called()
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions"), ("Fish", 1, "extra chilli")])

    def test_legacy_unnoted_repeats_still_send_only_the_added_quantity(self):
        self.seed(None)
        self.seed("")
        self.add()
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "")])

    def test_guest_note_is_capped_to_the_140_character_data_columns_before_pricing(self):
        result = self.add("n" * 141)
        self.assertEqual(self.invoice.items[0].comment, "n" * 140)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "n" * 140)])
        self.assertIsNone(result["items"][0]["comment"])

    def test_unicode_note_is_capped_in_characters_not_utf8_bytes(self):
        result = self.add("é" * 141)
        self.assertEqual(self.invoice.items[0].comment, "é" * 140)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "é" * 140)])
        self.assertIsNone(result["items"][0]["comment"])

    def test_note_at_the_column_limit_reaches_the_kitchen_without_truncation(self):
        self.add("n" * 140)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "n" * 140)])

    def test_short_note_is_not_truncated(self):
        result = self.add("n" * 139)
        self.assertEqual(self.invoice.items[0].comment, "n" * 139)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "n" * 139)])
        self.assertIsNone(result["items"][0]["comment"])

    def test_null_guest_note_remains_backward_compatible(self):
        self.add(None)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "")])


class TestStaffKOTNotes(OrderingCases, TestCase):
    def test_whole_order_void_preserves_variant_notes_quantities_and_original_tickets(self):
        self.seed("no onions", 2)
        self.seed("extra chilli")
        for row in self.invoice.items:
            row.get = lambda key, default=None, row=row: getattr(row, key, default)
        self.historical_tickets = {
            "KOT-PLAIN": SimpleNamespace(name="KOT-PLAIN", kot_items=[SimpleNamespace(item="Fish", comments="no onions")], save=MagicMock()),
            "KOT-HOT": SimpleNamespace(name="KOT-HOT", kot_items=[SimpleNamespace(item="Fish", comments="extra chilli")], save=MagicMock()),
        }
        self.framework.db.get_list.return_value = [SimpleNamespace(name=name) for name in self.historical_tickets]

        self.order.cancel_kot(self.invoice.name)

        self.assertEqual(len(self.tickets), 1)
        ticket = self.tickets[0]
        self.assertEqual(set(ticket.original_kot.split(",")), {"KOT-PLAIN", "KOT-HOT"})
        self.assertEqual(ticket.type, "Cancelled")
        self.assertEqual([(row.quantity, row.cancelled_qty, row.comments) for row in ticket.kot_items],
                         [(2, 2, "no onions"), (1, 1, "extra chilli")])
        self.assertEqual([ticket.docstatus for ticket in self.historical_tickets.values()], [2, 2])

    def test_staff_sync_preserves_existing_note_in_the_kot_baseline(self):
        self.seed("no onions")
        with patch.object(self.order, "kot_execute", wraps=self.kot.kot_execute) as execute:
            self.order.sync_order(
                items=[{"item": "Fish", "item_name": "Fish", "qty": 1, "comment": "no onions"}],
                cashier=None, owner=None, mode_of_payment="Cash", customer="Walk-in", no_of_pax=1,
                last_invoice=None, waiter="waiter@example.com", pos_profile="POS A", order_type="Take Away",
            )
        self.assertEqual(execute.call_args.args[4], [self.line("no onions")])
        self.framework.log_error.assert_not_called()
        self.assertEqual(self.tickets, [], "An unchanged noted staff line must not be sent again")


class TestSentNotePermissions(OrderingCases, TestCase):
    def setUp(self):
        super().setUp()
        self.framework.session.user = "waiter@example.com"
        self.framework.get_roles.return_value = ["URY Captain"]
        self.pos_profile.remove_items = 0
        self.invoice.modified = "2026-10-16 12:00:00"
        for name, value in {
            "mobile_number": "old phone", "custom_comments": "old comment", "no_of_pax": 1,
            "cashier": "cashier@example.com", "custom_aggregator_id": None,
            "custom_restaurant_room": None, "payments": [],
        }.items():
            setattr(self.invoice, name, value)
        self.framework.db.get_list.side_effect = lambda doctype, **kwargs: (
            [] if doctype == "URY KOT" else [SimpleNamespace(price_list_rate=100)]
        )

    def append_invoice(self, field, row):
        if field == "payments":
            self.invoice.payments.append(SimpleNamespace(**row))
        else:
            super().append_invoice(field, row)

    @staticmethod
    def requested(note="", qty=1):
        return {"item": "Fish", "item_name": "Fish", "qty": qty, "comment": note, "rate": 0.01}

    def send(self, items, **kwargs):
        return self.order.sync_order(
            items=items, cashier=None, owner=None, mode_of_payment="Cash", customer="Named Customer",
            no_of_pax=5, last_invoice=self.invoice.name, last_modified_time=self.invoice.modified,
            waiter="ignored@example.com", pos_profile="POS A", order_type="Take Away",
            comments="new comment", **kwargs,
        )

    def invoice_state(self):
        fields = (
            "customer", "order_type", "mobile_number", "custom_comments", "no_of_pax", "pos_profile",
            "cashier", "waiter", "custom_aggregator_id", "custom_restaurant_room", "restaurant_table",
            "custom_merged_tables", "invoice_created",
        )
        return {
            **{name: getattr(self.invoice, name) for name in fields},
            "items": [vars(row).copy() for row in self.invoice.items],
            "payments": [vars(row).copy() for row in self.invoice.payments],
        }

    def assert_denied_without_mutation(self, items):
        before = self.invoice_state()
        with patch.object(self.order, "kot_execute", wraps=self.kot.kot_execute) as execute:
            with self.assertRaisesRegex(PermissionError, "not permitted to reduce or remove items"):
                self.send(items)
            execute.assert_not_called()
        self.assertEqual(self.invoice_state(), before)
        self.invoice.append.assert_not_called()
        self.invoice.save.assert_not_called()
        self.assertEqual(self.tickets, [])
        self.framework.db.set_value.assert_not_called()
        self.framework.db.commit.assert_not_called()

    def assert_invalid_line_names_without_mutation(self, items, **kwargs):
        self.pos_profile.ury_enable_hold_fire = 0
        self.invoice.invoice_created = 0
        before = self.invoice_state()
        with patch.object(self.order, "kot_execute", wraps=self.kot.kot_execute) as execute:
            with self.assertRaisesRegex(ValueError, "line"):
                self.send(items, **kwargs)
            execute.assert_not_called()
        self.assertEqual(self.invoice_state(), before)
        self.invoice.append.assert_not_called()
        self.invoice.save.assert_not_called()
        self.assertEqual(self.tickets, [])
        self.framework.db.set_value.assert_not_called()
        self.framework.db.commit.assert_not_called()

    def named_sent_lines(self):
        self.seed("no onions")
        self.invoice.items[-1].name = "line-A"
        self.seed("extra chilli")
        self.invoice.items[-1].name = "line-B"

    def test_duplicate_line_a_cannot_replace_sent_line_b_with_hold_disabled(self):
        self.named_sent_lines()
        self.assert_invalid_line_names_without_mutation([
            {**self.requested("no onions"), "name": "line-A"},
            {**self.requested("extra chilli"), "name": "line-A"},
        ])

    def test_duplicate_line_b_cannot_replace_sent_line_a_with_hold_disabled(self):
        self.named_sent_lines()
        self.assert_invalid_line_names_without_mutation([
            {**self.requested("no onions"), "name": "line-B"},
            {**self.requested("extra chilli"), "name": "line-B"},
        ])

    def test_nonadjacent_duplicate_line_names_are_refused_in_full_replacement(self):
        self.named_sent_lines()
        self.assert_invalid_line_names_without_mutation([
            {**self.requested("no onions", 0.5), "name": "line-A"},
            {**self.requested("extra chilli"), "name": "line-B"},
            {**self.requested("no onions", 0.5), "name": "line-A"},
        ])

    def test_append_refuses_existing_line_name_before_adding_any_round_items(self):
        self.named_sent_lines()
        self.assert_invalid_line_names_without_mutation([
            self.requested("new round"),
            {**self.requested("no onions"), "name": "line-A"},
        ], append=1)

    def test_full_replacement_refuses_unknown_line_name_without_mutation(self):
        self.named_sent_lines()
        self.framework.db.exists.return_value = False
        self.assert_invalid_line_names_without_mutation([
            {**self.requested("no onions"), "name": "line-A"},
            {**self.requested("extra chilli"), "name": "unknown-line"},
        ])

    def test_full_replacement_refuses_foreign_line_name_without_mutation(self):
        self.named_sent_lines()
        self.framework.db.exists.return_value = True
        self.assert_invalid_line_names_without_mutation([
            {**self.requested("no onions"), "name": "line-A"},
            {**self.requested("extra chilli"), "name": "other-invoice-line"},
        ])

    def test_append_refuses_unknown_or_foreign_line_names_without_mutation(self):
        self.named_sent_lines()
        for name, exists in (("unknown-line", False), ("other-invoice-line", True)):
            with self.subTest(name=name):
                self.framework.db.exists.return_value = exists
                self.assert_invalid_line_names_without_mutation([
                    self.requested("new round"),
                    {**self.requested("no onions"), "name": name},
                ], append=1)

    def test_distinct_existing_line_names_remain_valid_with_hold_disabled(self):
        self.pos_profile.ury_enable_hold_fire = 0
        self.named_sent_lines()
        self.send([
            {**self.requested("no onions"), "name": "line-A"},
            {**self.requested("extra chilli"), "name": "line-B"},
        ])
        self.assertEqual([(row.name, row.comment, row.qty) for row in self.invoice.items],
                         [("line-A", "no onions", 1), ("line-B", "extra chilli", 1)])
        self.assertEqual(self.tickets, [])
        self.invoice.save.assert_called_once()

    def test_captain_cannot_replace_a_sent_note_without_removal_permission(self):
        self.seed("no onions")
        self.assert_denied_without_mutation([self.requested("extra chilli")])

    def test_captain_cannot_move_one_sent_noted_portion_to_plain(self):
        self.seed("no onions", 2)
        self.seed("", 1)
        self.assert_denied_without_mutation([self.requested("no onions"), self.requested("", 2)])

    def test_repeated_sent_rows_cannot_hide_a_variant_reduction(self):
        self.seed("no onions")
        self.seed("no onions")
        self.seed("")
        self.assert_denied_without_mutation([self.requested("no onions"), self.requested("", 2)])

    def test_quantity_reduction_is_refused_before_header_or_dummy_payment_mutation(self):
        self.seed("no onions", 2)
        self.invoice.invoice_created = 0
        self.assert_denied_without_mutation([self.requested("no onions")])

    def test_full_line_removal_is_refused_without_mutation(self):
        self.seed("no onions")
        self.assert_denied_without_mutation([])

    def test_manager_with_removal_permission_can_replace_a_sent_note(self):
        self.framework.get_roles.return_value = ["URY Manager"]
        self.pos_profile.remove_items = 1
        self.seed("no onions")
        self.send([self.requested("extra chilli")])
        self.framework.log_error.assert_not_called()
        self.assertEqual([(ticket.type, row.comments, getattr(row, "cancelled_qty", None))
                          for ticket in self.tickets for row in ticket.kot_items],
                         [("Order Modified", "extra chilli", None), ("Partially cancelled", "no onions", 1)])
        self.assertEqual([(row.comment, row.qty, row.rate) for row in self.invoice.items],
                         [("extra chilli", 1, 100)])
        self.invoice.save.assert_called_once()

    def test_removal_permission_does_not_require_a_manager_role(self):
        self.pos_profile.remove_items = 1
        self.seed("no onions", 2)
        self.seed("")
        self.send([self.requested("no onions"), self.requested("", 2)])
        self.framework.log_error.assert_not_called()
        self.assertEqual([(ticket.type, row.comments, getattr(row, "cancelled_qty", None))
                          for ticket in self.tickets for row in ticket.kot_items],
                         [("Order Modified", "", None), ("Partially cancelled", "no onions", 1)])

    def test_new_note_variant_does_not_require_void_permission_when_sent_note_remains(self):
        self.seed("no onions")
        self.send([self.requested("no onions"), self.requested("extra chilli")])
        self.framework.log_error.assert_not_called()
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "extra chilli")])
        self.assertEqual([ticket.type for ticket in self.tickets], ["Order Modified"])

    def test_new_unsent_order_note_does_not_require_void_permission(self):
        self.invoice.name = None
        self.invoice.waiter = None
        self.send([self.requested("extra chilli")])
        self.framework.log_error.assert_not_called()
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "extra chilli")])
        self.assertEqual(self.invoice.waiter, "waiter@example.com")

    def test_append_round_does_not_treat_omitted_sent_notes_as_removals(self):
        self.seed("no onions")
        self.send([self.requested("extra chilli")], append=1)
        self.framework.log_error.assert_not_called()
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "extra chilli")])
        self.assertEqual([(row.comment, row.qty) for row in self.invoice.items],
                         [("no onions", 1), ("extra chilli", 1)])

    def test_null_missing_and_empty_notes_remain_the_same_sent_identity(self):
        self.seed(None)
        for items in ([self.requested(None)], [self.requested("")],
                      [{"item": "Fish", "item_name": "Fish", "qty": 1}]):
            with self.subTest(items=items):
                self.send(items)
                self.framework.log_error.assert_not_called()
                self.assertEqual(self.tickets, [])

    def test_unchanged_repeated_rows_are_aggregated_for_permission(self):
        self.seed("no onions")
        self.seed("no onions")
        self.send([self.requested("no onions"), self.requested("no onions")])
        self.framework.log_error.assert_not_called()
        self.assertEqual(self.tickets, [])
        self.assertEqual([(row.comment, row.qty) for row in self.invoice.items],
                         [("no onions", 1), ("no onions", 1)])

    def test_removal_permission_does_not_bypass_another_waiters_order_ownership(self):
        self.pos_profile.remove_items = 1
        self.invoice.waiter = "other@example.com"
        self.seed("no onions")
        before = self.invoice_state()
        with self.assertRaisesRegex(PermissionError, "Not permitted to modify this order"):
            self.send([self.requested("extra chilli")])
        self.assertEqual(self.invoice_state(), before)
        self.invoice.save.assert_not_called()
        self.assertEqual(self.tickets, [])
