from unittest.mock import ANY, MagicMock, patch, mock_open, PropertyMock
import base64

import frappe
from frappe.tests.utils import FrappeTestCase

from ury.ury.api.ury_print import (
	network_printing,
	select_network_printer,
	qz_print_update,
	print_pos_page,
	qz_certificate,
	signature_promise,
	_get_qz_private_key,
	_mark_invoice_printed,
)


def _ordinary_invoice():
	invoice = MagicMock()
	invoice.get.return_value = None
	invoice.pos_profile = None
	return invoice


class BillPrintTestCase(FrappeTestCase):
	def setUp(self):
		super().setUp()
		# Routing and print bookkeeping tests supply their own invoice lookups.
		# Leave first-print and reprint authorization to the floor-action suite.
		self.enterContext(patch(
			"ury.ury_pos.floor_actions.authorize_and_count_bill_print",
			return_value={"is_reprint": False, "print_number": 1},
		))
		self.enterContext(patch("ury.ury.api.ury_print.frappe.utils.now_datetime",
								return_value="2026-11-03 10:15:00"))
		self.enterContext(patch("ury.ury.api.ury_fulfilment_posting_service.trigger_manufacture_on_bill_print"))


class TestNetworkPrinting(BillPrintTestCase):
	@patch("ury.ury.api.ury_print.validate_print_permission")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_network_printing_success(
		self, mock_get_doc, mock_validate_permission
	):
		"""A successful engine result gains the invoice key and triggers manufacture."""
		mock_printer_doc = MagicMock()
		mock_printer_doc.print_doc.return_value = {
			"status": "Success",
			"cups_job_id": 123,
		}
		mock_get_doc.side_effect = [_ordinary_invoice(), mock_printer_doc]

		with patch(
			"ury.ury.api.ury_fulfilment_posting_service.trigger_manufacture_on_bill_print"
		) as mock_trigger:
			result = network_printing("POS Invoice", "INV-001", "Printer-1")

		self.assertEqual(result, "Success")
		self.assertEqual(result["invoice"], "INV-001")
		mock_trigger.assert_called_once_with("INV-001")

		print_doc_kwargs = mock_printer_doc.print_doc.call_args[1]
		self.assertEqual(print_doc_kwargs["job_type"], "BILL")
		self.assertEqual(print_doc_kwargs["doctype"], "POS Invoice")
		self.assertEqual(print_doc_kwargs["name"], "INV-001")
		self.assertEqual(print_doc_kwargs["extra_metadata"]["invoice"], "INV-001")
		self.assertIn("job_owner", print_doc_kwargs["extra_metadata"])

	@patch("ury.ury.api.ury_print.validate_print_permission")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_network_printing_failure(
		self, mock_get_doc, mock_validate_permission
	):
		"""A failed engine result is returned without triggering manufacture."""
		mock_printer_doc = MagicMock()
		mock_printer_doc.print_doc.return_value = {
			"status": "Failure",
			"message": "printer offline",
		}
		mock_get_doc.side_effect = [_ordinary_invoice(), mock_printer_doc]

		with patch(
			"ury.ury.api.ury_fulfilment_posting_service.trigger_manufacture_on_bill_print"
		) as mock_trigger:
			result = network_printing("POS Invoice", "INV-001", "Printer-1")

		self.assertEqual(result, "Failure")
		self.assertNotIn("invoice", result)
		mock_trigger.assert_not_called()

	@patch("ury.ury.api.ury_print.validate_print_permission")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_network_printing_non_dict_result_passthrough(
		self, mock_get_doc, mock_validate_permission
	):
		"""Non-dict engine results are returned unchanged (no PrintResult wrap)."""
		mock_printer_doc = MagicMock()
		mock_printer_doc.print_doc.return_value = "Success"
		mock_get_doc.side_effect = [_ordinary_invoice(), mock_printer_doc]

		result = network_printing("POS Invoice", "INV-001", "Printer-1")

		self.assertEqual(result, "Success")
		self.assertIsInstance(result, str)


class TestSelectNetworkPrinter(BillPrintTestCase):
	@patch("ury.ury.api.ury_print._network_print_internal", create=True)
	@patch("ury.ury.api.ury_print.frappe.get_all")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_select_network_printer_uses_room_printers(
		self, mock_get_doc, mock_db_get_value, mock_get_all, mock_network_printing
	):
		"""Test that select_network_printer uses room printers if table has room."""
		mock_invoice = _ordinary_invoice()
		mock_invoice.restaurant_table = "T-01"
		mock_get_doc.return_value = mock_invoice

		mock_db_get_value.side_effect = [
			"T-01",  # table from invoice
			"Standard",  # print_format from POS Profile
			"Room-1",  # room from table
		]

		mock_get_all.return_value = ["Room Printer 1", "Room Printer 2"]

		mock_network_printing.return_value = "Success"

		result = select_network_printer("POS-1", "INV-001")

		self.assertEqual(result, "Success")
		self.assertEqual(
			[call.args[2] for call in mock_network_printing.call_args_list],
			["Room Printer 1", "Room Printer 2"],
		)

	@patch("ury.ury.api.ury_print._network_print_internal", create=True)
	@patch("ury.ury.api.ury_print.frappe.get_all")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_select_network_printer_no_printers_found(
		self, mock_get_doc, mock_db_get_value, mock_get_all, mock_network_printing
	):
		"""Test that select_network_printer returns None when no printers found."""
		mock_invoice = _ordinary_invoice()
		mock_invoice.restaurant_table = "T-01"
		mock_get_doc.return_value = mock_invoice

		mock_db_get_value.side_effect = ["T-01", "Standard", "Room-1"]
		mock_get_all.return_value = []

		result = select_network_printer("POS-1", "INV-001")

		self.assertIsNone(result)
		mock_network_printing.assert_not_called()

	@patch("ury.ury.api.ury_print._network_print_internal", create=True)
	@patch("ury.ury.api.ury_print.frappe.get_all")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_select_network_printer_uses_pos_profile_printers(
		self, mock_get_doc, mock_db_get_value, mock_get_all, mock_network_printing
	):
		"""Test that select_network_printer uses POS profile printers if no table."""
		mock_invoice = _ordinary_invoice()
		mock_invoice.restaurant_table = None
		mock_get_doc.return_value = mock_invoice

		mock_db_get_value.side_effect = [None, "Standard"]
		mock_get_all.return_value = ["POS Printer 1"]

		mock_network_printing.return_value = "Success"

		result = select_network_printer("POS-1", "INV-001")

		self.assertEqual(result, "Success")
		mock_network_printing.assert_called_once()

	@patch("ury.ury.api.ury_print.frappe.has_permission")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_select_network_printer_permission_denied(
		self, mock_get_doc, mock_has_permission
	):
		"""Test that select_network_printer throws on permission denied."""
		mock_invoice = _ordinary_invoice()
		mock_get_doc.return_value = mock_invoice
		mock_has_permission.return_value = False

		with self.assertRaises(frappe.PermissionError):
			select_network_printer("POS-1", "INV-001")


class TestQzPrintUpdate(BillPrintTestCase):
	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_qz_print_update_without_table(
		self, mock_get_doc, mock_db_get_value, mock_db_set_value
	):
		"""Test qz_print_update when invoice has no table."""
		mock_invoice = _ordinary_invoice()
		mock_get_doc.return_value = mock_invoice

		# table lookup, prior invoice_printed read by the helper, post-update validation
		mock_db_get_value.side_effect = [None, 0, 1]

		result = qz_print_update("INV-001")

		self.assertEqual(result, {"status": "Success"})
		mock_db_set_value.assert_called_once()

	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_qz_print_update_with_table_and_invoice_not_printed(
		self, mock_get_doc, mock_db_get_value, mock_db_set_value
	):
		"""Test qz_print_update with table when invoice_printed is 0."""
		mock_invoice = _ordinary_invoice()
		mock_get_doc.return_value = mock_invoice

		mock_db_get_value.side_effect = ["T-01", 0, 1, 0]

		with patch("ury.ury.api.ury_print._free_tables_if_no_open_invoices") as mock_release:
			result = qz_print_update("INV-001")

		self.assertEqual(result, {"status": "Success"})
		mock_release.assert_called_once_with("T-01", printed=True, branch=None, commit=False)

	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_qz_print_update_with_table_already_printed(
		self, mock_get_doc, mock_db_get_value, mock_db_set_value
	):
		"""Test qz_print_update when invoice_printed is already 1."""
		mock_invoice = _ordinary_invoice()
		mock_get_doc.return_value = mock_invoice

		mock_db_get_value.side_effect = ["T-01", 1]

		result = qz_print_update("INV-001")

		self.assertEqual(result, {"status": "Success"})
		mock_db_set_value.assert_not_called()


class TestPrintPosPage(BillPrintTestCase):
	@patch("ury.ury.api.ury_print._free_tables_if_no_open_invoices")
	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.publish_realtime")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_print_pos_page_success(
		self,
		mock_get_doc,
		mock_publish,
		mock_db_get_value,
		mock_db_set_value,
		mock_release,
	):
		"""Test print_pos_page successfully publishes and updates invoice."""
		mock_doc = _ordinary_invoice()
		mock_get_doc.return_value = mock_doc

		mock_db_get_value.side_effect = [("T-01", "Branch-1", "INV-001"), 0]

		print_pos_page("POS Invoice", "INV-001", "Standard")

		mock_publish.assert_called_once()
		call_args = mock_publish.call_args
		self.assertIn("print_", call_args[0][0])

		mock_db_set_value.assert_called_once_with(
			"POS Invoice",
			"INV-001",
			{"invoice_printed": 1, "custom_printing_time": ANY},
			update_modified=True,
		)
		mock_release.assert_called_once_with("T-01", printed=True, branch="Branch-1")

	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.publish_realtime")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_print_pos_page_without_table(
		self,
		mock_get_doc,
		mock_publish,
		mock_db_get_value,
		mock_db_set_value,
	):
		"""Test print_pos_page when invoice has no table."""
		mock_doc = _ordinary_invoice()
		mock_get_doc.return_value = mock_doc

		mock_db_get_value.side_effect = [(None, "Branch-1", "INV-001"), 0]

		with patch("ury.ury.api.ury_print._free_tables_if_no_open_invoices") as mock_release:
			print_pos_page("POS Invoice", "INV-001", "Standard")

		mock_publish.assert_called_once()
		mock_db_set_value.assert_called_once()
		mock_release.assert_not_called()

	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	@patch("ury.ury.api.ury_print.frappe.db.get_value")
	@patch("ury.ury.api.ury_print.frappe.publish_realtime")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_print_pos_page_already_printed_no_release(
		self,
		mock_get_doc,
		mock_publish,
		mock_db_get_value,
		mock_db_set_value,
	):
		"""Test print_pos_page when invoice already printed."""
		mock_doc = _ordinary_invoice()
		mock_get_doc.return_value = mock_doc

		# First call: table exists, invoice_printed already 1
		mock_db_get_value.side_effect = [("T-01", "Branch-1", "INV-001"), 1]

		with patch("ury.ury.api.ury_print._free_tables_if_no_open_invoices") as mock_release:
			print_pos_page("POS Invoice", "INV-001", "Standard")

		mock_publish.assert_called_once()
		mock_release.assert_not_called()

	@patch("ury.ury.api.ury_print.frappe.has_permission")
	@patch("ury.ury.api.ury_print.frappe.get_doc")
	def test_print_pos_page_permission_denied(
		self, mock_get_doc, mock_has_permission
	):
		"""Test print_pos_page throws on permission denied."""
		mock_doc = _ordinary_invoice()
		mock_get_doc.return_value = mock_doc
		mock_has_permission.return_value = False

		with self.assertRaises(frappe.PermissionError):
			print_pos_page("POS Invoice", "INV-001", "Standard")


class TestQzCertificate(FrappeTestCase):
	@patch("ury.ury.api.ury_print.frappe.get_site_config")
	def test_qz_certificate_returns_cert(self, mock_get_config):
		"""Test qz_certificate returns certificate from config."""
		from certifi import where
		from cryptography import x509
		from cryptography.hazmat.primitives.serialization import Encoding
		from pathlib import Path
		cert_value = x509.load_pem_x509_certificate(Path(where()).read_bytes()).public_bytes(Encoding.PEM).decode()
		mock_get_config.return_value = {"qz_cert": cert_value}

		result = qz_certificate()

		self.assertEqual(result, cert_value)

	@patch("ury.ury.api.ury_print.frappe.get_site_config")
	def test_qz_certificate_returns_none_if_not_set(self, mock_get_config):
		"""Test qz_certificate returns None if cert not configured."""
		mock_get_config.return_value = {}

		result = qz_certificate()

		self.assertIsNone(result)

	@patch("ury.ury.api.ury_print.frappe.get_site_config", return_value={"qz_cert": "-----BEGIN PRIVATE KEY-----\ninvalid\n-----END PRIVATE KEY-----"})
	def test_qz_certificate_rejects_key_in_certificate_setting(self, _config):
		with self.assertRaises(frappe.ValidationError):
			qz_certificate()

	def test_qz_certificate_guest_denied(self):
		"""Test qz_certificate denies Guest user."""
		with patch("frappe.session") as mock_session:
			mock_session.user = "Guest"
			with self.assertRaises(frappe.PermissionError):
				qz_certificate()


class TestSignaturePromise(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.print_settings = frappe._dict(qz_signing_roles="  Receipt Operator  \n")
		self.enterContext(patch("ury.ury.api.ury_print.frappe.get_single", return_value=self.print_settings))

	@patch("ury.ury.api.ury_print.frappe.get_roles")
	@patch("ury.ury.api.ury_print._get_qz_private_key")
	def test_signature_promise_success(self, mock_get_key, mock_get_roles):
		"""Test successful signature promise."""
		from cryptography.hazmat.primitives.asymmetric import rsa
		from cryptography.hazmat.primitives import serialization

		private_key = rsa.generate_private_key(
			public_exponent=65537,
			key_size=2048,
		)
		pem = private_key.private_bytes(
			encoding=serialization.Encoding.PEM,
			format=serialization.PrivateFormat.TraditionalOpenSSL,
			encryption_algorithm=serialization.NoEncryption(),
		)

		mock_get_key.return_value = pem
		mock_get_roles.return_value = ["Receipt Operator"]

		with patch("frappe.session") as mock_session:
			mock_session.user = "admin_user"
			result = signature_promise("test_payload")

		self.assertIsInstance(result, str)
		decoded = base64.b64decode(result)
		self.assertIsNotNone(decoded)

	@patch("ury.ury.api.ury_print.frappe.get_roles")
	def test_signature_promise_guest_denied(self, mock_get_roles):
		"""Test signature_promise denies Guest user."""
		with patch("frappe.session") as mock_session:
			mock_session.user = "Guest"
			with self.assertRaises(frappe.PermissionError):
				signature_promise("test_payload")

	@patch("ury.ury.api.ury_print.frappe.get_roles")
	def test_signature_promise_insufficient_roles(self, mock_get_roles):
		"""Test signature_promise denies users without signing roles."""
		mock_get_roles.return_value = ["System Manager", "URY Cashier"]

		with patch("frappe.session") as mock_session:
			mock_session.user = "normal_user"
			with self.assertRaises(frappe.PermissionError):
				signature_promise("test_payload")

	@patch("ury.ury.api.ury_print.frappe.get_roles", return_value=["Receipt Operator"])
	@patch("ury.ury.api.ury_print._get_qz_private_key")
	def test_signature_promise_empty_allowlist_revokes_signing(self, mock_get_key, _mock_get_roles):
		self.print_settings.qz_signing_roles = ""
		with self.assertRaises(frappe.PermissionError):
			signature_promise("test_payload")
		mock_get_key.assert_not_called()

	@patch("ury.ury.api.ury_print.frappe.get_roles")
	def test_signature_promise_missing_payload(self, mock_get_roles):
		"""Test signature_promise throws when payload is missing."""
		mock_get_roles.return_value = ["Receipt Operator"]

		with patch("frappe.session") as mock_session:
			mock_session.user = "admin_user"
			with self.assertRaises(frappe.ValidationError):
				signature_promise(toSign=None)


class TestGetQzPrivateKey(FrappeTestCase):
	@patch("ury.ury.api.ury_print.frappe.get_site_config")
	def test_get_qz_private_key_from_pem_content(self, mock_get_config):
		"""Test _get_qz_private_key reads PEM content directly."""
		pem_content = "-----BEGIN RSA PRIVATE KEY-----\nkey_data\n-----END RSA PRIVATE KEY-----"
		mock_get_config.return_value = {"qz_private_key": pem_content}

		result = _get_qz_private_key()

		self.assertEqual(result, pem_content.encode())

	@patch("ury.ury.api.ury_print.frappe.get_site_config")
	def test_get_qz_private_key_not_configured(self, mock_get_config):
		"""Test _get_qz_private_key throws when not configured."""
		mock_get_config.return_value = {}

		with self.assertRaises(frappe.ValidationError):
			_get_qz_private_key()

	@patch("ury.ury.api.ury_print.frappe.get_site_config")
	def test_get_qz_private_key_empty_config(self, mock_get_config):
		"""Test _get_qz_private_key with empty string in config."""
		mock_get_config.return_value = {"qz_private_key": "   "}

		with self.assertRaises(frappe.ValidationError):
			_get_qz_private_key()


class TestQzSigningRoles(FrappeTestCase):
	def test_seeded_print_settings_include_the_existing_signing_roles(self):
		settings = frappe.get_single("POS Print Settings")
		self.assertTrue(settings.qz_signing_roles_initialized)
		self.assertCountEqual(settings.qz_signing_roles.splitlines(), [
			"Administrator", "System Manager", "URY Admin", "URY Manager",
			"URY Cashier", "Restaurant Kitchen",
		])


class TestMarkInvoicePrinted(FrappeTestCase):
	@patch("ury.ury.api.ury_print.frappe.utils.now_datetime", return_value="2026-11-03 10:15:00")
	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	def test_first_print_records_printing_time(self, mock_set_value, _mock_now):
		"""The first print sets invoice_printed and custom_printing_time."""
		_mark_invoice_printed("INV-001", was_printed=0)

		mock_set_value.assert_called_once_with(
			"POS Invoice",
			"INV-001",
			{"invoice_printed": 1, "custom_printing_time": "2026-11-03 10:15:00"},
			update_modified=True,
		)

	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	def test_reprint_keeps_original_printing_time(self, mock_set_value):
		"""A reprint must not overwrite the first print time."""
		_mark_invoice_printed("INV-001", was_printed=1)

		mock_set_value.assert_called_once_with(
			"POS Invoice", "INV-001", {"invoice_printed": 1}, update_modified=True
		)

	@patch("ury.ury.api.ury_print.frappe.utils.now_datetime", return_value="2026-11-03 10:15:00")
	@patch("ury.ury.api.ury_print.frappe.db.set_value")
	@patch("ury.ury.api.ury_print.frappe.db.get_value", return_value=0)
	def test_reads_previous_state_when_not_supplied(self, _mock_get, mock_set_value, _mock_now):
		"""Without was_printed, the helper reads invoice_printed itself."""
		_mark_invoice_printed("INV-001", update_modified=False)

		mock_set_value.assert_called_once_with(
			"POS Invoice",
			"INV-001",
			{"invoice_printed": 1, "custom_printing_time": "2026-11-03 10:15:00"},
			update_modified=False,
		)


class TestReceiptDelivery(BillPrintTestCase):
	def test_only_confirmed_delivery_triggers_effects_and_duplicate_commit_is_safe(self):
		from ury.ury.api import ury_print as api
		from uuid import uuid4
		invoice = "Fixture-" + uuid4().hex
		frappe.get_doc({"doctype": "POS Invoice", "name": invoice, "docstatus": 1}).db_insert()
		with patch("frappe.www.printview.get_html_and_style", return_value={"html": "<p>receipt</p>"}), patch.object(api, "_record_delivered_bill", return_value={"status": "Success"}) as effects:
			request_id = str(uuid4())
			first = api.prepare_receipt_delivery(invoice, request_id)
			self.assertEqual(api.prepare_receipt_delivery(invoice, request_id), first)
			effects.assert_not_called()
			result = api.complete_receipt_delivery(invoice, first["delivery_id"])
			self.assertEqual(result["status"], "Success")
			api.complete_receipt_delivery(invoice, first["delivery_id"])
			effects.assert_called_once()
