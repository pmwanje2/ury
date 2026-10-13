"""Void-reason regression tests; database and table/KOT writes are isolated."""

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

# Import ERPNext without requiring a bench log directory, as in the customer tests.
with patch("frappe.logger", return_value=logging.getLogger(__name__)):
    from ury.ury.doctype.ury_order import ury_order


class TestCancelOrderReason(unittest.TestCase):
    def setUp(self):
        self.invoice = MagicMock()
        self.invoice.name = "POS-INV-001"
        self.invoice.branch = "Test Branch"
        self.invoice.restaurant_table = "T1"
        self.invoice.docstatus = 0
        self.framework = MagicMock()
        self.framework.PermissionError = frappe.PermissionError
        self.framework.get_doc.return_value = self.invoice
        self.framework.has_permission.return_value = True
        self.framework.session.user = "manager@example.com"
        self.framework.throw.side_effect = self.throw
        self.set_cancel_reason_field(length=0)
        self.release = MagicMock()
        self.kot = MagicMock()
        for target, replacement in (
            ("frappe", self.framework),
            ("_", lambda message: message),
            ("getBranch", lambda: "Test Branch"),
            ("release_merge_cluster_tables", self.release),
            ("cancel_kot", self.kot),
        ):
            self.enterContext(patch.object(ury_order, target, replacement))


    def set_cancel_reason_field(self, length=0, missing=False, broken=False):
        self.framework.get_meta.side_effect = None
        if broken:
            self.framework.get_meta.side_effect = RuntimeError("meta unavailable")
            return
        field = None if missing else SimpleNamespace(length=length)
        meta = MagicMock()

        def get_field(name):
            if name != "cancel_reason":
                return None
            return field

        meta.get_field.side_effect = get_field
        self.framework.get_meta.return_value = meta

    @staticmethod
    def throw(message, exception=frappe.ValidationError, **kwargs):
        raise exception(message)

    def assert_no_mutation(self):
        self.release.assert_not_called()
        self.kot.assert_not_called()
        self.assertEqual(self.invoice.method_calls, [])
        self.assertEqual(self.framework.db.method_calls, [])

    def assert_invalid_reasons(self, reasons):
        for reason in reasons:
            with self.subTest(reason=reason):
                self.release.reset_mock()
                self.kot.reset_mock()
                self.invoice.reset_mock()
                self.framework.db.reset_mock()
                try:
                    with self.assertRaisesRegex(frappe.ValidationError, "at least 4 characters"):
                        ury_order.cancel_order(self.invoice.name, reason)
                finally:
                    # Even a thrown error is too late if the release already committed.
                    self.assert_no_mutation()

    def test_blank_reason_is_refused_before_release(self):
        self.assert_invalid_reasons(("", " ", "\t\r\n"))

    def test_short_trimmed_reason_is_refused_before_release(self):
        self.assert_invalid_reasons(("a", "ab", "abc", "  abc \t"))

    def test_non_string_reason_is_refused_before_release(self):
        self.assert_invalid_reasons((None, 1234, False, [], {}, b"Wrong order"))

    def test_submitted_invoice_also_requires_a_reason_before_release(self):
        self.invoice.docstatus = 1
        self.assert_invalid_reasons(("", "  abc  ", None))

    def test_walk_in_invoice_also_requires_a_reason(self):
        self.invoice.restaurant_table = None
        self.assert_invalid_reasons(("abc",))

    def test_four_trimmed_characters_are_accepted_and_stored(self):
        for docstatus in (0, 1):
            with self.subTest(docstatus=docstatus):
                self.invoice.docstatus = docstatus
                self.invoice.reset_mock()
                self.release.reset_mock()
                ury_order.cancel_order(self.invoice.name, " \tVoid\n ")
                self.invoice.db_set.assert_called_with("cancel_reason", "Void")
                self.release.assert_called_once_with("T1")

    def test_cancel_permission_denial_precedes_reason_validation(self):
        self.framework.has_permission.return_value = False
        with self.assertRaises(frappe.PermissionError):
            ury_order.cancel_order(self.invoice.name, "x")
        self.framework.has_permission.assert_called_once_with("POS Invoice", "cancel", doc=self.invoice)
        self.assert_no_mutation()

    def test_branch_denial_precedes_reason_validation(self):
        self.invoice.branch = "Other Branch"
        with self.assertRaises(frappe.PermissionError):
            ury_order.cancel_order(self.invoice.name, "x")
        self.assert_no_mutation()

    def test_reason_longer_than_the_field_is_refused_before_any_side_effect(self):
        cases = (
            {"length": 12, "reason": "x" * 13, "limit": 12},
            {"length": 0, "reason": "x" * 141, "limit": 140},
            {"missing": True, "reason": "x" * 141, "limit": 140},
            {"broken": True, "reason": "x" * 141, "limit": 140},
            {"length": 140, "reason": "  " + ("x" * 141) + "\n", "limit": 140},
            {"length": 0, "reason": "x" * 141, "limit": 140, "docstatus": 1},
            {"length": 0, "reason": "x" * 141, "limit": 140, "table": None},
        )
        for case in cases:
            with self.subTest(**case):
                self.release.reset_mock()
                self.kot.reset_mock()
                self.invoice.reset_mock()
                self.framework.db.reset_mock()
                self.invoice.docstatus = case.get("docstatus", 0)
                self.invoice.restaurant_table = case.get("table", "T1")
                self.set_cancel_reason_field(
                    length=case.get("length", 0),
                    missing=case.get("missing", False),
                    broken=case.get("broken", False),
                )
                try:
                    with self.assertRaisesRegex(
                        frappe.ValidationError,
                        f"cannot be longer than {case['limit']} characters",
                    ):
                        ury_order.cancel_order(self.invoice.name, case["reason"])
                finally:
                    self.assert_no_mutation()

    def test_reason_at_the_field_length_is_accepted_and_stored(self):
        cases = (
            {"length": 12, "reason": "  " + ("y" * 12) + "\t", "stored": "y" * 12},
            {"length": 0, "reason": "z" * 140, "stored": "z" * 140},
            {"missing": True, "reason": "\n" + ("z" * 140) + " ", "stored": "z" * 140},
        )
        for case in cases:
            with self.subTest(**case):
                self.invoice.docstatus = 0
                self.invoice.restaurant_table = "T1"
                self.invoice.reset_mock()
                self.release.reset_mock()
                self.kot.reset_mock()
                self.set_cancel_reason_field(
                    length=case.get("length", 0),
                    missing=case.get("missing", False),
                )
                ury_order.cancel_order(self.invoice.name, case["reason"])
                self.invoice.db_set.assert_called_with("cancel_reason", case["stored"])
                self.release.assert_called_once_with("T1")
                self.framework.get_meta.assert_called_with("POS Invoice")

