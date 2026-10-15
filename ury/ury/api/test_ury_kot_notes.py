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
        self.assertEqual([row["comment"] for row in result["items"]], ["no onions", "extra chilli"])
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
        self.assertEqual(result["items"][0]["comment"], "n" * 140)

    def test_unicode_note_is_capped_in_characters_not_utf8_bytes(self):
        result = self.add("é" * 141)
        self.assertEqual(result["items"][0]["comment"], "é" * 140)

    def test_note_at_the_column_limit_reaches_the_kitchen_without_truncation(self):
        self.add("n" * 140)
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "n" * 140)])

    def test_short_note_is_not_truncated(self):
        result = self.add("n" * 139)
        self.assertEqual(result["items"][0]["comment"], "n" * 139)

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
