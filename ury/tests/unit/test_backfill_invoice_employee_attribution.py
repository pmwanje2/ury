"""The invoice patch must leave profile imports to migrate's final fixture sync."""
import runpy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class InvoiceEmployeeBackfillTest(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.flags = SimpleNamespace(in_fixtures=False)
        self.import_doc = Mock()
        self.sync_fixtures = Mock()
        frappe = SimpleNamespace(db=self.db, flags=self.flags, get_app_path=lambda *parts: '/'.join(parts))
        modules = {
            'frappe': frappe,
            'frappe.utils.fixtures': SimpleNamespace(sync_fixtures=self.sync_fixtures),
            'frappe.core.doctype.data_import.data_import': SimpleNamespace(import_doc=self.import_doc),
        }
        self.enterContext(patch.dict(sys.modules, modules))
        path = Path(__file__).resolve().parents[2] / 'patches/v3_0/backfill_invoice_employee_attribution.py'
        self.execute = runpy.run_path(str(path))['execute']

    def test_imports_only_custom_fields_once_before_backfill(self):
        for missing in (None, ('Sales Invoice', 'custom_closing_employee')):
            with self.subTest(missing=missing or 'all four columns'):
                self.db.has_column.side_effect = lambda dt, dest: missing is not None and (dt, dest) != missing
                self.import_doc.side_effect = lambda *a, **kw: self.assertTrue(self.flags.in_fixtures)
                self.db.sql.side_effect = lambda sql: self.import_doc.assert_called_once_with(
                    'ury/fixtures/custom_field.json', sort=True
                )
                self.execute()
                self.sync_fixtures.assert_not_called()
                self.import_doc.assert_called_once_with('ury/fixtures/custom_field.json', sort=True)
                self.assertEqual(self.db.sql.call_count, 4)
                self.assertFalse(self.flags.in_fixtures)
                self.db.reset_mock()
                self.import_doc.reset_mock()

    def test_existing_columns_skip_fixture_import(self):
        self.db.has_column.return_value = True
        self.execute()
        self.sync_fixtures.assert_not_called()
        self.import_doc.assert_not_called()
        self.assertEqual(self.db.sql.call_count, 4)

    def test_import_error_restores_fixture_flag_and_stops_backfill(self):
        self.db.has_column.return_value = False
        self.import_doc.side_effect = ValueError('invalid fixture')
        for previous in (False, True):
            with self.subTest(previous=previous):
                self.flags.in_fixtures = previous
                with self.assertRaisesRegex(ValueError, 'invalid fixture'):
                    self.execute()
                self.assertIs(self.flags.in_fixtures, previous)
                self.db.sql.assert_not_called()
