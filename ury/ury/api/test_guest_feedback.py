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
            with self.assertRaises(frappe.ValidationError):
                self.submit(session="opaque-session", ratings=RATINGS)
        finally:
            frappe.cache = None
            frappe.local.request = None
        self.feedback.insert.assert_not_called()


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
        )
        profile = SimpleNamespace(name="Profile A", branch="Salama", pos_profile="Salama Till", enable_feedback=1, enabled=1)
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
        self.assertIn("ury.ury.api.self_ordering.feedback_receipt_link", methods)
