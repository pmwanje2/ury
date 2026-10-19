# Copyright (c) 2026, Tridz Technologies Pvt. Ltd. and contributors
# See license.txt
#
# Guest feedback is native Quality Feedback. document_type can only be User
# or Customer, so the bill link is the tenant field custom_pos_invoice (S1).
# These tests fail until the guest endpoint, profile fields, and receipt
# grant exist.

import base64
import calendar
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document

from ury.ury.api import self_ordering


MOD = "ury.ury.api.self_ordering"
FIXED_NOW = datetime(2026, 10, 16, 12, 0, 0)
GRANT_LIFETIME_SECONDS = 7 * 24 * 60 * 60
SECRET = "test-secret"
RATINGS = [
    {"parameter": "Ambience", "rating": 3},
    {"parameter": "Food", "rating": 5},
    {"parameter": "Service", "rating": 4},
]


def _epoch(moment):
    return calendar.timegm(moment.utctimetuple())


def _expected_grant(profile, purpose, invoice, expiry_ts, secret=SECRET):
    payload = f"{profile}|{purpose}|{invoice}|{expiry_ts}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}|{signature}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _bind_locals():
    frappe.local.flags = frappe._dict(mute_messages=True, in_test=True)
    frappe.local.session = frappe._dict(user="Guest", data=frappe._dict())
    frappe.local.message_log = []
    frappe.local.error_log = []
    frappe.local.conf = frappe._dict(db_name="unit")
    if not getattr(frappe.local, "form_dict", None):
        frappe.local.form_dict = frappe._dict()
    if not getattr(frappe.local, "db", None):
        frappe.local.db = MagicMock(name="db")


class TestGuestFeedback(unittest.TestCase):
    def setUp(self):
        _bind_locals()
        self.api = self_ordering
        self.session = SimpleNamespace(
            name="SESSION-1",
            table="Table 7",
            invoice="POS-1",
            ordering_profile="Profile A",
        )
        self.profile = SimpleNamespace(
            name="Profile A",
            enabled=1,
            enable_feedback=1,
            feedback_template="Guest Meal",
            branch="Salama",
            pos_profile="Salama Till",
        )
        self.invoice = SimpleNamespace(
            name="POS-1",
            branch="Salama",
            pos_profile="Salama Till",
            invoice_printed=1,
            docstatus=0,
            outstanding_amount=20,
            grand_total=20,
            is_return=0,
            status="Draft",
            customer="SECRET-CUSTOMER",
            customer_name="SECRET-CUSTOMER",
            contact_mobile="0700999888",
            contact_email="secret@example.test",
            check_permission=MagicMock(),
        )
        self.template = SimpleNamespace(
            name="Guest Meal",
            parameters=[
                SimpleNamespace(parameter="Food"),
                SimpleNamespace(parameter="Service"),
                SimpleNamespace(parameter="Ambience"),
            ],
        )
        self.feedback = MagicMock()
        self.feedback.name = "QA-FB-9"
        self.invoice_loads = []
        self.template_loads = []
        self.payloads = []
        self.exists_calls = []
        self.duplicate = False
        self.meta = MagicMock()
        self.meta.has_field.side_effect = lambda field: field == "custom_pos_invoice"
        self.resolve = self.enterContext(patch(f"{MOD}._resolve_session", return_value=self.session))
        self.get_doc = self.enterContext(patch(f"{MOD}.frappe.get_doc", side_effect=self._get_doc))
        self.exists = self.enterContext(patch(f"{MOD}.frappe.db.exists", side_effect=self._exists))
        self.get_value = self.enterContext(patch(f"{MOD}.frappe.db.get_value", side_effect=self._get_value))
        self.get_all = self.enterContext(patch(f"{MOD}.frappe.get_all", side_effect=self._get_all))
        self.get_meta = self.enterContext(patch(f"{MOD}.frappe.get_meta", return_value=self.meta))
        self.user = self.enterContext(patch(f"{MOD}.frappe.set_user"))
        self.secret = self.enterContext(patch(f"{MOD}._get_profile_secret", return_value=SECRET))
        self.now = self.enterContext(patch(f"{MOD}.now_datetime", return_value=FIXED_NOW))
        self.sql = self.enterContext(patch(f"{MOD}.frappe.db.sql", side_effect=AssertionError("guest feedback must not build SQL")))

    def _get_doc(self, doctype, name=None, **kwargs):
        if isinstance(doctype, dict):
            self.payloads.append(doctype)
            return self.feedback
        if doctype == "URY Self Ordering Profile":
            return self.profile
        if doctype == "POS Invoice":
            self.invoice_loads.append(name)
            if name != self.invoice.name:
                missing = SimpleNamespace(**self.invoice.__dict__)
                missing.name = name
                missing.customer = "OTHER-CUSTOMER"
                return missing
            return self.invoice
        if doctype == "Quality Feedback Template":
            self.template_loads.append(name)
            return self.template
        raise AssertionError(f"unexpected get_doc {doctype} {name}")

    def _exists(self, doctype, filters=None, **kwargs):
        self.exists_calls.append((doctype, filters))
        if doctype == "Quality Feedback" and self.duplicate:
            return "QA-FB-1"
        return None

    def _get_value(self, doctype, filters, fieldname="name", **kwargs):
        self.assertEqual(doctype, "Quality Feedback")
        self.assertEqual(fieldname, "name")
        return self._exists(doctype, filters)

    def _get_all(self, doctype, *args, **kwargs):
        self.assertNotEqual(doctype, "Quality Feedback")
        if doctype == "URY Self Ordering Profile":
            return getattr(self, "profiles", ["Profile A"])
        raise AssertionError(f"unexpected get_all {doctype}")

    def submit(self, **kwargs):
        method = getattr(self.api, "submit_feedback", None)
        self.assertTrue(callable(method), "Guests cannot submit native feedback yet")
        return method(**kwargs)

    def test_feedback_disabled_by_default_refuses_before_touching_the_bill(self):
        for enabled in (0, None):
            with self.subTest(enabled=enabled):
                self.profile.enable_feedback = enabled
                if enabled is None:
                    del self.profile.enable_feedback
                with self.assertRaises(frappe.ValidationError) as caught:
                    self.submit(session="opaque-session", ratings=RATINGS, invoice="POS-9", template="EVIL")
                self.assertIn("URY_FEEDBACK_DISABLED", str(caught.exception))
        self.assertEqual(self.invoice_loads, [])
        self.feedback.insert.assert_not_called()

    def test_enabled_profile_without_template_is_refused(self):
        self.profile.feedback_template = None
        with self.assertRaises(frappe.ValidationError) as caught:
            self.submit(session="opaque-session", ratings=RATINGS)
        self.assertIn("URY_FEEDBACK_TEMPLATE", str(caught.exception))
        self.feedback.insert.assert_not_called()

    def test_missing_bill_link_field_refuses_before_insert(self):
        self.meta.has_field.side_effect = lambda field: False
        with self.assertRaises(frappe.ValidationError) as caught:
            self.submit(session="opaque-session", ratings=RATINGS)
        self.assertIn("URY_FEEDBACK_BILL_LINK_MISSING", str(caught.exception))
        self.assertEqual(self.invoice_loads, [])
        self.feedback.insert.assert_not_called()

    def test_open_unprinted_bill_is_not_eligible(self):
        self.invoice.invoice_printed = 0
        self.invoice.docstatus = 0
        with self.assertRaises(frappe.ValidationError) as caught:
            self.submit(session="opaque-session", ratings=RATINGS)
        self.assertIn("URY_FEEDBACK_NOT_ELIGIBLE", str(caught.exception))
        self.feedback.insert.assert_not_called()

    def test_printed_unpaid_bill_stores_the_session_invoice_only(self):
        result = self.submit(
            session="opaque-session",
            ratings=json.dumps(RATINGS),
            invoice="POS-1' OR '1'='1",
            template="EVIL",
            receipt_token=_expected_grant("Profile A", "feedback", "POS-2", _epoch(FIXED_NOW) + 60),
        )
        self.assertEqual(result, {"status": "Received"})
        self.assertNotIn("SECRET-CUSTOMER", json.dumps(result))
        self.assertNotIn("0700999888", json.dumps(result))
        self.assertEqual(self.invoice_loads, ["POS-1"])
        self.assertEqual(self.template_loads, ["Guest Meal"])
        self.assertEqual(self.exists_calls, [("Quality Feedback", {"custom_pos_invoice": "POS-1"})])
        self.assertEqual(self.payloads, [{
            "doctype": "Quality Feedback",
            "template": "Guest Meal",
            "document_type": "Customer",
            "document_name": "SECRET-CUSTOMER",
            "custom_pos_invoice": "POS-1",
            "parameters": [
                {"parameter": "Food", "rating": "5"},
                {"parameter": "Service", "rating": "4"},
                {"parameter": "Ambience", "rating": "3"},
            ],
        }])
        self.feedback.insert.assert_called_once_with(ignore_permissions=True)
        self.assertEqual(self.user.call_args_list[0].args, ("Administrator",))
        self.assertEqual(self.user.call_args_list[-1].args, ("Guest",))
        self.sql.assert_not_called()

    def test_paid_unprinted_bill_is_eligible(self):
        self.invoice.invoice_printed = 0
        self.invoice.docstatus = 1
        self.invoice.outstanding_amount = 0
        self.invoice.status = "Paid"
        self.assertEqual(self.submit(session="opaque-session", ratings=RATINGS), {"status": "Received"})

    def test_partly_paid_unprinted_bill_is_refused(self):
        self.invoice.invoice_printed = 0
        self.invoice.docstatus = 1
        self.invoice.outstanding_amount = 5
        self.invoice.status = "Partly Paid"
        with self.assertRaises(frappe.ValidationError) as caught:
            self.submit(session="opaque-session", ratings=RATINGS)
        self.assertIn("URY_FEEDBACK_NOT_ELIGIBLE", str(caught.exception))
        self.feedback.insert.assert_not_called()

    def test_cancelled_or_return_bill_is_refused(self):
        cases = (
            {"docstatus": 2, "invoice_printed": 1, "is_return": 0, "outstanding_amount": 0, "status": "Cancelled"},
            {"docstatus": 1, "invoice_printed": 1, "is_return": 1, "outstanding_amount": 0, "status": "Return"},
        )
        for changes in cases:
            with self.subTest(status=changes["status"]):
                for key, value in changes.items():
                    setattr(self.invoice, key, value)
                with self.assertRaises(frappe.ValidationError) as caught:
                    self.submit(session="opaque-session", ratings=RATINGS)
                self.assertIn("URY_FEEDBACK_NOT_ELIGIBLE", str(caught.exception))
        self.feedback.insert.assert_not_called()

    def test_bill_from_another_till_is_refused(self):
        self.invoice.pos_profile = "Training Till"
        with self.assertRaises(frappe.PermissionError):
            self.submit(session="opaque-session", ratings=RATINGS)
        self.feedback.insert.assert_not_called()

    def test_second_feedback_for_the_same_bill_does_not_insert(self):
        self.duplicate = True
        self.assertEqual(self.submit(session="opaque-session", ratings=RATINGS), {"status": "Already Received"})
        self.feedback.insert.assert_not_called()
        self.assertNotIn("QA-FB-1", json.dumps(self.submit(session="opaque-session", ratings=RATINGS)))

    def test_receipt_retry_sees_feedback_committed_while_waiting_for_invoice_lock(self):
        # A consistent read can retain an old snapshot even after another
        # transaction commits. Both invoice and duplicate reads must be current.
        self.exists.side_effect = lambda *args, **kwargs: None
        get_doc = self._get_doc

        def after_winner_commit(doctype, name=None, **kwargs):
            if doctype == "POS Invoice" and name == "POS-1" and kwargs.get("for_update"):
                self.duplicate = True
            return get_doc(doctype, name, **kwargs)

        def current_feedback(doctype, filters, fieldname="name", **kwargs):
            if kwargs.get("for_update"):
                return self._get_value(doctype, filters, fieldname, **kwargs)
            return None

        self.get_doc.side_effect = after_winner_commit
        self.get_value.side_effect = current_feedback
        token = _expected_grant("Profile A", "feedback", "POS-1", _epoch(FIXED_NOW) + 60)
        self.assertEqual(self.submit(receipt_token=token, ratings=RATINGS), {"status": "Already Received"})
        self.feedback.insert.assert_not_called()

    def test_receipt_retry_is_idempotent_despite_an_earlier_empty_snapshot(self):
        token = _expected_grant("Profile A", "feedback", "POS-1", _epoch(FIXED_NOW) + 60)
        self.feedback.insert.side_effect = lambda **kwargs: setattr(self, "duplicate", True)
        self.exists.side_effect = lambda *args, **kwargs: None
        get_value = self._get_value
        self.get_value.side_effect = lambda *args, **kwargs: get_value(*args, **kwargs) if kwargs.get("for_update") else None
        self.assertEqual(self.submit(receipt_token=token, ratings=RATINGS), {"status": "Received"})
        self.assertEqual(self.submit(receipt_token=token, ratings=RATINGS), {"status": "Already Received"})
        self.feedback.insert.assert_called_once_with(ignore_permissions=True)

    def test_native_snapshot_conflict_returns_safe_retry_not_database_details(self):
        original_get_doc = self._get_doc

        def conflict(doctype, name=None, **kwargs):
            if doctype == "POS Invoice" and kwargs.get("for_update"):
                raise frappe.QueryDeadlockError(1020, "PRIVATE native snapshot details")
            return original_get_doc(doctype, name, **kwargs)

        self.get_doc.side_effect = conflict
        try:
            self.submit(session="opaque-session", ratings=RATINGS)
        except (frappe.ValidationError, frappe.QueryDeadlockError) as exc:
            self.assertIsInstance(exc, frappe.ValidationError)
            self.assertIn("URY_FEEDBACK_RETRY", str(exc))
            self.assertNotIn("PRIVATE", str(exc))
        else:
            self.fail("A native snapshot conflict must refuse with a safe retry")
        self.feedback.insert.assert_not_called()

    def test_native_duplicate_lock_timeout_returns_safe_retry_without_guessing_success(self):
        self.get_value.side_effect = frappe.QueryTimeoutError("PRIVATE native lock details")
        try:
            self.submit(session="opaque-session", ratings=RATINGS)
        except (frappe.ValidationError, frappe.QueryTimeoutError) as exc:
            self.assertIsInstance(exc, frappe.ValidationError)
            self.assertIn("URY_FEEDBACK_RETRY", str(exc))
            self.assertNotIn("PRIVATE", str(exc))
        else:
            self.fail("A native lock timeout must not guess successful feedback")
        self.feedback.insert.assert_not_called()

    def test_rating_outside_1_to_5_is_refused(self):
        for rating in (0, 6, -1, True, 5.0, "2x", " 5 ", "", None):
            with self.subTest(rating=rating):
                ratings = [
                    {"parameter": "Food", "rating": rating},
                    {"parameter": "Service", "rating": 4},
                    {"parameter": "Ambience", "rating": 3},
                ]
                with self.assertRaises(frappe.ValidationError) as caught:
                    self.submit(session="opaque-session", ratings=ratings)
                self.assertIn("URY_FEEDBACK_RATINGS", str(caught.exception))
        self.feedback.insert.assert_not_called()

    def test_unknown_parameter_is_refused(self):
        ratings = [
            {"parameter": "Food' OR '1'='1", "rating": 5},
            {"parameter": "Service", "rating": 4},
            {"parameter": "Ambience", "rating": 3},
        ]
        with self.assertRaises(frappe.ValidationError) as caught:
            self.submit(session="opaque-session", ratings=ratings)
        self.assertIn("URY_FEEDBACK_RATINGS", str(caught.exception))
        self.feedback.insert.assert_not_called()
        self.sql.assert_not_called()

    def test_free_text_is_not_stored_on_the_rating_row(self):
        ratings = [
            {"parameter": "Food", "rating": 5, "feedback": "<script>alert(1)</script>"},
            {"parameter": "Service", "rating": 4},
            {"parameter": "Ambience", "rating": 3},
        ]
        with self.assertRaises(frappe.ValidationError) as caught:
            self.submit(session="opaque-session", ratings=ratings)
        self.assertIn("URY_FEEDBACK_RATINGS", str(caught.exception))
        self.feedback.insert.assert_not_called()

    def test_missing_template_parameter_is_refused(self):
        ratings = [{"parameter": "Food", "rating": 5}, {"parameter": "Service", "rating": 4}]
        with self.assertRaises(frappe.ValidationError) as caught:
            self.submit(session="opaque-session", ratings=ratings)
        self.assertIn("URY_FEEDBACK_RATINGS", str(caught.exception))
        self.feedback.insert.assert_not_called()

    def test_session_without_its_own_invoice_is_refused(self):
        self.session.invoice = None
        with self.assertRaises(frappe.ValidationError):
            self.submit(session="opaque-session", ratings=RATINGS, invoice="POS-1")
        self.assertEqual(self.invoice_loads, [])
        self.feedback.insert.assert_not_called()

    def test_forged_session_is_refused_before_insert(self):
        self.resolve.side_effect = frappe.PermissionError("Expired")
        with self.assertRaises(frappe.PermissionError):
            self.submit(session="forged", ratings=RATINGS, invoice="POS-1")
        self.get_doc.assert_not_called()

    def test_receipt_token_submits_its_own_invoice_without_a_session(self):
        self.profile.name = "Salama|Kampala"
        expiry = _epoch(FIXED_NOW) + 3600
        token = _expected_grant("Salama|Kampala", "feedback", "POS-1", expiry)
        result = self.submit(receipt_token=token, ratings=RATINGS, invoice="POS-9", template="EVIL")
        self.assertEqual(result, {"status": "Received"})
        self.resolve.assert_not_called()
        self.assertEqual(self.invoice_loads, ["POS-1"])
        self.assertEqual(self.payloads[0]["custom_pos_invoice"], "POS-1")
        self.assertEqual(self.payloads[0]["document_name"], "SECRET-CUSTOMER")
        self.assertNotIn("SECRET-CUSTOMER", json.dumps(result))

    def _invoice_permission_boundary(self, allowed):
        self.invoice.has_permission = lambda permtype: permtype in allowed
        self.invoice._handle_permission_failure = MagicMock(side_effect=frappe.PermissionError("Denied"))
        self.invoice.check_permission = lambda permtype: Document.check_permission(self.invoice, permtype)

    def test_receipt_helper_refuses_without_invoice_permission_before_profile_or_secret(self):
        self._invoice_permission_boundary(set())
        with self.assertRaises(frappe.PermissionError):
            self.api.feedback_receipt_link("POS-1")
        self.get_all.assert_not_called()
        self.secret.assert_not_called()
        self.user.assert_not_called()

    def test_receipt_helper_does_not_treat_invoice_read_as_print_authority(self):
        self._invoice_permission_boundary({"read"})
        with self.assertRaises(frappe.PermissionError):
            self.api.feedback_receipt_link("POS-1")
        self.secret.assert_not_called()

    def test_receipt_helper_accepts_native_print_authority_without_read(self):
        self._invoice_permission_boundary({"print"})
        token = self.api.feedback_receipt_link("POS-1")
        self.assertEqual(self.api.read_signed_invoice_grant(token, "feedback"), ("Profile A", "POS-1"))
        self.user.assert_not_called()

    def test_forged_expired_or_bill_grant_is_refused(self):
        expiry = _epoch(FIXED_NOW) + 3600
        valid = _expected_grant("Profile A", "feedback", "POS-1", expiry)
        forged = valid[:-1] + ("A" if valid[-1] != "A" else "B")
        cases = {
            "forged": forged,
            "expired": _expected_grant("Profile A", "feedback", "POS-1", _epoch(FIXED_NOW)),
            "too_long": _expected_grant("Profile A", "feedback", "POS-1", _epoch(FIXED_NOW) + GRANT_LIFETIME_SECONDS + 1),
            "bill_purpose": _expected_grant("Profile A", "bill", "POS-1", expiry),
            "malformed": "not-a-token",
        }
        for name, token in cases.items():
            with self.subTest(token=name):
                with self.assertRaises(frappe.PermissionError):
                    self.submit(receipt_token=token, ratings=RATINGS)
        with self.assertRaises(frappe.PermissionError):
            self.submit(ratings=RATINGS, invoice="POS-1")
        self.feedback.insert.assert_not_called()
        self.sql.assert_not_called()

    def test_malformed_signature_is_a_permission_refusal_not_a_server_error(self):
        body = f"Profile A|feedback|POS-1|{_epoch(FIXED_NOW) + 60}"
        for signature in ("é" * 64, "☃", "", "0" * 63, "0" * 65, "g" * 64):
            with self.subTest(signature=signature):
                token = base64.urlsafe_b64encode(f"{body}|{signature}".encode()).decode().rstrip("=")
                try:
                    self.submit(receipt_token=token, ratings=RATINGS)
                except Exception as error:
                    self.assertIsInstance(error, frappe.PermissionError)
                else:
                    self.fail("Malformed signature was accepted")
        self.feedback.insert.assert_not_called()

    def test_insert_failure_restores_guest_identity(self):
        self.feedback.insert.side_effect = frappe.ValidationError("Cannot insert")
        with self.assertRaises(frappe.ValidationError):
            self.submit(session="opaque-session", ratings=RATINGS)
        self.assertEqual(self.user.call_args_list[-1].args, ("Guest",))

    def test_guest_cannot_read_feedback_back(self):
        for name in ("get_feedback", "list_feedback", "get_customer_feedback"):
            self.assertFalse(callable(getattr(self.api, name, None)), name)
        method = getattr(self.api, "submit_feedback", None)
        self.assertTrue(callable(method), "Guests cannot submit native feedback yet")
        self.assertIn(method, frappe.guest_methods)
        self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])
        for helper in ("feedback_receipt_link", "mint_signed_invoice_grant", "read_signed_invoice_grant"):
            fn = getattr(self.api, helper, None)
            self.assertTrue(callable(fn), helper)
            self.assertNotIn(fn, frappe.guest_methods)

    def test_repeated_attempts_are_rate_limited_per_address(self):
        self.profile.enable_feedback = 0
        frappe.cache = _MemoryCache()
        frappe.local.request = SimpleNamespace(method="POST")
        frappe.local.request_ip = "203.0.113.5"
        frappe.local.form_dict = frappe._dict(
            cmd="ury.ury.api.self_ordering.submit_feedback",
            session="opaque-session",
        )
        try:
            for _ in range(5):
                with self.assertRaises(frappe.ValidationError):
                    self.submit(session="opaque-session", ratings=RATINGS)
            with self.assertRaises(frappe.RateLimitExceededError):
                self.submit(session="opaque-session", ratings=RATINGS)
            frappe.local.request_ip = "203.0.113.6"
            with self.assertRaises((frappe.ValidationError, frappe.RateLimitExceededError)) as caught:
                self.submit(session="opaque-session", ratings=RATINGS)
            self.assertIsInstance(caught.exception, frappe.RateLimitExceededError)
        finally:
            frappe.cache = None
            frappe.local.request = None
        self.feedback.insert.assert_not_called()

    def _feedback_budget_outcomes(self, credentials, addresses, credential_key):
        self.profile.enable_feedback = 0
        self.resolve.side_effect = lambda token: SimpleNamespace(**{**vars(self.session), "name": f"session-{token}"})
        self.enterContext(patch.object(frappe, "cache", _MemoryCache()))
        frappe.local.request = SimpleNamespace(method="POST")
        self.addCleanup(setattr, frappe.local, "request", None)
        outcomes = []
        for credential, address in zip(credentials, addresses, strict=True):
            frappe.local.request_ip = address
            frappe.local.form_dict = frappe._dict(
                cmd=f"{MOD}.submit_feedback",
                **{credential_key: credential},
                _ury_feedback_budget=f"guest-chosen-{address}",
            )
            try:
                self.submit(**{credential_key: credential}, ratings=RATINGS)
            except frappe.RateLimitExceededError:
                outcomes.append("limited")
            except frappe.ValidationError:
                outcomes.append("disabled")
        return outcomes

    def test_ip_budget_cannot_be_reset_by_changing_session(self):
        outcomes = self._feedback_budget_outcomes(
            [f"attempt-{index}" for index in range(6)], ["203.0.113.5"] * 6, "session",
        )
        self.assertEqual(outcomes, ["disabled"] * 5 + ["limited"])

    def test_session_budget_cannot_be_reset_by_changing_ip(self):
        outcomes = self._feedback_budget_outcomes(
            ["opaque-session"] * 6, [f"203.0.113.{index}" for index in range(6)], "session",
        )
        self.assertEqual(outcomes, ["disabled"] * 5 + ["limited"])

    def test_receipt_budget_cannot_be_reset_by_changing_ip(self):
        token = _expected_grant("Profile A", "feedback", "POS-1", _epoch(FIXED_NOW) + 60)
        outcomes = self._feedback_budget_outcomes(
            [token] * 6, [f"203.0.113.{index}" for index in range(6)], "receipt_token",
        )
        self.assertEqual(outcomes, ["disabled"] * 5 + ["limited"])

    def test_receipt_budget_uses_verified_bill_not_guest_encoding_or_budget_key(self):
        token = _expected_grant("Profile A", "feedback", "POS-1", _epoch(FIXED_NOW) + 60)
        outcomes = self._feedback_budget_outcomes(
            [token + "=" * index for index in range(6)],
            [f"203.0.113.{index}" for index in range(6)], "receipt_token",
        )
        self.assertEqual(outcomes, ["disabled"] * 5 + ["limited"])


class TestInvoiceGrant(unittest.TestCase):
    def setUp(self):
        _bind_locals()
        self.api = self_ordering
        self.enterContext(patch(f"{MOD}._get_profile_secret", return_value=SECRET))
        self.now = self.enterContext(patch(f"{MOD}.now_datetime", return_value=FIXED_NOW))

    def mint(self, profile, invoice, purpose, expires_at):
        method = getattr(self.api, "mint_signed_invoice_grant", None)
        self.assertTrue(callable(method), "Invoice grant mint helper is missing")
        return method(profile, invoice, purpose, expires_at)

    def read(self, token, purpose):
        method = getattr(self.api, "read_signed_invoice_grant", None)
        self.assertTrue(callable(method), "Invoice grant reader is missing")
        return method(token, purpose)

    def test_grant_round_trip_uses_the_profile_signing_secret(self):
        expires_at = FIXED_NOW + timedelta(days=7)
        token = self.mint("Salama|Kampala", "POS-1", "feedback", expires_at)
        self.assertEqual(token, _expected_grant("Salama|Kampala", "feedback", "POS-1", _epoch(expires_at)))
        self.assertEqual(self.read(token, "feedback"), ("Salama|Kampala", "POS-1"))
        with self.assertRaises(frappe.PermissionError):
            self.read(token, "bill")

    def test_receipt_link_binds_the_unprinted_bill_it_will_be_printed_on(self):
        invoice = SimpleNamespace(
            name="POS-1", branch="Salama", pos_profile="Salama Till",
            docstatus=0, invoice_printed=0, is_return=0,
            check_permission=MagicMock(),
        )
        profile = SimpleNamespace(
            name="Profile A", branch="Salama", pos_profile="Salama Till",
            enable_feedback=1, enabled=1, feedback_template="Guest Meal",
        )
        self.enterContext(patch(f"{MOD}.frappe.get_doc", side_effect=lambda doctype, name=None: invoice if doctype == "POS Invoice" else profile))
        recorded = {}

        def get_all(doctype, *args, **kwargs):
            recorded["doctype"] = doctype
            recorded["filters"] = kwargs.get("filters")
            return ["Profile A"]

        self.enterContext(patch(f"{MOD}.frappe.get_all", side_effect=get_all))
        method = getattr(self.api, "feedback_receipt_link", None)
        self.assertTrue(callable(method), "Receipt feedback link helper is missing")
        token = method("POS-1")
        self.assertEqual(recorded["doctype"], "URY Self Ordering Profile")
        self.assertEqual(recorded["filters"], {
            "branch": "Salama",
            "pos_profile": "Salama Till",
            "enabled": 1,
            "enable_feedback": 1,
        })
        self.assertEqual(token, _expected_grant("Profile A", "feedback", "POS-1", _epoch(FIXED_NOW) + GRANT_LIFETIME_SECONDS))
        self.assertNotIn(method, frappe.guest_methods)

    def test_receipt_link_is_blank_when_feedback_is_not_uniquely_enabled(self):
        invoice = SimpleNamespace(
            name="POS-1", branch="Salama", pos_profile="Salama Till",
            docstatus=0, invoice_printed=0, is_return=0,
            check_permission=MagicMock(),
        )
        self.enterContext(patch(f"{MOD}.frappe.get_doc", return_value=invoice))
        profiles = {"rows": ["Profile A", "Profile B"]}

        def get_all(doctype, *args, **kwargs):
            return profiles["rows"]

        self.enterContext(patch(f"{MOD}.frappe.get_all", side_effect=get_all))
        method = getattr(self.api, "feedback_receipt_link", None)
        self.assertTrue(callable(method), "Receipt feedback link helper is missing")
        self.assertEqual(method("POS-1"), "")

        profiles["rows"] = ["Profile A"]
        for changes in ({"docstatus": 2, "is_return": 0}, {"docstatus": 1, "is_return": 1}):
            with self.subTest(changes=changes):
                current = SimpleNamespace(**invoice.__dict__)
                for key, value in changes.items():
                    setattr(current, key, value)
                self.enterContext(patch(f"{MOD}.frappe.get_doc", return_value=current))
                self.assertEqual(method("POS-1"), "")

class TestFeedbackForm(unittest.TestCase):
    setUp = TestGuestFeedback.setUp
    _get_doc = TestGuestFeedback._get_doc
    _exists = TestGuestFeedback._exists
    _get_value = TestGuestFeedback._get_value
    _get_all = TestGuestFeedback._get_all
    _invoice_permission_boundary = TestGuestFeedback._invoice_permission_boundary

    def _receipt_qr_without_native_qr(self):
        method = getattr(self.api, "feedback_receipt_qr", None)
        self.assertTrue(callable(method), "Receipt QR helper is missing")
        from frappe.utils import print_format_generator

        qr = self.enterContext(patch.object(print_format_generator, "get_qr_code", side_effect=AssertionError("no QR expected")))
        logged = self.enterContext(patch(f"{MOD}.frappe.log_error"))
        return method, qr, logged

    def test_receipt_qr_is_blank_for_a_read_only_printer(self):
        # Native print accepts read OR print; the receipt must still render.
        self._invoice_permission_boundary({"read"})
        method, qr, logged = self._receipt_qr_without_native_qr()
        self.assertEqual(method("POS-1", "https://salama.example/order/feedback"), "")
        qr.assert_not_called()
        logged.assert_not_called()
        self.secret.assert_not_called()

    def test_receipt_qr_is_blank_when_the_link_lookup_fails(self):
        self.get_all.side_effect = RuntimeError("db down")
        method, qr, logged = self._receipt_qr_without_native_qr()
        self.assertEqual(method("POS-1", "https://salama.example/order/feedback"), "")
        qr.assert_not_called()
        logged.assert_called_once()

    def test_receipt_qr_is_blank_until_a_feedback_template_is_chosen(self):
        # Same eligibility as the capability and the form: enabled AND template.
        self.profile.feedback_template = None
        self.assertEqual(self.api.feedback_receipt_link("POS-1"), "")
        method, qr, _logged = self._receipt_qr_without_native_qr()
        self.assertEqual(method("POS-1", "https://salama.example/order/feedback"), "")
        qr.assert_not_called()
        self.secret.assert_not_called()

    def test_feedback_capability_requires_an_enabled_profile_and_template(self):
        for enabled, template, expected in (
            (0, None, False),
            (1, None, False),
            (0, "Guest Meal", False),
            (1, "Guest Meal", True),
        ):
            with self.subTest(enabled=enabled, template=template):
                profile = SimpleNamespace(
                    restaurant="Salama",
                    enable_product_detail_page=0,
                    show_item_images=0,
                    show_item_descriptions=0,
                    enable_item_notes=0,
                    enable_request_bill=0,
                    enable_customer_payment=0,
                    enable_payment_link=0,
                    enable_pay_at_counter=0,
                    allow_add_to_running_table=0,
                    enable_feedback=enabled,
                    feedback_template=template,
                )
                response = self.api._ordering_context_response("SESSION", "QR Table", profile, "Table 7", "Mobile")
                self.assertEqual(response["capabilities"]["feedback_enabled"], expected)

    def _get_feedback_form(self, **kwargs):
        method = getattr(self.api, "get_feedback_form", None)
        self.assertTrue(callable(method), "Guest feedback form reader is missing")
        return method(**kwargs)

    def test_feedback_form_requires_a_verified_credential(self):
        expiry = _epoch(FIXED_NOW) + 3600
        valid = _expected_grant("Profile A", "feedback", "POS-1", expiry)
        expired = _expected_grant("Profile A", "feedback", "POS-1", _epoch(FIXED_NOW))
        wrong_purpose = _expected_grant("Profile A", "bill", "POS-1", expiry)
        for kwargs in ({}, {"receipt_token": "forged"}, {"receipt_token": expired}, {"receipt_token": wrong_purpose}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(frappe.PermissionError) as caught:
                    self._get_feedback_form(**kwargs)
                self.assertIn("Invalid feedback link", str(caught.exception))
        self.assertEqual(self._get_feedback_form(session="opaque-session")["status"], "open")
        self.assertEqual(self._get_feedback_form(receipt_token=valid)["status"], "open")

    def test_feedback_form_returns_only_ordered_translated_template_labels(self):
        self.enterContext(patch(f"{MOD}._", side_effect=lambda value: f"translated:{value}"))
        response = self._get_feedback_form(session="opaque-session")
        self.assertEqual(set(response), {"status", "parameters"})
        self.assertEqual(response["status"], "open")
        self.assertEqual(response["parameters"], [
            {"parameter": "Food", "label": "translated:Food"},
            {"parameter": "Service", "label": "translated:Service"},
            {"parameter": "Ambience", "label": "translated:Ambience"},
        ])
        serialized = json.dumps(response)
        for secret in ("POS-1", "SECRET-CUSTOMER", "QA-FB-9"):
            self.assertNotIn(secret, serialized)

    def test_feedback_form_reports_open_received_and_not_eligible_statuses(self):
        self.assertEqual(self._get_feedback_form(session="opaque-session")["status"], "open")
        self.duplicate = True
        self.assertEqual(self._get_feedback_form(session="opaque-session")["status"], "received")

        cases = (
            {"invoice_printed": 0, "docstatus": 0, "is_return": 0},
            {"invoice_printed": 1, "docstatus": 2, "is_return": 0},
            {"invoice_printed": 1, "docstatus": 1, "is_return": 1},
            {"invoice_printed": 1, "docstatus": 0, "is_return": 0, "pos_profile": "Other Till"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.duplicate = False
                self.invoice.invoice_printed = 1
                self.invoice.docstatus = 0
                self.invoice.is_return = 0
                self.invoice.pos_profile = "Salama Till"
                for key, value in changes.items():
                    setattr(self.invoice, key, value)
                self.assertEqual(self._get_feedback_form(session="opaque-session")["status"], "not_eligible")

    def test_feedback_form_is_disabled_and_post_only(self):
        self.profile.enable_feedback = 0
        method = getattr(self.api, "get_feedback_form", None)
        self.assertTrue(callable(method), "Guest feedback form reader is missing")
        with self.assertRaises(frappe.ValidationError) as caught:
            method(session="opaque-session")
        self.assertIn("URY_FEEDBACK_DISABLED", str(caught.exception))
        self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])

    def test_feedback_form_charges_the_verified_credential_budget(self):
        self.profile.enable_feedback = 0
        self.enterContext(patch.object(frappe, "cache", _MemoryCache()))
        frappe.local.request = SimpleNamespace(method="POST")
        self.addCleanup(setattr, frappe.local, "request", None)
        for index in range(5):
            frappe.local.request_ip = f"203.0.113.{index}"
            frappe.local.form_dict = frappe._dict(cmd=f"{MOD}.get_feedback_form", session="opaque-session")
            with self.assertRaises(frappe.ValidationError):
                self._get_feedback_form(session="opaque-session")
        frappe.local.request_ip = "203.0.113.99"
        frappe.local.form_dict = frappe._dict(cmd=f"{MOD}.get_feedback_form", session="opaque-session")
        with self.assertRaises(frappe.RateLimitExceededError):
            self._get_feedback_form(session="opaque-session")

    def test_feedback_receipt_qr_is_blank_or_native_svg_and_fails_closed(self):
        method = getattr(self.api, "feedback_receipt_qr", None)
        self.assertTrue(callable(method), "Receipt QR helper is missing")
        link = self.enterContext(patch(f"{MOD}.feedback_receipt_link", return_value="TOKEN"))
        from frappe.utils import print_format_generator

        def fake_qr(value):
            return "data:image/svg+xml;base64," + base64.b64encode(value.encode()).decode()

        qr = self.enterContext(patch.object(print_format_generator, "get_qr_code", side_effect=fake_qr))
        result = method("POS-1", "https://salama.example/order/feedback")
        self.assertTrue(result.startswith("data:image/svg+xml;base64,"))
        self.assertEqual(
            base64.b64decode(result.split(",", 1)[1]).decode(),
            "https://salama.example/order/feedback#r=TOKEN",
        )
        qr.assert_called_once_with("https://salama.example/order/feedback#r=TOKEN")
        link.return_value = ""
        qr.reset_mock()
        self.assertEqual(method("POS-1", "https://salama.example/order/feedback"), "")
        qr.assert_not_called()

        link.return_value = "TOKEN"
        qr.side_effect = RuntimeError("native QR unavailable")
        logged = self.enterContext(patch(f"{MOD}.frappe.log_error"))
        self.assertEqual(method("POS-1", "https://salama.example/order/feedback"), "")
        logged.assert_called_once()


class _MemoryCache:
    def __init__(self):
        self.store = {}

    def make_key(self, key, user=None, shared=False):
        return f"unit|{key}".encode()

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, seconds, value):
        self.store[key] = value

    def incrby(self, key, amount):
        self.store[key] = int(self.store.get(key) or 0) + amount
        return self.store[key]


class TestFeedbackProfileMetadata(unittest.TestCase):
    def test_profile_feedback_is_off_until_a_template_is_chosen(self):
        from pathlib import Path
        import ury.ury.doctype.ury_self_ordering_profile as profile_module

        document = json.loads((Path(profile_module.__file__).parent / "ury_self_ordering_profile.json").read_text())
        fields = {field["fieldname"]: field for field in document["fields"]}
        self.assertIn("enable_feedback", fields)
        self.assertEqual(fields["enable_feedback"]["fieldtype"], "Check")
        self.assertEqual(fields["enable_feedback"].get("default"), "0")
        self.assertIn("feedback_template", fields)
        self.assertEqual(fields["feedback_template"]["fieldtype"], "Link")
        self.assertEqual(fields["feedback_template"]["options"], "Quality Feedback Template")
        self.assertNotIn("reqd", fields["feedback_template"])
        self.assertIn("enable_feedback", document["field_order"])
        self.assertIn("feedback_template", document["field_order"])


class TestFeedbackReceiptRegistration(unittest.TestCase):
    def test_print_formats_can_call_the_receipt_helper(self):
        import ury.hooks as hooks

        methods = []
        jinja = getattr(hooks, "jinja", None) or {}
        if isinstance(jinja, dict):
            methods = jinja.get("methods") or []
        self.assertEqual(methods, [
            "ury.ury.api.self_ordering.feedback_receipt_link",
            "ury.ury.api.self_ordering.feedback_receipt_qr",
        ])
        form = getattr(self_ordering, "get_feedback_form", None)
        self.assertTrue(callable(form), "Guest feedback form reader is missing")
        self.assertIn(form, frappe.guest_methods)
        self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[form], ["POST"])
