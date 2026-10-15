"""P2: real POS/Payment Request persistence; only the PSP controller is a double."""

import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext.accounts.utils import create_payment_gateway_account
from frappe.utils import add_to_date, now_datetime

from ury.tests.offline_pos_utils import make_offline_pos, send_order, settle_order
from ury.ury.api.self_ordering import create_payment_request, get_payment_status


class TestGuestPhonePayment(unittest.TestCase):
    def setUp(self):
        self.pos = make_offline_pos()
        frappe.set_user("Administrator")
        if not frappe.db.exists("Payment Gateway", "P2 Test"):
            frappe.get_doc({"doctype": "Payment Gateway", "gateway": "P2 Test"}).insert()
        create_payment_gateway_account("P2 Test", payment_channel="Phone", company="URY")
        self.gateway = frappe.get_doc("Payment Gateway Account",
            {"payment_gateway": "P2 Test", "company": "URY"})
        self.gateway.is_default = 1
        self.gateway.save()
        account = self.gateway.payment_account
        if not frappe.db.exists("Mode of Payment", "P2 Phone"):
            frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "P2 Phone",
                "type": "Phone", "accounts": [{"company": "URY", "default_account": account}]}).insert()
        self.pos.profile.append("payments", {"mode_of_payment": "P2 Phone"})
        self.pos.profile.save()
        self.profile = frappe.get_doc({"doctype": "URY Self Ordering Profile",
            "name": "P2 Guest " + frappe.generate_hash(length=8),
            "profile_name": self.pos.profile.name, "restaurant": self.pos.restaurant.name,
            "branch": self.pos.branch.name, "pos_profile": self.pos.profile.name,
            "default_customer": self.pos.customer.name, "enable_customer_payment": 1}).insert()
        frappe.set_user(self.pos.user.name)
        self.invoice_name = send_order(self.pos).name
        frappe.set_user("Administrator")
        self.token = frappe.generate_hash(length=64)
        frappe.get_doc({"doctype": "URY Ordering Session", "ordering_profile": self.profile.name,
            "source": "QR Pickup", "token_hash": hashlib.sha256(self.token.encode()).hexdigest(),
            "status": "Active", "opened_at": now_datetime(),
            "expires_at": add_to_date(now_datetime(), minutes=30),
            "invoice": self.invoice_name}).insert()
        self.controller = controller = SimpleNamespace(
            validate_transaction_currency=lambda currency: None,
            request_for_payment=lambda **kwargs: None,
            get_payment_url=lambda **kwargs: "https://psp.example.test/" + kwargs["reference_docname"],
        )
        self.enterContext(patch(
            "erpnext.accounts.doctype.payment_request.payment_request._get_payment_gateway_controller",
            return_value=controller,
        ))
        frappe.local.response = frappe._dict()
        frappe.set_user("Guest")

    def tearDown(self):
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.clear_cache()

    def request(self, **extra):
        try:
            return frappe.call(create_payment_request, session=self.token, **extra)
        except frappe.ValidationError as error:
            self.fail(f"An unpaid configured invoice must create a Phone request: {error}")

    def test_missing_gateway_refuses_with_stable_id_without_creating_request(self):
        frappe.db.delete("Payment Gateway Account", {"name": self.gateway.name})
        with self.assertRaisesRegex(frappe.ValidationError, "URY-PAYMENT-NOT-CONFIGURED"):
            create_payment_request(self.token)
        self.assertEqual(frappe.db.count("Payment Request", {"reference_name": self.invoice_name}), 0)
        self.assertEqual(frappe.session.user, "Guest")

    def test_profile_without_gateway_phone_row_refuses_with_stable_id(self):
        frappe.db.delete("POS Payment Method", {"parent": self.pos.profile.name,
            "mode_of_payment": "P2 Phone"})
        frappe.clear_cache(doctype="POS Profile")
        with self.assertRaisesRegex(frappe.ValidationError, "URY-PAYMENT-NOT-CONFIGURED"):
            create_payment_request(self.token)
        self.assertEqual(frappe.db.count("Payment Request", {"reference_name": self.invoice_name}), 0)

    def test_server_populates_phone_row_for_exact_unpaid_invoice_total(self):
        result = self.request()
        self.assertEqual(result["amount"], 100)
        invoice = frappe.get_doc("POS Invoice", self.invoice_name)
        self.assertEqual([(p.mode_of_payment, p.amount) for p in invoice.payments if p.amount],
            [("P2 Phone", 100)])
        pr = frappe.get_doc("Payment Request", result["payment_request"])
        self.assertEqual((pr.grand_total, pr.payment_channel, pr.mode_of_payment, pr.docstatus),
            (100, "Phone", "P2 Phone", 1))
        self.assertEqual(frappe.session.user, "Guest")

    def test_client_amount_is_ignored_by_real_dispatch(self):
        result = self.request(amount=1, grand_total=2, payment_gateway_account="foreign")
        self.assertEqual(result["amount"], 100)
        self.assertEqual(frappe.db.get_value("Payment Request", result["payment_request"],
            "payment_gateway_account"), self.gateway.name)

    def test_uses_company_default_gateway_not_first_profile_phone_mode(self):
        frappe.set_user("Administrator")
        frappe.get_doc({"doctype": "Payment Gateway", "gateway": "P2 Preferred"}).insert()
        create_payment_gateway_account("P2 Preferred", payment_channel="Phone", company="URY")
        preferred = frappe.get_doc("Payment Gateway Account",
            {"payment_gateway": "P2 Preferred", "company": "URY"})
        preferred.is_default = 1
        preferred.save()
        frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "P2 Preferred",
            "type": "Phone", "accounts": [{"company": "URY",
            "default_account": preferred.payment_account}]}).insert()
        self.pos.profile.append("payments", {"mode_of_payment": "P2 Preferred"})
        self.pos.profile.save()
        frappe.set_user("Guest")
        result = self.request()
        pr = frappe.get_doc("Payment Request", result["payment_request"])
        self.assertEqual((pr.payment_gateway_account, pr.mode_of_payment),
            (preferred.name, "P2 Preferred"))

    def test_positive_invoice_outstanding_is_used_instead_of_total(self):
        frappe.db.set_value("POS Invoice", self.invoice_name, "outstanding_amount", 60)
        self.assertEqual(self.request()["amount"], 60)

    def test_retap_returns_existing_request_and_url(self):
        first = self.request()
        second = self.request()
        self.assertEqual((second["payment_request"], second["payment_url"], second["amount"]),
            (first["payment_request"], first["payment_url"], 100))
        self.assertEqual(frappe.db.count("Payment Request",
            {"reference_name": self.invoice_name, "docstatus": 1}), 1)

    def test_payment_status_returns_existing_payment_url(self):
        first = self.request()
        status = frappe.call(get_payment_status, session=self.token)
        self.assertEqual(status.get("payment_url"), first["payment_url"])

    def test_request_errors_are_not_reported_as_unconfigured(self):
        def refuse(**kwargs):
            frappe.throw("PAY-005: Configure native SMS Settings before requesting phone payment.")

        self.controller.request_for_payment = refuse
        with self.assertRaises(frappe.ValidationError) as raised:
            create_payment_request(self.token)
        self.assertIn("PAY-005", str(raised.exception))
        self.assertNotIn("isn't set up", str(raised.exception))

    def test_uses_phone_row_gateway_when_company_default_is_another_gateway(self):
        frappe.set_user("Administrator")
        frappe.get_doc({"doctype": "Payment Gateway", "gateway": "P2 Other"}).insert()
        create_payment_gateway_account("P2 Other", payment_channel="Phone", company="URY")
        other = frappe.get_doc("Payment Gateway Account",
            {"payment_gateway": "P2 Other", "company": "URY"})
        other.is_default = 1
        other.save()
        frappe.set_user("Guest")
        result = self.request()
        self.assertEqual(frappe.db.get_value("Payment Request", result["payment_request"],
            "payment_gateway_account"), self.gateway.name)

    def test_no_shopping_cart_redirect_or_automatic_sales_invoice(self):
        # Characterize the v16 trap independently of the missing-row defect.
        frappe.set_user("Administrator")
        invoice = frappe.get_doc("POS Invoice", self.invoice_name)
        for row in invoice.payments:
            row.amount = 0
        invoice.append("payments", {"mode_of_payment": "P2 Phone", "amount": 100})
        invoice.save()
        frappe.set_user("Guest")
        result = self.request()
        self.assertNotEqual(frappe.local.response.get("type"), "redirect")
        self.assertIsNone(frappe.local.response.get("location"))
        self.assertFalse(frappe.db.get_value("Payment Request", result["payment_request"], "make_sales_invoice"))
        self.assertEqual(frappe.db.get_value("Payment Request", result["payment_request"], "mute_email"), 1)

    def test_cashier_settlement_rebuilds_gateway_tender_and_submits(self):
        result = self.request()
        frappe.set_user("Administrator")
        frappe.get_doc("Payment Request", result["payment_request"]).set_as_paid()
        frappe.set_user(self.pos.user.name)
        settle_order(self.pos, self.invoice_name,
            payments=[{"mode_of_payment": "P2 Phone", "amount": 100}])
        saved = frappe.get_doc("POS Invoice", self.invoice_name)
        self.assertEqual(saved.docstatus, 1)
        self.assertEqual([(p.mode_of_payment, p.amount) for p in saved.payments], [("P2 Phone", 100)])
