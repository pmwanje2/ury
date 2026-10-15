"""U3 behavior regressions using real staff pricing/KOT/ownership code, no site."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ury.ury.api.test_ury_kot_notes import OrderingCases


class Row(SimpleNamespace):
    def get(self, key, default=None):
        return getattr(self, key, default)


class HoldFireTests(OrderingCases, TestCase):
    def setUp(self):
        super().setUp()
        self.framework.session.user = "waiter@example.com"
        self.pos_profile.get = lambda key, default=None: getattr(self.pos_profile, key, default)
        self.pos_profile.ury_enable_hold_fire = 1
        self.invoice.docstatus = 0
        self.invoice.modified = "2026-10-16 12:00:00"
        self.invoice.get.side_effect = lambda key, default=None: getattr(self.invoice, key, default)
        self.invoice.as_dict.side_effect = lambda: {"name": self.invoice.name, "items": [vars(row).copy() for row in self.invoice.items]}
        self.invoice.reload.return_value = self.invoice
        self.invoice.append.side_effect = self.append_row
        self.framework.db.get_list.side_effect = lambda dt, **kw: ([Row(price_list_rate=100)] if dt == "Item Price" else [])
        self.framework.get_value.side_effect = self.get_value
        self.order.getBranch.return_value = "Branch A"
        self.order.getRoom.return_value = [{"name": "Dining"}]
        self.framework.db.sql.return_value = []

    def append_row(self, field, data):
        self.assertEqual(field, "items")
        row = Row(**data)
        row.name = data.get("name") or f"row-{len(self.invoice.items) + 1}"
        self.invoice.items.append(row)
        return row

    def row(self, name="held-1", held=1, qty=1, note="no onions", code="Fish", course="Main"):
        row = Row(name=name, item_code=code, item_name=code, qty=qty, comment=note,
                  ury_held=held, custom_course=course, rate=100)
        self.invoice.items.append(row)
        return row

    def sync(self, items, **kwargs):
        return self.order.sync_order(items, "untrusted", "untrusted", "Cash", "Walk-in", 2,
                                    self.invoice.name, "untrusted", self.pos_profile.name,
                                    last_modified_time=self.invoice.modified, **kwargs)

    @staticmethod
    def request(name=None, held=1, qty=1, note="no onions", code="Fish"):
        return dict(name=name, item=code, item_name=code, qty=qty, comment=note, ury_held=held, rate=1)

    def fire(self, **kwargs):
        method = getattr(self.order, "fire_items", None)
        self.assertTrue(callable(method), "Staff fire_items action is missing")
        return method(self.invoice.name, **kwargs)

    def test_hold_is_persisted_but_not_sent_and_price_remains_server_authoritative(self):
        self.sync([self.request()])
        self.assertEqual(self.invoice.items[0].get("ury_held"), 1)
        self.assertEqual(self.invoice.items[0].rate, 100)
        self.assertEqual(self.invoice.waiter, "waiter@example.com")
        self.assertEqual(self.ticket_lines(), [])

    def test_hold_and_fire_default_off_refuses_hold(self):
        self.pos_profile.ury_enable_hold_fire = 0
        with self.assertRaisesRegex(ValueError, "URY_HOLD_DISABLED"):
            self.sync([self.request()])
        self.invoice.save.assert_not_called()

    def test_offline_replay_refuses_held_lines(self):
        with self.assertRaisesRegex(ValueError, "URY_HOLD_OFFLINE"):
            self.sync([self.request()], offline_printed=1)

    def test_fire_line_is_idempotent_preserves_price_notes_and_sends_only_that_line(self):
        self.row()
        self.row("held-2", note="chilli", qty=2)
        self.fire(line="held-1")
        self.fire(line="held-1")
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions")])
        self.assertEqual([row.ury_held for row in self.invoice.items], [0, 1])
        self.assertEqual([row.rate for row in self.invoice.items], [100, 100])

    def test_fire_course_reuses_native_course_and_does_not_fire_other_courses(self):
        self.row()
        self.row("held-2", note="chilli", qty=2)
        self.row("dessert", code="Tea", course="Dessert")
        self.fire(course="Main")
        self.assertEqual(self.ticket_lines(), [("Fish", 1, "no onions"), ("Fish", 2, "chilli")])
        self.assertEqual(self.invoice.items[-1].ury_held, 1)

    def test_fire_same_dish_note_as_sent_line_sends_only_held_quantity(self):
        self.row("sent", held=0, qty=2)
        self.row(qty=3)
        self.fire(line="held-1")
        self.assertEqual(self.ticket_lines(), [("Fish", 3, "no onions")])

    def test_other_waiter_cannot_fire(self):
        self.row()
        self.framework.get_roles.return_value = ["URY Captain"]
        self.framework.session.user = "other@example.com"
        with self.assertRaises(PermissionError):
            self.fire(line="held-1")
        self.assertEqual(self.invoice.items[0].ury_held, 1)
        self.invoice.save.assert_not_called()

    def test_fire_requires_document_write_and_branch_and_room_access(self):
        self.row()
        self.framework.has_permission.return_value = False
        with self.assertRaises(PermissionError):
            self.fire(line="held-1")
        self.framework.has_permission.return_value = True
        self.order.getBranch.return_value = "Other branch"
        with self.assertRaises(PermissionError):
            self.fire(line="held-1")
        self.order.getBranch.return_value = "Branch A"
        self.invoice.restaurant_table = "T-1"
        self.framework.get_roles.return_value = ["URY Captain"]
        original = self.framework.db.get_value.side_effect
        self.framework.db.get_value.side_effect = lambda dt, key, field: "Other room" if field == "restaurant_room" else original(dt, key, field)
        with self.assertRaises(PermissionError):
            self.fire(line="held-1")

    def test_fire_refuses_closed_disabled_and_invalid_selection(self):
        self.row()
        for kwargs in ({}, {"line": "missing"}, {"line": "held-1", "course": "Main"}, {"course": "missing"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.fire(**kwargs)
        self.pos_profile.ury_enable_hold_fire = 0
        with self.assertRaisesRegex(ValueError, "URY_HOLD_DISABLED"):
            self.fire(line="held-1")
        self.pos_profile.ury_enable_hold_fire = 1
        self.invoice.docstatus = 1
        with self.assertRaises(ValueError):
            self.fire(line="held-1")

    def test_sync_cannot_silently_fire_or_rehold_sent_line(self):
        self.row()
        with self.assertRaisesRegex(ValueError, "URY_HOLD_REQUIRES_FIRE"):
            self.sync([self.request("held-1", held=0)])
        self.invoice.items = []
        self.row("sent", held=0)
        with self.assertRaisesRegex(ValueError, "URY_ALREADY_SENT"):
            self.sync([self.request("sent")])

    def test_held_identity_survives_sync_and_unsent_removal_does_not_cancel_kitchen(self):
        self.row()
        self.row("sent", held=0, note="chilli", qty=2)
        self.sync([self.request("held-1", qty=3), self.request("sent", held=0, note="chilli", qty=1)])
        self.assertEqual(self.invoice.items[0].name, "held-1")
        self.assertEqual([(r.comments, r.cancelled_qty) for t in self.tickets for r in t.kot_items], [("chilli", 1)])
        self.tickets.clear()
        self.sync([self.request("sent", held=0, note="chilli", qty=1)])
        self.assertEqual(self.tickets, [])

    def test_sync_does_not_swallow_kitchen_failure_when_feature_enabled(self):
        with patch.object(self.order, "kot_execute", side_effect=RuntimeError("printer failed")):
            with self.assertRaisesRegex(RuntimeError, "printer failed"):
                self.sync([self.request(held=0)])

    def test_whole_order_cancel_excludes_held_lines(self):
        self.row()
        self.row("sent", held=0, note="chilli", qty=2)
        with patch.object(self.order, "process_items_for_cancel_kot") as cancel:
            self.order.cancel_kot(self.invoice.name)
        self.assertEqual([(r["comments"], r["qty"]) for r in cancel.call_args.args[3]], [("chilli", 2)])

    def test_settlement_refuses_held_lines_even_if_switch_was_turned_off(self):
        self.row()
        self.pos_profile.ury_enable_hold_fire = 0
        with self.assertRaisesRegex(ValueError, "URY_HELD_NOT_FIRED"):
            self.order.make_invoice("Walk-in", [], "untrusted", "POS A", "untrusted", invoice=self.invoice.name)
        self.invoice.save.assert_not_called()
        self.invoice.submit.assert_not_called()

    def test_split_copy_preserves_hold_flag(self):
        row = self.row()
        for key in ("price_list_rate", "base_price_list_rate", "cost_center", "uom", "conversion_factor", "warehouse"):
            setattr(row, key, 1)
        self.assertEqual(self.order._copy_invoice_item_fields(row, 1).get("ury_held"), 1)

    def test_fire_locks_and_reloads_invoice_before_reading_held_state(self):
        self.row()
        self.fire(line="held-1")
        self.assertTrue(any("for update" in call.args[0].lower() for call in self.framework.db.sql.call_args_list))
        self.invoice.reload.assert_called()

    def test_adding_a_new_dish_preserves_an_existing_held_row(self):
        self.row()
        self.sync([self.request("held-1"), self.request(held=0, code="Tea")])
        self.assertEqual(self.ticket_lines(), [("Tea", 1, "no onions")])
        self.assertEqual(self.invoice.items[0].ury_held, 1)

    def test_append_round_with_existing_held_line_sends_only_the_new_round(self):
        self.row()
        self.sync([self.request(held=0, code="Tea")], append=1)
        self.assertEqual(self.ticket_lines(), [("Tea", 1, "no onions")])
        self.assertEqual(self.invoice.items[0].ury_held, 1)

    def test_table_order_restricted_role_cannot_fire_even_with_billing_access(self):
        self.row()
        self.invoice.order_type = "Dine In"
        self.pos_profile.role_restricted_for_table_order = [Row(role="URY Cashier")]
        with self.assertRaises(PermissionError):
            self.fire(line="held-1")

    def test_guest_addition_does_not_release_existing_held_items(self):
        self.row()
        self.add(code="Tea")
        self.assertEqual(self.ticket_lines(), [("Tea", 1, "")])
        self.assertEqual(self.invoice.items[0].ury_held, 1)

    def test_after_fire_next_sync_and_cancel_keep_the_native_diffs(self):
        self.row(qty=2)
        self.row("held-2", qty=4, note="chilli")
        self.fire(line="held-1")
        self.tickets.clear()
        self.sync([self.request("held-1", held=0, qty=1), self.request("held-2", qty=4, note="chilli")])
        self.assertEqual([(r.comments, r.cancelled_qty) for t in self.tickets for r in t.kot_items], [("no onions", 1)])

    def test_stale_fire_and_kot_failure_are_not_acknowledged_as_success(self):
        self.row()
        with self.assertRaisesRegex(ValueError, "modified"):
            self.fire(line="held-1", last_modified_time="old version")
        with patch.object(self.order, "kot_execute", side_effect=RuntimeError("KOT failed")):
            with self.assertRaisesRegex(RuntimeError, "KOT failed"):
                self.fire(line="held-1")

    def test_native_custom_fields_default_off_and_hold_survives_upgrade(self):
        native = ModuleType("frappe.custom.doctype.custom_field.custom_field")
        native.create_custom_fields = MagicMock()
        roles = ModuleType("ury.role_permissions")
        roles.setup_role_permissions = MagicMock()
        with patch.dict(sys.modules, {native.__name__: native, roles.__name__: roles}):
            path = Path(__file__).resolve().parents[2] / "setup_customizations.py"
            spec = importlib.util.spec_from_file_location("hold_fields_test", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fields = module.get_custom_fields()
        profile = next((f for f in fields["POS Profile"] if f["fieldname"] == "ury_enable_hold_fire"), None)
        self.assertIsNotNone(profile, "Default-off POS Profile hold/fire flag not installed")
        self.assertEqual((profile["fieldtype"], profile["default"]), ("Check", "0"))
        held = next((f for f in fields["POS Invoice Item"] if f["fieldname"] == "ury_held"), None)
        self.assertIsNotNone(held, "Native invoice hold field not installed")
        self.assertEqual((held["fieldtype"], held["default"]), ("Check", "0"))
        patches = (Path(__file__).resolve().parents[2] / "patches.txt").read_text()
        self.assertIn("ury.patches.v2_0.hold_fire_fields", patches)
