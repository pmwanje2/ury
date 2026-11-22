"""Site-less tests; the contention test executes the production UPDATE in SQLite, not a bench."""

import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier, local
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ury.ury.api import test_availability_tiers as tiers
from ury.ury.api.test_guest_session_reads import Doc
from ury.ury.api import test_ury_hold_fire
from ury.ury.api.test_ury_kot_notes import OrderingCases

DT = "URY Item Availability"
APP = Path(__file__).resolve().parents[1]
BRANCH, COMPANY = tiers.BRANCH, tiers.COMPANY
FISH = [{"item": "Fish", "qty": 1}]


class TestAvailabilityOperations(unittest.TestCase):
    load = staticmethod(tiers.TestAvailabilityTiers.load)
    def enable_reservations(self):
        tiers.TestAvailabilityTiers.enable_reservations(self)
        self.frappe.local = type("Local", (), {})()

    def setUp(self):
        tiers.TestAvailabilityTiers.setUp(self)
        self.row = Doc(name="override", branch=BRANCH, item="Fish", status="Sold Out", flags=SimpleNamespace(),
                       countdown_enabled=1, remaining_qty=3, daily_limit=3, auto_reset_on_open=1,
                       last_reset_on=None, reason="Kitchen closed", updated_by="operator", updated_at=None)
        self.frappe.get_all.side_effect = self.get_all
        self.frappe.db.get_values.side_effect = self.get_all
        self.frappe._dict.side_effect = Doc

    def get_all(self, doctype, filters=None, fields=None, **kwargs):
        if doctype == DT:
            codes = filters.get("item", ("in", [self.row.item]))
            return [self.row.copy()] if filters.get("branch") == BRANCH and self.row.item in codes[1] else []
        return [{"name": code, "disabled": self.items[code]} for code in filters["name"][1] if code in self.items]

    def get_value(self, dt, name, field, **kwargs):
        if dt == "URY Menu":
            return "Lunch"
        if dt == DT:
            if field == "name":
                return self.row.name
            return self.row if isinstance(field, list) else self.row.get(field)
        return tiers.TestAvailabilityTiers.get_value(self, dt, name, field, **kwargs)

    def require(self, name):
        method = getattr(self.api, name, None)
        self.assertTrue(callable(method), f"{name} is missing")
        return method

    def test_schema_tracks_manual_changes_and_does_not_grant_floor_writes(self):
        schema = APP / "doctype/ury_item_availability/ury_item_availability.json"
        self.assertTrue(schema.is_file(), "Availability DocType is missing")
        data = json.loads(schema.read_text())
        self.assertEqual(data["track_changes"], 1)
        fields = {f["fieldname"]: f for f in data["fields"]}
        self.assertEqual(fields["status"]["options"], "Available\nSold Out")
        self.assertTrue({"branch", "item", "countdown_enabled", "remaining_qty", "daily_limit",
                         "auto_reset_on_open", "last_reset_on", "reason", "updated_by", "updated_at"} <= fields.keys())
        self.assertTrue(all(p["role"] in {"URY Manager", "URY Admin", "System Manager"}
                            for p in data["permissions"] if p.get("write") or p.get("create")))

    def test_staff_and_guest_batches_show_manual_sold_out_in_both_tiers(self):
        for tier in (1, 2):
            with self.subTest(tier=tier):
                if tier == 2:
                    self.enable_reservations()
                staff = self.api.get_menu_availability(BRANCH, COMPANY, ["Fish"])["items"]["Fish"]
                self.assertEqual((staff["reason_code"], staff["source"]), ("SOLD_OUT_MANUAL", "MANUAL"))
                guest = self.guest.get_customer_menu_availability("valid-session")["Fish"]
                self.assertEqual(guest, dict(item_code="Fish", sellable=False, available_qty=0,
                                             reason_code="SOLD_OUT_MANUAL"))
        self.resolver.assert_not_called()

    def test_count_caps_native_and_production_without_overriding_production_block(self):
        self.row.status = "Available"
        self.assertEqual(self.api.get_item_availability("Fish", BRANCH, COMPANY)["available_qty"], 3)
        self.enable_reservations()
        self.assertEqual(self.api.get_item_availability("Fish", BRANCH, COMPANY)["reason_code"], "CONFIGURATION_ERROR")
        self.resolver.return_value = {"production_policy": "DIRECT_RETAIL", "department": "D1", "warehouse": "W1"}
        self.api._fill_direct_retail = lambda response, *args: response.update(sellable=True, available_qty=9, reason_code="AVAILABLE")
        response = self.api.get_item_availability("Fish", BRANCH, COMPANY)
        self.assertEqual((response["available_qty"], response["source"]), (3, "COUNT_DOWN"))
        self.row.remaining_qty = 0
        self.assertEqual(self.api.get_item_availability("Fish", BRANCH, COMPANY)["reason_code"], "COUNT_EXHAUSTED")

    def test_guest_cannot_order_manual_sold_out_before_reservation_in_both_tiers(self):
        self.frappe.get_doc.return_value = Doc(branch=BRANCH, enabled=1, default_customer="Walk in",
                                              allow_add_to_running_table=1, pos_profile="PROFILE")
        session = self.session.return_value
        session.device = None
        session.source = "QR"
        invoice = SimpleNamespace(name="INV-1", branch=BRANCH, company=COMPANY, restaurant_table="T1", customer="Walk in",
                                  items=[], selling_price_list="Lunch")
        self.guest._resolve_or_create_pos_invoice.return_value = (invoice, "INV-1")
        class ReachedPricing(Exception):
            pass
        self.guest.price_items_for_invoice.side_effect = ReachedPricing
        for tier in (1, 2):
            with self.subTest(tier=tier):
                if tier == 2:
                    self.enable_reservations()
                try:
                    with self.assertRaisesRegex(self.frappe.ValidationError, r"\[AVAIL-SOLD-OUT\].*Fish"):
                        self.guest.add_customer_items("valid-session", FISH)
                except ReachedPricing:
                    self.fail("A sold-out dish reached guest pricing")
                self.guest.reconcile_order_reservations.assert_not_called()

    def test_floor_roles_can_only_mark_sold_out_and_managers_can_restore_and_set_count(self):
        action = self.require("set_item_availability")
        self.frappe.session.user = "operator@example.test"
        self.frappe.db.exists.return_value = True
        self.frappe.get_doc.return_value = self.row
        self.row.save = Mock()
        self.api._branch_menu_items = Mock(return_value=[{"item": "Fish", "name": "Fish"}])
        for role in ("URY Cashier", "URY Waiter", "URY Captain", "Kitchen User", "Kitchen Cook"):
            with self.subTest(role=role):
                self.frappe.get_roles.return_value = [role]
                for args in ({"status": "Available"}, {"count": 2}, {"reset_policy": "Never"}):
                    with self.assertRaisesRegex(self.frappe.PermissionError, r"\[AVAIL-ROLE\]"):
                        action(BRANCH, "Fish", **args)
                self.row.status = "Available"
                action(BRANCH, "Fish", status="Sold Out")
                self.assertEqual(self.row.status, "Sold Out")
        for role in ("URY Manager", "URY Admin", "System Manager"):
            self.frappe.get_roles.return_value = [role]
            result = action(BRANCH, "Fish", status="Available", count=2)
            self.assertEqual((self.row.status, self.row.remaining_qty, result["available_qty"]), ("Available", 2, 2))
        self.frappe.publish_realtime.assert_called_with(
            "menu_availability_update_B1", {"affected_items": ["Fish"], "branch": BRANCH, "source": "MANUAL"},
            after_commit=True,
        )
        self.frappe.get_roles.return_value = ["URY Cashier"]
        self.frappe.db.exists.return_value = False
        with self.assertRaisesRegex(self.frappe.PermissionError, r"\[AVAIL-SCOPE\]"):
            action(BRANCH, "Fish", status="Sold Out")

    def sql_db(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "test.sqlite"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("""CREATE TABLE `tabURY Item Availability` (
                name TEXT, branch TEXT, item TEXT, status TEXT, countdown_enabled INT,
                remaining_qty INT, daily_limit INT, auto_reset_on_open INT,
                last_reset_on TEXT, count_started_at TEXT, updated_at TEXT, updated_by TEXT, reason TEXT,
                UNIQUE (branch, item))""")
            conn.execute("INSERT INTO `tabURY Item Availability` VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("override", BRANCH, "Fish", "Available", 1, 1, 3, 1, None, None, None, None, None))

        self.frappe.db._cursor = local()

        def sql(query, values=(), as_dict=False):
            # SQLite serializes writers; the conditional UPDATE is still real, not mocked.
            query = query.replace("%s", "?").replace("FOR UPDATE", "")
            values = tuple(v.isoformat() if isinstance(v, date) else v for v in values)
            with closing(sqlite3.connect(path, timeout=8)) as conn, conn:
                conn.create_function("LEAST", 2, min)
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, values)
                if as_dict:
                    return [Doc(dict(r)) for r in cursor.fetchall()]
                self.frappe.db._cursor.rowcount = cursor.rowcount
                return cursor.fetchall()

        self.frappe.db.sql.side_effect = sql
        return sql

    def test_two_tills_cannot_both_sell_the_last_portion(self):
        apply = self.require("apply_count_delta")
        sql = self.sql_db()
        self.row.status = "Available"
        self.row.remaining_qty = 1
        barrier = Barrier(2)

        def send():
            barrier.wait(timeout=5)
            try:
                apply(BRANCH, [], FISH)
            except self.frappe.ValidationError as error:
                self.assertIn("[AVAIL-COUNT-EXHAUSTED]", str(error))
                return "blocked"
            return "accepted"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(lambda _: send(), range(2)), ["accepted", "blocked"])
        self.assertEqual(sql("SELECT remaining_qty FROM `tabURY Item Availability`")[0][0], 0)
        self.assertTrue(all(call.kwargs.get("after_commit") for call in self.frappe.publish_realtime.call_args_list))

    def test_void_before_prep_restores_count_and_prepared_void_does_not(self):
        apply = self.require("apply_count_delta")
        sql = self.sql_db()
        apply(BRANCH, FISH, [], prepared={"Fish": 1})
        self.assertEqual(sql("SELECT remaining_qty FROM `tabURY Item Availability`")[0][0], 1)
        apply(BRANCH, FISH, [])
        self.assertEqual(sql("SELECT remaining_qty FROM `tabURY Item Availability`")[0][0], 2)

    def test_split_from_an_older_count_does_not_refill_the_new_count(self):
        apply = self.require("apply_count_delta")
        sql = self.sql_db()
        self.row.count_started_at = datetime(2026, 11, 22, 9)
        split = SimpleNamespace(name="INV-CHILD", creation=datetime(2026, 11, 22, 10), custom_split_from="INV-ROOT")
        self.frappe.db.get_value.side_effect = lambda dt, *args, **kw: (
            Doc(creation=datetime(2026, 11, 22, 8), custom_split_from=None)
            if dt == "POS Invoice" else self.get_value(dt, *args, **kw))
        apply(BRANCH, FISH, [], invoice=split, prepared=())
        self.assertEqual(sql("SELECT remaining_qty FROM `tabURY Item Availability`")[0][0], 1)

    def test_first_opening_resets_but_second_opening_does_not(self):
        reset = self.require("reset_branch_availability")
        sql = self.sql_db()
        sql("UPDATE `tabURY Item Availability` SET status = 'Sold Out'")
        reset(BRANCH, date(2026, 11, 22))
        sql("UPDATE `tabURY Item Availability` SET remaining_qty = 2")
        reset(BRANCH, date(2026, 11, 22))
        self.assertEqual(tuple(sql("SELECT status, remaining_qty FROM `tabURY Item Availability`")[0]), ("Available", 2))
        reset(BRANCH, date(2026, 11, 23))
        self.assertEqual(tuple(sql("SELECT status, remaining_qty FROM `tabURY Item Availability`")[0]), ("Available", 3))

    def test_pos_sync_and_guest_gate_run_outside_the_tier_two_gate(self):
        for path, method in (("doctype/ury_order/ury_order.py", "sync_order"),
                             ("api/self_ordering.py", "add_customer_items")):
            with self.subTest(method=method):
                root = ast.parse((APP / path).read_text())
                function = next(node for node in root.body if isinstance(node, ast.FunctionDef) and node.name == method)
                body = function.body if method == "sync_order" else [s for w in function.body
                                                                    if isinstance(w, ast.With) for s in w.body]
                calls = [s.value.func.id for s in body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                         and isinstance(s.value.func, ast.Name)]
                self.assertIn("apply_count_delta", calls, f"{method} is not enforcing operational availability in both tiers")

    def test_transactional_restore_and_first_opening_are_registered(self):
        for path, method in (("doctype/ury_order/ury_order.py", "cancel_order"),
                             ("api/ury_pos_invoice_qty_reduction.py", "reduce_order_item_qty")):
            source = (APP / path).read_text()
            fn = next((n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == method), None)
            self.assertIsNotNone(fn, method)
            self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                and n.func.id == "apply_count_delta" for n in ast.walk(fn)), method)
        hook = (APP.parent / "hooks.py").read_text()
        self.assertIn('"on_submit": "ury.ury.hooks.ury_pos_opening_entry.reset_item_availability_on_open"', hook)


class TestStaffAvailability(OrderingCases, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.framework.session.user = "waiter@example.com"
        self.invoice.modified = "2026-10-16 12:00:00"

    sync = test_ury_hold_fire.HoldFireTests.sync

    def test_pos_rejects_manual_sold_out_before_saving_in_both_tiers(self):
        self.framework.db.get_values.return_value = [
            Doc(item="Fish", status="Sold Out", countdown_enabled=0),
        ]
        for enabled in (False, True):
            with self.subTest(reservation_control_enabled=enabled), patch.object(
                self.order, "get_branch_stock_policy", return_value=SimpleNamespace(reservation_control_enabled=enabled),
            ), patch.object(self.order, "reconcile_order_reservations") as reserve:
                with self.assertRaisesRegex(ValueError, r"\[AVAIL-SOLD-OUT\]"):
                    self.sync([{"item": "Fish", "item_name": "Fish", "qty": 1, "rate": 100}])
                self.invoice.save.assert_not_called()
                reserve.assert_not_called()

    def test_no_availability_row_causes_no_count_writes(self):
        self.framework.db.get_values.return_value = []
        self.sync([{"item": "Fish", "item_name": "Fish", "qty": 1, "rate": 100}])
        self.invoice.save.assert_called_once()
        self.assertFalse(any("tabURY Item Availability" in str(call)
                             for call in self.framework.db.sql.call_args_list))
