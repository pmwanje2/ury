"""Grant-independent security contracts for the native guest ordering path."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ury.ury.api import self_ordering as api


class TestGuestHardening(unittest.TestCase):
    def setUp(self):
        frappe.local.flags = frappe._dict(mute_messages=True, in_test=True)
        frappe.local.session = frappe._dict(user="Guest", data=frappe._dict())
        frappe.local.message_log = []
        frappe.local.error_log = []
        frappe.local.db = MagicMock(name="db")

    def test_bill_bearing_guest_reads_are_post_only(self):
        for name in (
            "get_customer_menu",
            "get_customer_product",
            "get_customer_order",
            "get_order_status",
            "get_payment_status",
        ):
            with self.subTest(name=name):
                method = getattr(api, name)
                self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])

    def test_guest_session_tokens_are_bounded_before_database_lookup(self):
        lookup = MagicMock(side_effect=AssertionError("oversized token reached the database"))
        with patch.object(frappe.db, "get_value", lookup):
            with self.assertRaises(frappe.PermissionError):
                api._resolve_session("x" * 513)
        lookup.assert_not_called()

    def test_native_lock_conflicts_map_to_retryable_guest_error(self):
        session = SimpleNamespace(
            name="SESSION-1", ordering_profile="Profile A", table="Table 7",
            invoice="POS-1", source="QR Table", device=None,
        )
        profile = SimpleNamespace(
            name="Profile A", enabled=1, allow_add_to_running_table=1,
            branch="Branch A", pos_profile="POS A", default_customer="Walk-in",
        )
        invoice = SimpleNamespace(
            name="POS-1", restaurant_table="Table 7", order_type="Dine In",
            customer="Walk-in", branch="Branch A", pos_profile="POS A",
            selling_price_list="Standard Selling", invoice_created=1,
            invoice_printed=0, grand_total=20, creation=datetime(2026, 10, 20, 12),
            items=[],
        )
        invoice.append = lambda _field, row: invoice.items.append(SimpleNamespace(**row))
        invoice.save = MagicMock()

        with (
            patch.object(api, "_resolve_session", return_value=session),
            patch.object(api.frappe, "get_doc", return_value=profile),
            patch.object(api.frappe.db, "get_value", return_value="Menu A"),
            patch.object(api, "resolve_restaurant_menu", return_value={"items": [{"item": "Tea"}]}),
            patch.object(api, "price_items_for_invoice", return_value=[{
                "item_code": "Tea", "item_name": "Tea", "qty": 1,
                "comment": "", "rate": 20, "amount": 20,
            }]),
            patch.object(api, "_resolve_or_create_pos_invoice", return_value=(invoice, invoice.name)),
            patch.object(api, "_ensure_admin_branch_mapping"),
        ):
            for error in (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
                with self.subTest(error=error):
                    invoice.items.clear()
                    invoice.save.reset_mock()
                    invoice.save.side_effect = error("private native lock details")
                    with self.assertRaisesRegex(frappe.ValidationError, "URY_GUEST_RETRY"):
                        api.add_customer_items("session-token", [{"item": "Tea", "qty": 1}])

    def test_share_payment_link_rejects_session_without_invoice_before_lookup(self):
        session = SimpleNamespace(invoice=None, ordering_profile="Profile A")
        profile = SimpleNamespace(enable_payment_link=1)
        lookup = MagicMock(side_effect=AssertionError("unbound payment request was queried"))
        with (
            patch.object(api, "_resolve_session", return_value=session),
            patch.object(api.frappe, "get_doc", return_value=profile),
            patch.object(api.frappe.db, "get_value", lookup),
        ):
            with self.assertRaises((frappe.PermissionError, frappe.ValidationError)):
                api.share_payment_link("session-token", "guest@example.test")
        lookup.assert_not_called()


    def _resolve_with(self, *, enabled, expires_at):
        now = datetime(2026, 10, 20, 12)
        session = frappe._dict(
            name="S1", ordering_profile="P1", status="Active", table="T7",
            invoice="POS-1", expires_at=expires_at, source="QR Table",
        )
        session.save = MagicMock()
        profile = frappe._dict(name="P1", enabled=enabled, session_idle_timeout_minutes=30)

        def get_doc(doctype, name=None, **kwargs):
            return profile if doctype == "URY Self Ordering Profile" else session

        def get_value(doctype, filters=None, fieldname=None, *args, **kwargs):
            if doctype == "URY Ordering Session":
                return "S1"
            if fieldname == "enabled":
                return profile.enabled
            return 30

        with (
            patch.object(api, "now_datetime", return_value=now),
            patch.object(api, "add_to_date", return_value=now + timedelta(minutes=30)),
            patch.object(api.frappe, "get_doc", side_effect=get_doc),
            patch.object(api.frappe.db, "get_value", side_effect=get_value),
        ):
            result = api._resolve_session("t" * 64)
        return result, session

    def test_disabled_ordering_profile_refuses_existing_guest_sessions(self):
        with self.assertRaises(frappe.PermissionError):
            self._resolve_with(enabled=0, expires_at=datetime(2026, 10, 20, 12, 10))

    def test_guest_session_without_expiry_is_refused(self):
        with self.assertRaises(frappe.PermissionError):
            self._resolve_with(enabled=1, expires_at=None)

    def test_valid_guest_session_on_enabled_profile_still_slides(self):
        result, session = self._resolve_with(enabled=1, expires_at=datetime(2026, 10, 20, 12, 10))
        self.assertIs(result, session)
        session.save.assert_called_once_with(ignore_permissions=True)
