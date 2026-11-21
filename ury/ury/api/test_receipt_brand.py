"""Site-less native print and QR identity contracts."""

import base64
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from ury.ury.api import self_ordering, ury_print
from ury.www import order


class TestReceiptBrand(unittest.TestCase):
    def test_network_bill_defaults_to_zero_and_forwards_explicit_letterhead_flags(self):
        invoice, printer = MagicMock(), MagicMock()
        with (patch.object(ury_print, 'frappe') as framework,
              patch.object(ury_print, 'validate_print_permission'),
              patch.object(ury_print, 'check_split_target_access') as check_split):
            framework.get_doc.side_effect = lambda doctype, name: invoice if doctype == "POS Invoice" else printer
            for options in ({}, {'no_letterhead': 1}, {'no_letterhead': 0}):
                with self.subTest(options=options):
                    ury_print.network_printing('POS Invoice', 'INV-1', 'Test printer', print_format='Receipt', **options)
                    check_split.assert_called_with(invoice, 'write')
                    self.assertEqual(printer.print_doc.call_args.kwargs['no_letterhead'], options.get('no_letterhead', 0))

    def test_non_ascii_qr_signature_renders_site_identity_and_no_profile_override(self):
        token = base64.urlsafe_b64encode('Profile|Table|é'.encode()).decode()
        settings = SimpleNamespace(app_name='Example site', app_logo=None, favicon=None)
        with (patch.object(frappe, 'form_dict', {'t': token}), patch.object(frappe, 'conf', {}),
              patch.object(frappe, 'get_cached_doc', return_value=settings), patch.object(frappe, 'db', MagicMock()),
              patch.object(frappe.sessions, 'get_csrf_token', return_value='test'),
              patch.object(frappe.website.utils, 'get_boot_data', return_value={}),
              patch.object(frappe.utils, 'get_build_version', return_value='test'),
              patch.object(self_ordering, '_get_profile_secret', return_value='test-only-secret')):
            context = order.get_context({})
        self.assertEqual(context.get('brand'), {'name': 'Example site', 'logo': None, 'favicon': None})
