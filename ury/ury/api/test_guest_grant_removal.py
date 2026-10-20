# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# See license.txt

import json
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ury.ury.api import self_ordering


MOD = "ury.ury.api.self_ordering"
APP_ROOT = Path(__file__).resolve().parents[1]


class TestDuplicateGuestGrantLayerRemoved(unittest.TestCase):
    def setUp(self):
        frappe.local.flags = frappe._dict(mute_messages=True, in_test=True)
        frappe.local.session = frappe._dict(user="Guest", data=frappe._dict())
        frappe.local.message_log = []
        frappe.local.error_log = []
        frappe.local.conf = frappe._dict(db_name="unit")
        frappe.local.form_dict = frappe._dict()
        frappe.local.db = MagicMock(name="db")

    def test_staff_grant_and_grant_bootstrap_seams_are_gone(self):
        for name in (
            "issue_table_grant",
            "can_issue_table_grant",
            "get_granted_ordering_context",
            "_require_table_grant",
            "_grantable_invoice",
            "_grant_fingerprint",
            "_grant_row",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(self_ordering, name))

    def test_grant_fields_and_search_indexes_are_gone_from_native_contract(self):
        session_path = APP_ROOT / "doctype/ury_ordering_session/ury_ordering_session.json"
        profile_path = APP_ROOT / "doctype/ury_self_ordering_profile/ury_self_ordering_profile.json"
        session = json.loads(session_path.read_text())
        profile = json.loads(profile_path.read_text())

        grant_session_fields = {"grant_fingerprint", "granted_by", "is_granted"}
        session_fields = {field["fieldname"] for field in session["fields"]}
        self.assertTrue(grant_session_fields.isdisjoint(session_fields))
        self.assertTrue(grant_session_fields.isdisjoint(session["field_order"]))
        invoice = next(field for field in session["fields"] if field["fieldname"] == "invoice")
        self.assertNotEqual(invoice.get("search_index"), 1)

        profile_fields = {field["fieldname"] for field in profile["fields"]}
        self.assertNotIn("require_guest_invoice_grant", profile_fields)
        self.assertNotIn("require_guest_invoice_grant", profile["field_order"])

    def test_native_qr_table_session_keeps_legacy_shape(self):
        profile = SimpleNamespace(name="Profile A", session_idle_timeout_minutes=30)
        session = MagicMock(name="session")
        with (
            patch(f"{MOD}.frappe.db.commit") as mock_commit,
            patch(f"{MOD}.frappe.get_doc", return_value=session) as mock_get_doc,
            patch(f"{MOD}.frappe.generate_hash", return_value="raw-table-session"),
            patch(f"{MOD}.frappe.set_user"),
            patch(f"{MOD}.now_datetime", return_value=datetime(2026, 10, 20, 12)),
            patch(f"{MOD}.add_to_date", return_value="expiry"),
        ):
            token, opened = self_ordering._open_session(profile, "QR Table", "Table 7")

        self.assertEqual(token, "raw-table-session")
        self.assertIs(opened, session)
        payload = mock_get_doc.call_args.args[0]
        self.assertEqual(payload["source"], "QR Table")
        self.assertEqual(payload["table"], "Table 7")
        for field in ("is_granted", "grant_fingerprint", "granted_by"):
            self.assertNotIn(field, payload)
        session.insert.assert_called_once_with(ignore_permissions=True)
        mock_commit.assert_called_once_with()

    def test_legacy_table_session_still_slides_on_guest_activity(self):
        session = MagicMock(
            name="SESSION-1",
            ordering_profile="Profile A",
            status="Active",
            expires_at=datetime(2099, 1, 1),
            table="Table 7",
            invoice=None,
        )
        profile = SimpleNamespace(
            name="Profile A",
            enabled=1,
            session_idle_timeout_minutes=30,
        )
        def get_doc(doctype, name=None, **kwargs):
            return session if doctype == "URY Ordering Session" else profile

        with (
            patch(f"{MOD}.frappe.get_doc", side_effect=get_doc),
            patch(f"{MOD}.frappe.db.get_value", return_value="SESSION-1"),
            patch(f"{MOD}.frappe.set_user"),
            patch(f"{MOD}.now_datetime", return_value=datetime(2026, 10, 20, 12)),
            patch(f"{MOD}.add_to_date", return_value="slid-expiry"),
            patch(f"{MOD}.get_datetime", return_value=datetime(2099, 1, 1)),
        ):
            result = self_ordering._resolve_session("raw-table-session")

        self.assertIs(result, session)
        self.assertEqual(session.expires_at, "slid-expiry")
        session.save.assert_called_once_with(ignore_permissions=True)

