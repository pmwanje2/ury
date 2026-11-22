"""Guest session reads do not write-lock the session row; site-less (no bench, no database).

Live: get_customer_product -> _resolve_session -> session.save() -> check_if_latest ran
SELECT ... FOR UPDATE on `tabURY Ordering Session` and raised QueryDeadlockError 1020
(MariaDB 11.8 innodb_snapshot_isolation=1) while add_customer_items touched the same
session. A read must not fail because another request extended the session.
"""

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

APP = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 20, 12, 0, 0)
IDLE_MINUTES = 30
TOKEN = "t" * 64


class Doc(dict):
    __getattr__ = dict.get

    def __setattr__(self, key, value):
        self[key] = value


def _site_less_frappe():
    """The modules self_ordering imports at load time. No bench and no frappe."""
    framework = MagicMock()
    for name in ("PermissionError", "ValidationError", "QueryDeadlockError", "QueryTimeoutError"):
        setattr(framework, name, type(name, (Exception,), {}))
    framework._ = lambda *messages: messages[0]
    framework.whitelist = lambda *args, **kwargs: (lambda function: function)

    def throw(message, exception=None, **kwargs):
        raise (exception or framework.ValidationError)(message)

    framework.throw = throw
    utils = ModuleType("frappe.utils")
    utils.add_to_date = lambda value, minutes=0, **kwargs: value + timedelta(minutes=minutes)
    utils.cint = lambda value: int(value or 0)
    utils.flt = lambda value: float(value or 0)
    utils.get_datetime = lambda value: value
    utils.now_datetime = lambda: NOW
    rate_limiter = ModuleType("frappe.rate_limiter")
    rate_limiter.rate_limit = lambda **kwargs: (lambda function: function)
    pos_api = ModuleType("ury.ury_pos.api")
    pos_api.resolve_restaurant_menu = MagicMock()
    order = ModuleType("ury.ury.doctype.ury_order.ury_order")
    for name in ("_kitchen_snapshot", "_resolve_or_create_pos_invoice", "_ensure_invoice_reservation_ref", "get_menu_name",
                 "get_restaurant_and_menu_name", "invoice_pricing_date", "price_items_for_invoice"):
        setattr(order, name, MagicMock())
    kot = ModuleType("ury.ury.api.ury_kot_generate")
    kot._execute_kot = MagicMock()
    item_price = ModuleType("ury.ury.api.item_price")
    item_price.item_price_release_states = MagicMock()
    item_price.price_valid_on = MagicMock()
    reservations = ModuleType("ury.ury.api.ury_order_reservation_service")
    reservations.reconcile_order_reservations = MagicMock()
    stock_policy = ModuleType("ury.ury.api.ury_stock_policy")
    stock_policy.get_branch_stock_policy = MagicMock()
    availability = ModuleType("ury.ury.api.ury_availability")
    availability._batch_availability = MagicMock()
    availability._catalog_gate = MagicMock()
    return framework, {
        "frappe": framework, "frappe.utils": utils, "frappe.rate_limiter": rate_limiter,
        "ury.ury_pos.api": pos_api, "ury.ury.doctype.ury_order.ury_order": order,
        "ury.ury.api.ury_kot_generate": kot, "ury.ury.api.item_price": item_price,
        "ury.ury.api.ury_order_reservation_service": reservations,
        "ury.ury.api.ury_stock_policy": stock_policy,
        "ury.ury.api.ury_availability": availability,
    }


def _load_self_ordering(modules):
    # A private module name, so a bench-loaded self_ordering is never reused.
    name = "_site_less_self_ordering"
    spec = importlib.util.spec_from_file_location(name, APP / "ury/api/self_ordering.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {**modules, name: module}):
        spec.loader.exec_module(module)
    return module


class TestGuestSessionReads(unittest.TestCase):
    def setUp(self):
        self.frappe, modules = _site_less_frappe()
        self.api = _load_self_ordering(modules)
        self.now = NOW
        self.enterContext(patch.object(self.api, "now_datetime", side_effect=lambda: self.now))
        self.enterContext(patch.object(self.api, "_elevated", MagicMock()))
        self.stored = {"name": "S1", "ordering_profile": "P1", "status": "Active",
                       "last_activity": NOW - timedelta(minutes=5), "expires_at": NOW + timedelta(minutes=25)}
        self.loaded = []
        self.idle_minutes = IDLE_MINUTES
        self.frappe.get_doc.side_effect = self.get_doc
        self.frappe.db.get_value.side_effect = self.get_value
        self.frappe.db.set_value.side_effect = self.set_value

    def get_doc(self, doctype, name=None, **kwargs):
        self.assertEqual((doctype, name), ("URY Ordering Session", "S1"))
        doc = Doc(self.stored)
        doc.save = MagicMock(name="session.save")
        self.loaded.append(doc)
        return doc

    def get_value(self, doctype, filters=None, fieldname=None, *args, **kwargs):
        if doctype == "URY Ordering Session":
            return "S1" if self.stored["status"] == "Active" else None
        self.assertEqual((doctype, filters), ("URY Self Ordering Profile", "P1"))
        return {"enabled": 1, "session_idle_timeout_minutes": self.idle_minutes}[fieldname]

    def set_value(self, doctype, name, values, *args, **kwargs):
        self.stored.update(values)

    def saves(self):
        return sum(doc.save.call_count for doc in self.loaded)

    def test_second_resolve_within_a_minute_writes_nothing(self):
        self.api._resolve_session(TOKEN)
        self.frappe.db.set_value.reset_mock()
        self.loaded.clear()
        self.now = NOW + timedelta(seconds=30)
        session = self.api._resolve_session(TOKEN)
        self.assertEqual(session.name, "S1")
        self.frappe.db.set_value.assert_not_called()
        self.assertEqual(self.saves(), 0)

    def test_resolve_after_a_minute_slides_expiry_with_one_column_update(self):
        self.stored["last_activity"] = NOW - timedelta(seconds=61)
        session = self.api._resolve_session(TOKEN)
        self.assertEqual(session.name, "S1")
        self.frappe.db.set_value.assert_called_once_with(
            "URY Ordering Session", "S1",
            {"last_activity": NOW, "expires_at": NOW + timedelta(minutes=IDLE_MINUTES)},
            update_modified=False,
        )
        self.assertEqual(self.saves(), 0)
        savepoint = self.frappe.db.savepoint.call_args.args[0]
        self.frappe.db.release_savepoint.assert_called_once_with(savepoint)
        self.frappe.db.rollback.assert_not_called()

    def conflict_on_slide(self, error):
        self.stored["last_activity"] = NOW - timedelta(minutes=2)
        self.frappe.db.set_value.side_effect = error(1020, "Record has changed since last read")
        original_get_doc = self.get_doc

        def get_doc_whose_save_conflicts(doctype, name=None, **kwargs):
            doc = original_get_doc(doctype, name, **kwargs)
            doc.save.side_effect = error(1020, "Record has changed since last read")
            return doc

        self.frappe.get_doc.side_effect = get_doc_whose_save_conflicts

    def test_deadlock_on_the_slide_rolls_back_the_whole_transaction_and_still_reads(self):
        # MariaDB aborts the whole transaction on 1020 (ER_CHECKREAD) and 1213, so
        # the savepoint is gone: ROLLBACK TO SAVEPOINT would fail with 1305.
        def rollback(*args, save_point=None, **kwargs):
            if save_point is not None:
                raise RuntimeError(f"(1305, 'SAVEPOINT {save_point} does not exist')")

        self.frappe.db.rollback.side_effect = rollback
        self.conflict_on_slide(self.frappe.QueryDeadlockError)
        session = self.api._resolve_session(TOKEN)
        self.assertEqual(session.name, "S1")
        self.frappe.db.rollback.assert_called_once_with()
        self.frappe.db.release_savepoint.assert_not_called()

    def test_lock_wait_timeout_on_the_slide_rolls_back_to_the_savepoint_and_still_reads(self):
        # 1205 rolls back only the statement, so the savepoint survives.
        self.conflict_on_slide(self.frappe.QueryTimeoutError)
        session = self.api._resolve_session(TOKEN)
        self.assertEqual(session.name, "S1")
        savepoint = self.frappe.db.savepoint.call_args.args[0]
        self.frappe.db.rollback.assert_called_once_with(save_point=savepoint)

    def test_a_skipped_slide_is_logged_without_an_error_log(self):
        # The swallowed conflict stays observable in the app log, not as Error Log rows.
        for error in (self.frappe.QueryDeadlockError, self.frappe.QueryTimeoutError):
            with self.subTest(error=error.__name__):
                self.frappe.logger.reset_mock()
                self.frappe.log_error.reset_mock()
                self.conflict_on_slide(error)
                self.api._resolve_session(TOKEN)
                self.frappe.logger.assert_called_once_with("ury")
                warning = self.frappe.logger.return_value.warning
                warning.assert_called_once()
                message = warning.call_args.args[0] % warning.call_args.args[1:]
                self.assertIn(error.__name__, message)
                self.assertIn("S1", message)
                self.assertNotIn(TOKEN, message)
                self.frappe.log_error.assert_not_called()

    def test_a_slide_that_succeeds_logs_nothing(self):
        self.stored["last_activity"] = NOW - timedelta(minutes=2)
        self.api._resolve_session(TOKEN)
        self.frappe.logger.assert_not_called()

    def test_short_idle_timeout_slides_before_half_of_it_has_passed(self):
        # A 1-minute timeout polled every 50 s must not expire while the guest is active.
        self.idle_minutes = 1
        self.stored["last_activity"] = NOW - timedelta(seconds=50)
        self.stored["expires_at"] = NOW + timedelta(seconds=10)
        self.api._resolve_session(TOKEN)
        self.frappe.db.set_value.assert_called_once_with(
            "URY Ordering Session", "S1",
            {"last_activity": NOW, "expires_at": NOW + timedelta(minutes=1)},
            update_modified=False,
        )

    def test_short_idle_timeout_skips_the_write_within_half_of_it(self):
        self.idle_minutes = 1
        self.stored["last_activity"] = NOW - timedelta(seconds=20)
        self.stored["expires_at"] = NOW + timedelta(seconds=40)
        self.api._resolve_session(TOKEN)
        self.frappe.db.set_value.assert_not_called()

    def test_other_errors_on_the_slide_still_fail(self):
        self.stored["last_activity"] = NOW - timedelta(minutes=2)
        self.frappe.db.set_value.side_effect = RuntimeError("not a lock conflict")
        with self.assertRaises(RuntimeError):
            self.api._resolve_session(TOKEN)

    def refusal(self, token=TOKEN):
        with self.assertRaises(self.frappe.PermissionError) as caught:
            self.api._resolve_session(token)
        return type(caught.exception).__name__, str(caught.exception)

    def assert_no_session_write(self):
        self.assertEqual(self.saves(), 0)
        self.frappe.db.set_value.assert_not_called()
        self.frappe.db.savepoint.assert_not_called()

    def test_expired_session_is_refused_without_writing_the_session_row(self):
        # The request rolls back on the refusal, so an "Expired" write never persists; it can only clash.
        for expires_at in (NOW - timedelta(seconds=1), None):
            with self.subTest(expires_at=expires_at):
                self.stored["expires_at"] = expires_at
                self.assertEqual(self.refusal()[0], "URYGuestSessionInvalid")
                self.assert_no_session_write()

    def test_expiry_refusal_survives_a_concurrent_writer_on_the_session_row(self):
        # add_customer_items changed the row: a FOR UPDATE re-read raises 1020 under snapshot isolation.
        self.conflict_on_slide(self.frappe.QueryDeadlockError)
        self.stored["expires_at"] = NOW - timedelta(seconds=1)
        self.assertEqual(self.refusal()[0], "URYGuestSessionInvalid")
        self.assert_no_session_write()

    def test_every_unusable_token_gets_one_refusal(self):
        # Enumeration-safe: an expired real token reads exactly like an unknown one.
        refusals = {"too long": self.refusal("x" * 513)}
        self.stored["status"] = "Closed"
        refusals["unknown or closed"] = self.refusal()
        self.stored["status"] = "Active"
        self.stored["expires_at"] = NOW - timedelta(seconds=1)
        refusals["expired"] = self.refusal()
        self.stored["expires_at"] = None
        refusals["no expiry"] = self.refusal()
        self.stored["expires_at"] = NOW + timedelta(minutes=5)
        self.frappe.db.get_value.side_effect = lambda doctype, filters=None, fieldname=None, *a, **k: (
            "S1" if doctype == "URY Ordering Session" else 0)
        refusals["profile disabled"] = self.refusal()
        self.assertEqual(set(refusals.values()), {(
            "URYGuestSessionInvalid", "Ordering session expired or invalid. Please rescan the QR code.",
        )}, refusals)

    def test_token_and_profile_checks_are_unchanged(self):
        with self.assertRaises(self.frappe.PermissionError):
            self.api._resolve_session("")
        with self.assertRaises(self.frappe.PermissionError):
            self.api._resolve_session("x" * 513)
        self.stored["status"] = "Closed"
        with self.assertRaises(self.frappe.PermissionError):
            self.api._resolve_session(TOKEN)
        self.stored["status"] = "Active"
        self.frappe.db.get_value.side_effect = lambda doctype, filters=None, fieldname=None, *a, **k: (
            "S1" if doctype == "URY Ordering Session" else 0)
        with self.assertRaises(self.frappe.PermissionError):
            self.api._resolve_session(TOKEN)
        self.frappe.db.set_value.assert_not_called()


if __name__ == "__main__":
    unittest.main()
