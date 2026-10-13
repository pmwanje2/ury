# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and Contributors
# See license.txt

"""Resolution audit is filled on save, including the native workflow path."""

import datetime
import unittest
from types import SimpleNamespace

from ury.ury.doctype.ury_service_request.ury_service_request import URYServiceRequest


class TestURYServiceRequest(unittest.TestCase):
    def setUp(self):
        from ury.ury.doctype.ury_service_request import ury_service_request as module

        self.module = module
        self.user = "waiter1@salama.test"
        self.when = datetime.datetime(2026, 10, 13, 13, 49, 27)
        self.had_frappe = hasattr(module, "frappe")
        self.original_frappe = getattr(module, "frappe", None)
        module.frappe = SimpleNamespace(
            session=SimpleNamespace(user=self.user),
            utils=SimpleNamespace(now_datetime=lambda: self.when),
        )

    def tearDown(self):
        if self.had_frappe:
            self.module.frappe = self.original_frappe
        elif hasattr(self.module, "frappe"):
            del self.module.frappe

    def request(self, status, previous_status, resolved_by=None, resolved_at=None):
        previous = None if previous_status is None else {"status": previous_status}
        return SimpleNamespace(
            status=status,
            resolved_by=resolved_by,
            resolved_at=resolved_at,
            get_doc_before_save=lambda: previous,
        )

    def save(self, doc):
        for hook_name in ("validate", "before_save"):
            hook = getattr(URYServiceRequest, hook_name, None)
            if hook is not None:
                hook(doc)

    def test_becoming_resolved_records_the_session_user_and_time(self):
        cases = (
            ("Open", None),
            ("Acknowledged", None),
            (None, None),
            ("Acknowledged", ""),
        )
        for previous, resolved_by in cases:
            with self.subTest(previous=previous, resolved_by=resolved_by):
                doc = self.request("Resolved", previous, resolved_by=resolved_by)
                self.save(doc)
                self.assertEqual(doc.resolved_by, self.user)
                self.assertEqual(doc.resolved_at, self.when)

    def test_acknowledge_does_not_record_a_resolver(self):
        doc = self.request("Acknowledged", "Open")
        self.save(doc)
        self.assertIsNone(doc.resolved_by)
        self.assertIsNone(doc.resolved_at)

    def test_existing_resolver_is_kept(self):
        original_at = datetime.datetime(2026, 10, 13, 9, 0, 0)
        doc = self.request(
            "Resolved",
            "Acknowledged",
            resolved_by="captain@example.com",
            resolved_at=original_at,
        )
        self.save(doc)
        self.assertEqual(doc.resolved_by, "captain@example.com")
        self.assertEqual(doc.resolved_at, original_at)

    def test_already_resolved_blank_audit_is_not_backfilled(self):
        doc = self.request("Resolved", "Resolved")
        self.save(doc)
        self.assertIsNone(doc.resolved_by)
        self.assertIsNone(doc.resolved_at)

    def test_existing_resolver_does_not_leave_empty_timestamp(self):
        doc = self.request(
            "Resolved",
            "Acknowledged",
            resolved_by="captain@example.com",
            resolved_at=None,
        )
        self.save(doc)
        self.assertEqual(doc.resolved_by, "captain@example.com")
        self.assertEqual(doc.resolved_at, self.when)

    def test_existing_timestamp_is_not_overwritten_when_resolver_is_empty(self):
        original_at = datetime.datetime(2026, 10, 13, 9, 0, 0)
        doc = self.request(
            "Resolved",
            "Acknowledged",
            resolved_by=None,
            resolved_at=original_at,
        )
        self.save(doc)
        self.assertEqual(doc.resolved_by, self.user)
        self.assertEqual(doc.resolved_at, original_at)
